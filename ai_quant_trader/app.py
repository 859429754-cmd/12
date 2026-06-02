from __future__ import annotations

import argparse
import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from ai_quant_trader.brain.budget import DeepSeekBudgetGuard
from ai_quant_trader.brain.deepseek import DeepSeekBrain
from ai_quant_trader.brain.wakeup import WakeupEngine
from ai_quant_trader.core.config import load_config
from ai_quant_trader.core.control import RuntimeControlManager
from ai_quant_trader.core.logging import setup_logging
from ai_quant_trader.core.models import (
    AggregatedOrderflow,
    AiDecision,
    DenseZone,
    HealthStatus,
    NewsDigest,
    OrderRequest,
    PatternCandidate,
    PositionSnapshot,
    RegimePattern,
    Side,
    SignalAction,
    StrategySignal,
)
from ai_quant_trader.data.market import MarketDataClient
from ai_quant_trader.data.news import NewsCollector
from ai_quant_trader.data.news_memory import DailyNewsFlashStore, NewsMemoryStore
from ai_quant_trader.data.orderflow import MultiExchangeOrderflowClient
from ai_quant_trader.execution.gateway import create_exchange_gateway, execution_mode_from_config
from ai_quant_trader.execution.lifecycle import OrderLifecycleManager, OrderRejected, OrderSubmissionUncertain
from ai_quant_trader.execution.reconciliation import run_read_only_reconciliation
from ai_quant_trader.execution.safety import ExchangeSafetyMonitor
from ai_quant_trader.features.dense_zone import DenseZoneAnalyzer
from ai_quant_trader.features.orderflow import OrderflowAggregator
from ai_quant_trader.features.patterns import PatternDetector
from ai_quant_trader.features.regime import RegimePatternAnalyzer
from ai_quant_trader.monitoring.ai_drift import AIDriftMonitor
from ai_quant_trader.monitoring.data_health import DataHealthMonitor
from ai_quant_trader.monitoring.heartbeat import WorkerHeartbeatRecorder
from ai_quant_trader.monitoring.price import PriceWakeupMonitor
from ai_quant_trader.ops.systemd import SystemdNotifier
from ai_quant_trader.optimizer.proposals import StrategyOptimizer
from ai_quant_trader.reporting.hourly import HourlyReportBuilder
from ai_quant_trader.risk.manager import RiskManager
from ai_quant_trader.storage.sqlite import SQLiteStore
from ai_quant_trader.strategy.lab import StrategyCodeError, generate_custom_signal, get_active_strategy
from ai_quant_trader.strategy.trend import TrendStrategy
from ai_quant_trader.strategy.trend_state import TrendPositionState, TrendStateStore

logger = logging.getLogger(__name__)

TREND_ACCOUNT_SLOT = "trend"


class TradingApp:
    def __init__(self, config_path: str = "config/config.yaml"):
        self.config_path = config_path
        self.config = load_config(config_path)
        self.store = SQLiteStore(self.config.runtime.database_path, self.config.runtime.audit_log_path)
        self.control = RuntimeControlManager(self.store, config_path)
        self.state = self.control.load_state([symbol.symbol for symbol in self.config.symbols])
        if self.config.runtime.allow_live_orders_after_restart:
            self.state.opening_paused = False
            for symbol in self.config.symbols:
                if symbol.enabled_on_boot:
                    self.state.authorize_symbol(symbol.symbol)

        self.market = MarketDataClient()
        self.orderflow_client = MultiExchangeOrderflowClient(
            self.config.orderflow.exchanges,
            live_public_data=execution_mode_from_config(self.config) == "live",
        )
        self.orderflow_aggregator = OrderflowAggregator(self.config.orderflow.weights)
        self.news = NewsCollector(
            self.config.news.rss_sources,
            self.config.news.scrape_sources,
            max_age_hours=self.config.news.max_age_hours,
            jin10_enabled=self.config.news.jin10_enabled,
        )
        self.news_memory = NewsMemoryStore()
        self.daily_news = DailyNewsFlashStore()
        self.brain = DeepSeekBrain(
            base_url=self.config.ai.base_url,
            model=self.config.ai.decision_model,
        )
        self.deepseek_budget = DeepSeekBudgetGuard.from_config(self.store, self.config.ai)
        self.risk = RiskManager(self.config.risk, self.state)
        self.execution = create_exchange_gateway(self.config, account_slot=TREND_ACCOUNT_SLOT)
        self.exchange_safety = ExchangeSafetyMonitor(self.config.risk.stale_data_seconds)
        self.order_lifecycle = OrderLifecycleManager(self.store, gateway_mode=execution_mode_from_config(self.config))
        self.ai_drift = AIDriftMonitor(self.store)
        self.data_health = DataHealthMonitor(
            stale_data_seconds=self.config.risk.stale_data_seconds,
            news_max_age_hours=self.config.news.max_age_hours,
        )
        self.heartbeat = WorkerHeartbeatRecorder(self.store)
        self.systemd = SystemdNotifier.from_environment()
        self.optimizer = StrategyOptimizer(self.store, self.brain, self.control)
        self.report_builder = HourlyReportBuilder()
        self.dense_zone = DenseZoneAnalyzer()
        self.patterns = PatternDetector()
        self.regime_patterns = RegimePatternAnalyzer()
        self.price_monitor = PriceWakeupMonitor(
            threshold_pct=self.config.runtime.price_wakeup_threshold_pct,
            volatility_multiplier=self.config.runtime.price_wakeup_volatility_multiplier,
        )
        self.wakeup_engine = WakeupEngine()
        self.trend_state = TrendStateStore()
        self._price_wakeup_cooldown: dict[str, datetime] = {}
        self._news_risk_review_seen: set[str] = set()
        self.trend_strategies: dict[str, TrendStrategy] = {}
        self._refresh_symbol_strategies()

    async def run_once(self, equity: float = 10_000.0, live_news: bool = True) -> None:
        await self.reload_runtime_config()
        equity = await self._effective_equity(equity)
        news_digest = await self._news_for_trading_cycle(live_news=live_news)
        rows = []
        active_symbols = [s for s in self.config.symbols if self.state.should_report(s.symbol)]
        if not active_symbols:
            report = "# AI hourly report\n\nNo symbols are enabled for analysis."
            self.store.insert("hourly_reports", {"text": report})
            return

        risk_symbols = sorted({symbol.symbol for symbol in self.config.symbols})
        await self._refresh_exchange_safety(risk_symbols)
        positions = await self._fetch_positions(risk_symbols)
        major_news_events = self.wakeup_engine.events_from_news(news_digest)
        for symbol_cfg in active_symbols:
            candles = await self.market.fetch_ohlcv(symbol_cfg.symbol, symbol_cfg.timeframe)
            position = next(pos for pos in positions if pos.symbol == symbol_cfg.symbol)
            position.mark_price = float(candles["close"].iloc[-1])
            signal = self._generate_local_signal(symbol_cfg.symbol, symbol_cfg.timeframe, candles, position, equity)
            signal = self._attach_major_news_context(signal, major_news_events)
            orderflow_summaries = await self.orderflow_client.fetch_summaries(symbol_cfg.symbol)
            aggregated = self.orderflow_aggregator.aggregate(symbol_cfg.symbol, orderflow_summaries)
            zone = self.dense_zone.calculate(symbol_cfg.symbol, candles)
            pattern = self.patterns.detect(symbol_cfg.symbol, candles)
            regime_pattern = self.regime_patterns.analyze(symbol_cfg.symbol, candles, zone, pattern)
            signal = self.regime_patterns.enrich_signal(signal, regime_pattern)
            if self._ai_enabled_for_symbol(symbol_cfg.symbol) and self._should_call_deepseek_for_signal(signal, position):
                ai = await self._analyze_with_deepseek_budget(
                    "trading_cycle",
                    signal,
                    aggregated,
                    zone,
                    pattern,
                    news_digest,
                    regime_pattern,
                )
            elif self._ai_enabled_for_symbol(symbol_cfg.symbol):
                self.deepseek_budget.record_skipped(
                    symbol=symbol_cfg.symbol,
                    call_type="trading_cycle",
                    reason="no_signal_no_position",
                )
                ai = self.brain.local_fallback_decision(
                    signal,
                    aggregated,
                    zone,
                    pattern,
                    news_digest,
                    "deepseek_skipped:no_signal_no_position",
                    regime_pattern,
                )
            else:
                ai = self.brain.local_fallback_decision(
                    signal,
                    aggregated,
                    zone,
                    pattern,
                    news_digest,
                    "ai_disabled_for_symbol",
                    regime_pattern,
                )
            drift = self.ai_drift.evaluate(symbol_cfg.symbol, ai)
            data_health = self.data_health.evaluate_symbol(
                symbol=symbol_cfg.symbol,
                timeframe=symbol_cfg.timeframe,
                candles=candles,
                news=news_digest,
                orderflow=aggregated,
            )
            risk = self.risk.evaluate(signal, ai, equity, positions)

            self.store.insert("orderflow_summaries", aggregated, symbol_cfg.symbol)
            self.store.insert("dense_zones", zone, symbol_cfg.symbol)
            self.store.insert("data_health", data_health, symbol_cfg.symbol)
            self.store.insert("ai_drift_checks", drift, symbol_cfg.symbol)
            self.store.insert("ai_decisions", ai, symbol_cfg.symbol)

            if risk.allowed and signal.action in {SignalAction.EXIT_LONG, SignalAction.EXIT_SHORT}:
                order = await self.execution.close_position(symbol_cfg.symbol, reason=risk.reason)
                if order:
                    await self._cancel_native_stop_order(symbol_cfg.symbol)
                    self.trend_state.clear(symbol_cfg.symbol)
                    self.store.insert("orders", order, symbol_cfg.symbol)

            if risk.allowed and signal.action in {SignalAction.LONG, SignalAction.SHORT}:
                if not data_health.can_open_new_entries:
                    risk.allowed = False
                    risk.reason = f"data_health_blocks_new_entry:{data_health.status.value}:{data_health.reason}"
                    risk.warnings.append("market/news/orderflow freshness gate blocked this entry")
                    rows.append((signal, ai, aggregated, zone, risk))
                    continue
                safety = self.exchange_safety.state
                if execution_mode_from_config(self.config) == "live" and not safety.can_open_new_entries:
                    risk.allowed = False
                    risk.reason = f"exchange_safety_blocks_new_entry:{safety.status.value}:{safety.reason}"
                    risk.warnings.append(safety.manual_action)
                    self.store.insert(
                        "order_lifecycle",
                        {
                            "state": "blocked_before_submit",
                            "symbol": symbol_cfg.symbol,
                            "reason": risk.reason,
                            "gateway_mode": execution_mode_from_config(self.config),
                            "risk_state": safety.model_dump(mode="json"),
                        },
                        symbol_cfg.symbol,
                    )
                    rows.append((signal, ai, aggregated, zone, risk))
                    continue
                if self._same_direction_trend_state_exists(symbol_cfg.symbol, signal):
                    logger.warning("trend_entry_skipped_existing_local_state", extra={"symbol": symbol_cfg.symbol})
                    rows.append((signal, ai, aggregated, zone, risk))
                    continue
                opposite = self._opposite_position(signal, positions)
                if opposite is not None:
                    close_order = await self.execution.close_position(symbol_cfg.symbol, reason="reverse_signal_close_first")
                    if close_order is None:
                        logger.warning("reverse_close_skipped_entry", extra={"symbol": symbol_cfg.symbol})
                        rows.append((signal, ai, aggregated, zone, risk))
                        continue
                    await self._cancel_native_stop_order(symbol_cfg.symbol)
                    self.trend_state.clear(symbol_cfg.symbol)
                    self.store.insert("orders", close_order, symbol_cfg.symbol)
                entry_request = OrderRequest(
                    symbol=symbol_cfg.symbol,
                    side="buy" if signal.action == SignalAction.LONG else "sell",
                    amount=risk.clipped_qty,
                    reduce_only=False,
                    client_order_id=f"aiq_{uuid.uuid4().hex[:20]}",
                    reason=risk.reason,
                )
                try:
                    order = await self.order_lifecycle.submit_market_order(self.execution, entry_request)
                except OrderSubmissionUncertain as exc:
                    if execution_mode_from_config(self.config) == "live":
                        state = self.exchange_safety.mark_failure("entry_order_submission_state_unknown", [entry_request.client_order_id])
                        self.store.insert("exchange_health", state.model_dump(mode="json"))
                    raise RuntimeError("entry_order_submission_state_unknown") from exc
                self.store.insert("orders", order, symbol_cfg.symbol)
                entry_state = self._record_trend_entry_state(symbol_cfg.symbol, signal, order.price or signal.current_price)
                if entry_state is None and execution_mode_from_config(self.config) == "live":
                    close_order = await self.execution.close_position(symbol_cfg.symbol, reason="missing_trend_stop_state_emergency_close")
                    if close_order:
                        self.store.insert("orders", close_order, symbol_cfg.symbol)
                    raise RuntimeError("live_entry_missing_trend_stop_state")
                if entry_state is not None:
                    await self._place_native_stop_loss(symbol_cfg.symbol, signal, entry_state, risk.clipped_qty)

            rows.append((signal, ai, aggregated, zone, risk))

        report = self.report_builder.build(rows, news_digest, major_news_only=self.state.major_news_only)
        image_path = self.report_builder.render_image_card(
            rows,
            news_digest,
            output_path="data/reports/latest_hourly.png",
            major_news_only=self.state.major_news_only,
        )
        self.store.insert("hourly_reports", {"text": report, "image_path": image_path})

    async def run_forever(self, equity: float = 10_000.0) -> None:
        await self._run_with_systemd_watchdog(
            "ai_quant_trader_workers_ready",
            self._hourly_trading_loop(equity, live_news=True),
            self._news_refresh_loop(),
            self._price_wakeup_loop(equity),
            self._order_status_loop(),
        )

    async def run_trading_worker(self, equity: float = 10_000.0) -> None:
        await self._run_with_systemd_watchdog(
            "ai_quant_trading_worker_ready",
            self._hourly_trading_loop(equity, live_news=False),
        )

    async def run_news_worker(self) -> None:
        await self._run_with_systemd_watchdog("ai_quant_news_worker_ready", self._news_refresh_loop())

    async def run_price_monitor_worker(self, equity: float = 10_000.0) -> None:
        await self._run_with_systemd_watchdog(
            "ai_quant_price_monitor_worker_ready",
            self._price_wakeup_loop(equity),
        )

    async def run_order_status_worker(self) -> None:
        await self._run_with_systemd_watchdog(
            "ai_quant_order_status_worker_ready",
            self._order_status_loop(),
        )

    async def _run_with_systemd_watchdog(self, ready_status: str, *coroutines) -> None:
        self.systemd.ready(ready_status)
        tasks = list(coroutines)
        if self.systemd.watchdog_enabled:
            tasks.append(self._systemd_watchdog_loop())
        try:
            await asyncio.gather(*tasks)
        finally:
            self.systemd.stopping("ai_quant_trader_stopping")

    async def _systemd_watchdog_loop(self) -> None:
        interval = self.systemd.watchdog_interval_seconds()
        while True:
            self.systemd.watchdog("ai_quant_trader_alive")
            await asyncio.sleep(interval)

    async def _hourly_trading_loop(self, equity: float, live_news: bool) -> None:
        while True:
            await asyncio.sleep(self._seconds_until_next_report())
            try:
                await self.run_once(equity=equity, live_news=live_news)
                self.heartbeat.ok(
                    "trading_worker",
                    reason="trading_cycle_ok",
                    interval_seconds=3600,
                    details={"live_news": live_news},
                )
            except Exception as exc:  # noqa: BLE001
                self.heartbeat.fail(
                    "trading_worker",
                    reason="trading_cycle_failed",
                    status=HealthStatus.BLOCK,
                    interval_seconds=3600,
                    details={"error_type": type(exc).__name__},
                )
                logger.exception("trading_worker_failed")
                if execution_mode_from_config(self.config) == "live":
                    raise

    async def _news_refresh_loop(self) -> None:
        while True:
            interval = self.config.news.refresh_interval_minutes * 60
            try:
                await self.collect_news_once(notify=True)
                self.heartbeat.ok("news_worker", reason="news_refresh_ok", interval_seconds=interval)
            except Exception:  # noqa: BLE001
                self.heartbeat.fail(
                    "news_worker",
                    reason="news_refresh_failed",
                    status=HealthStatus.WARN,
                    interval_seconds=interval,
                )
                logger.exception("news_refresh_worker_failed")
            await asyncio.sleep(interval)

    async def _price_wakeup_loop(self, equity: float) -> None:
        while True:
            await self.reload_runtime_config()
            interval = self.config.runtime.price_monitor_interval_seconds
            failures: list[str] = []
            for symbol_cfg in self.config.symbols:
                if not self.state.should_report(symbol_cfg.symbol):
                    continue
                try:
                    await self._enforce_fixed_atr_stop_once(symbol_cfg.symbol, symbol_cfg.timeframe)
                    candles_1m = await self.market.fetch_ohlcv(symbol_cfg.symbol, "1m", limit=90, source="auto", closed_only=False)
                    event = self.price_monitor.evaluate(symbol_cfg.symbol, candles_1m)
                    if event and self._wakeup_allowed(symbol_cfg.symbol):
                        self._price_wakeup_cooldown[symbol_cfg.symbol] = datetime.now(UTC)
                        await self._handle_price_wakeup(event, equity)
                except Exception:  # noqa: BLE001
                    failures.append(symbol_cfg.symbol)
                    logger.exception("price_monitor_worker_failed symbol=%s", symbol_cfg.symbol)
                    self.heartbeat.fail(
                        "price_monitor_worker",
                        reason="price_monitor_failed",
                        status=HealthStatus.BLOCK if execution_mode_from_config(self.config) == "live" else HealthStatus.WARN,
                        interval_seconds=interval,
                        details={"symbol": symbol_cfg.symbol},
                    )
                    if execution_mode_from_config(self.config) == "live":
                        raise
            if not failures:
                self.heartbeat.ok(
                    "price_monitor_worker",
                    reason="price_monitor_ok",
                    interval_seconds=interval,
                    details={"symbols": [item.symbol for item in self.config.symbols if self.state.should_report(item.symbol)]},
                )
            await asyncio.sleep(interval)

    async def _order_status_loop(self) -> None:
        while True:
            await self.reload_runtime_config()
            interval = self.config.runtime.price_monitor_interval_seconds
            try:
                symbols = [symbol.symbol for symbol in self.config.symbols]
                safety = await self._refresh_reconciliation_and_order_status_once(symbols)
                latest_order = self.store.fetch_latest("order_lifecycle")
                self.heartbeat.ok(
                    "order_status_worker",
                    reason="order_status_and_reconciliation_refresh_ok",
                    interval_seconds=interval,
                    details={
                        "symbols": symbols,
                        "exchange_safety": safety.status.value,
                        "latest_order_lifecycle_id": latest_order.get("id") if latest_order else None,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                self.heartbeat.fail(
                    "order_status_worker",
                    reason="order_status_refresh_failed",
                    status=HealthStatus.BLOCK if execution_mode_from_config(self.config) == "live" else HealthStatus.WARN,
                    interval_seconds=interval,
                    details={"error_type": type(exc).__name__},
                )
                logger.exception("order_status_worker_failed")
                if execution_mode_from_config(self.config) == "live":
                    raise
            await asyncio.sleep(interval)

    async def _refresh_reconciliation_and_order_status_once(self, symbols: list[str]):
        """Refresh private exchange truth used by live readiness.

        The order-status worker is the only high-frequency private worker that
        is always expected in live deployments. It must refresh both order
        lifecycle state and account reconciliation; otherwise live readiness can
        become stale between hourly trading cycles.
        """
        state = await self._refresh_exchange_safety(symbols)
        if execution_mode_from_config(self.config) != "live":
            await self._refresh_order_status_once(symbols)
        return state

    def _wakeup_allowed(self, symbol: str) -> bool:
        last = self._price_wakeup_cooldown.get(symbol)
        return last is None or datetime.now(UTC) - last >= timedelta(minutes=10)

    async def _handle_price_wakeup(self, event, equity: float) -> None:
        if not event.symbol:
            return
        symbol_cfg = next((item for item in self.config.symbols if item.symbol == event.symbol), None)
        if symbol_cfg is None:
            return
        news_digest = await self._news_for_trading_cycle(live_news=False)
        positions = await self._fetch_positions([event.symbol])
        candles = await self.market.fetch_ohlcv(event.symbol, symbol_cfg.timeframe)
        position = positions[0]
        position.mark_price = float(candles["close"].iloc[-1])
        signal = self._generate_local_signal(event.symbol, symbol_cfg.timeframe, candles, position, equity)
        signal = self._attach_major_news_context(signal, self.wakeup_engine.events_from_news(news_digest))
        orderflow_summaries = await self.orderflow_client.fetch_summaries(event.symbol)
        aggregated = self.orderflow_aggregator.aggregate(event.symbol, orderflow_summaries)
        zone = self.dense_zone.calculate(event.symbol, candles)
        pattern = self.patterns.detect(event.symbol, candles)
        regime_pattern = self.regime_patterns.analyze(event.symbol, candles, zone, pattern)
        signal = self.regime_patterns.enrich_signal(signal, regime_pattern)
        ai = await self._analyze_with_deepseek_budget(
            "price_wakeup",
            signal,
            aggregated,
            zone,
            pattern,
            news_digest,
            regime_pattern,
        )
        drift = self.ai_drift.evaluate(event.symbol, ai)
        data_health = self.data_health.evaluate_symbol(
            symbol=event.symbol,
            timeframe=symbol_cfg.timeframe,
            candles=candles,
            news=news_digest,
            orderflow=aggregated,
        )
        risk = self.risk.evaluate(signal, ai, equity, positions)
        payload = {
            "event": event.model_dump(mode="json"),
            "signal": signal.model_dump(mode="json"),
            "ai": ai.model_dump(mode="json"),
            "risk": risk.model_dump(mode="json"),
            "data_health": data_health.model_dump(mode="json"),
            "ai_drift": drift.model_dump(mode="json"),
        }
        self.store.insert("data_health", data_health, event.symbol)
        self.store.insert("ai_drift_checks", drift, event.symbol)
        self.store.insert("ai_decisions", payload, event.symbol)

    async def collect_news_once(self, notify: bool = False):
        await self.reload_runtime_config()
        self.news.max_age = timedelta(hours=self.config.news.max_age_hours)
        digest = await self.news.collect()
        digest = self.news_memory.update(digest)
        digest = self.daily_news.update(digest)
        digest = self._enrich_digest_with_recent_news_context(digest)
        self.store.insert("news_summaries", digest.model_dump(mode="json"))
        if notify:
            try:
                await self._handle_news_risk_reviews(digest)
            except Exception:  # noqa: BLE001
                logger.exception("major_news_risk_review_failed")
        if notify:
            report = self.report_builder.news_report(digest, major_only=self.state.major_news_only)
            if report:
                logger.info("news_report_ready", extra={"items": len(digest.items)})
        return digest

    async def _handle_news_risk_reviews(self, digest: NewsDigest) -> None:
        events = [
            event for event in self.wakeup_engine.events_from_news(digest)
            if self._mark_news_risk_review_event(event)
        ]
        if not events:
            return
        symbols = await self._news_risk_review_symbols()
        if not symbols:
            return
        for event in events:
            for symbol_cfg in [item for item in self.config.symbols if item.symbol in symbols]:
                try:
                    await self._review_major_news_for_symbol(event, symbol_cfg.symbol, symbol_cfg.timeframe)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "major_news_symbol_review_failed",
                        extra={"symbol": symbol_cfg.symbol, "event_type": event.event_type},
                    )

    def _mark_news_risk_review_event(self, event) -> bool:
        key = self._news_risk_event_key(event)
        if key in self._news_risk_review_seen:
            return False
        for row in self.store.fetch_payloads("news_risk_reviews", limit=500):
            if (row.get("payload") or {}).get("event_key") == key:
                self._news_risk_review_seen.add(key)
                return False
        self._news_risk_review_seen.add(key)
        return True

    def _news_risk_event_key(self, event) -> str:
        return "|".join(
            [
                str(event.source or ""),
                str((event.raw or {}).get("published_at") or ""),
                str(event.title or ""),
            ]
        )

    async def _news_risk_review_symbols(self) -> set[str]:
        symbols = {
            item.symbol for item in self.config.symbols
            if self.state.should_report(item.symbol) or self.state.can_open(item.symbol)
        }
        try:
            positions = await self._fetch_positions([item.symbol for item in self.config.symbols])
        except Exception as exc:  # noqa: BLE001
            logger.warning("news_risk_review_position_fetch_failed", extra={"error": type(exc).__name__})
            positions = []
        symbols.update(position.symbol for position in positions if position.side != Side.FLAT and abs(position.qty) > 0)
        return symbols

    async def _review_major_news_for_symbol(self, event, symbol: str, timeframe: str) -> None:
        candles = await self.market.fetch_ohlcv(symbol, timeframe)
        position = await self._current_position_for_symbol(symbol)
        position.mark_price = float(candles["close"].iloc[-1])
        signal = self._generate_local_signal(symbol, timeframe, candles, position, equity=0.0)
        review_signal = signal.model_copy(
            update={
                "action": SignalAction.HOLD,
                "suggested_qty": 0.0,
                "technical_evidence": {
                    **signal.technical_evidence,
                    "major_news_context": True,
                    "news_risk_review": True,
                    "original_strategy_action": signal.action.value,
                    "major_news_severity": event.severity.value,
                    "major_news_title": event.title,
                    "major_news_source": event.source,
                },
            }
        )
        if not self._should_call_deepseek_for_signal(signal, position):
            self._record_skipped_major_news_review(event, review_signal, symbol, reason="no_signal_no_position")
            return
        orderflow_summaries = await self.orderflow_client.fetch_summaries(symbol)
        aggregated = self.orderflow_aggregator.aggregate(symbol, orderflow_summaries)
        zone = self.dense_zone.calculate(symbol, candles)
        pattern = self.patterns.detect(symbol, candles)
        regime_pattern = self.regime_patterns.analyze(symbol, candles, zone, pattern)
        review_signal = self.regime_patterns.enrich_signal(review_signal, regime_pattern)
        event_digest = NewsDigest(
            items=[],
            macro_risk_level="high",
            crypto_sentiment=aggregated.alignment_hint,
            summary=f"{event.title}\n{event.summary}\n{self.daily_news.context_summary(limit=30)}\n{self.news_memory.context_summary(days=2, limit=20)}",
            warnings=["major_news_risk_review_only_no_order"],
        )
        ai = await self._analyze_with_deepseek_budget(
            "major_news_risk_review",
            review_signal,
            aggregated,
            zone,
            pattern,
            event_digest,
            regime_pattern,
            event_key=self._news_risk_event_key(event),
        )
        risk = self.risk.evaluate(review_signal, ai, equity=0.0, positions=[])
        payload = {
            "review_type": "major_news_risk_review",
            "event_key": self._news_risk_event_key(event),
            "no_order_submitted": True,
            "event": event.model_dump(mode="json"),
            "signal": review_signal.model_dump(mode="json"),
            "ai": ai.model_dump(mode="json"),
            "risk": risk.model_dump(mode="json"),
        }
        self.store.insert("news_risk_reviews", payload, symbol)
        self.store.insert("ai_decisions", payload, symbol)

    async def _current_position_for_symbol(self, symbol: str) -> PositionSnapshot:
        try:
            positions = await self._fetch_positions([symbol])
        except Exception as exc:  # noqa: BLE001
            logger.warning("news_risk_review_position_fetch_failed", extra={"symbol": symbol, "error": type(exc).__name__})
            return PositionSnapshot(symbol=symbol, side=Side.FLAT, qty=0.0, mark_price=0.0)
        for position in positions:
            if position.symbol == symbol:
                return position
        return PositionSnapshot(symbol=symbol, side=Side.FLAT, qty=0.0, mark_price=0.0)

    def _has_open_position(self, position: PositionSnapshot) -> bool:
        return position.side != Side.FLAT and abs(float(position.qty or 0.0)) > 0

    def _should_call_deepseek_for_signal(self, signal: StrategySignal, position: PositionSnapshot) -> bool:
        return signal.action != SignalAction.HOLD or self._has_open_position(position)

    def _record_skipped_major_news_review(
        self,
        event,
        signal: StrategySignal,
        symbol: str,
        *,
        reason: str,
    ) -> None:
        event_key = self._news_risk_event_key(event)
        self.deepseek_budget.record_skipped(
            symbol=symbol,
            call_type="major_news_risk_review",
            reason=reason,
            event_key=event_key,
        )
        payload = {
            "review_type": "major_news_risk_review",
            "event_key": event_key,
            "status": "skipped",
            "skip_reason": reason,
            "deepseek_called": False,
            "no_order_submitted": True,
            "event": event.model_dump(mode="json"),
            "signal": signal.model_dump(mode="json"),
            "ai": None,
            "risk": {
                "allowed": False,
                "reason": "major_news_without_strategy_signal",
                "warnings": ["deepseek_prefilter_skipped_no_signal_no_position"],
            },
        }
        self.store.insert("news_risk_reviews", payload, symbol)

    async def _news_for_trading_cycle(self, live_news: bool) -> NewsDigest:
        if live_news:
            try:
                return await self.collect_news_once(notify=False)
            except Exception as exc:  # noqa: BLE001
                logger.exception("news module unavailable; using cache or degraded inputs")
                cached = self._latest_cached_news_digest()
                if cached:
                    cached.warnings.append(f"news_service_unavailable_using_cache:{type(exc).__name__}")
                    return cached
                return NewsDigest(
                    summary="News service unavailable; trading cycle uses technical, orderflow, and risk inputs only.",
                    warnings=[f"news_service_unavailable:{type(exc).__name__}"],
                )
        cached = self._latest_cached_news_digest()
        if cached:
            return self._enrich_digest_with_recent_news_context(cached)
        return NewsDigest(
            summary="No cached news digest available; trading cycle runs with no news input.",
            warnings=["news_cache_empty_trading_worker_degraded"],
        )

    def _enrich_digest_with_recent_news_context(self, digest: NewsDigest) -> NewsDigest:
        digest = self.daily_news.enrich_digest(digest)
        context = self.news_memory.context_summary(days=2, limit=20)
        if not context or context in digest.summary:
            return digest
        summary = f"{context}\n\n当前新闻窗口：{digest.summary}" if digest.summary else context
        warnings = [*digest.warnings, "news_context_48h_attached"]
        return digest.model_copy(update={"summary": summary, "warnings": warnings})

    def _attach_major_news_context(self, signal: StrategySignal, events: list) -> StrategySignal:
        if not events:
            return signal
        severities = [str(getattr(event.severity, "value", event.severity)) for event in events]
        highest = "critical" if "critical" in severities else max(severities)
        evidence = {
            **signal.technical_evidence,
            "major_news_context": True,
            "major_news_event_count": len(events),
            "major_news_highest_severity": highest,
            "major_news_titles": " | ".join(str(event.title)[:80] for event in events[:3]),
        }
        return signal.model_copy(update={"technical_evidence": evidence})

    def _latest_cached_news_digest(self) -> NewsDigest | None:
        rows = self.store.fetch_payloads("news_summaries", limit=1)
        if not rows:
            return None
        payload = rows[0].get("payload") or {}
        try:
            return NewsDigest.model_validate(payload)
        except Exception:  # noqa: BLE001
            return None

    async def _effective_equity(self, configured_equity: float) -> float:
        if execution_mode_from_config(self.config) == "mock":
            return configured_equity
        try:
            balance = await self.execution.fetch_balance_summary()
        except Exception as exc:  # noqa: BLE001
            state = self.exchange_safety.mark_failure("live_balance_fetch_failed", [type(exc).__name__])
            self.store.insert("exchange_health", state.model_dump(mode="json"))
            logger.exception("live_balance_fetch_failed_blocking_trading_cycle")
            raise RuntimeError("live_balance_fetch_failed_blocking_trading_cycle") from exc
        if not balance.get("ok"):
            state = self.exchange_safety.mark_failure("live_balance_unavailable", ["balance_ok_false"])
            self.store.insert("exchange_health", state.model_dump(mode="json"))
            raise RuntimeError("live_balance_unavailable_blocking_trading_cycle")
        equity = float(balance.get("usdt_total") or balance.get("usdt_free") or 0.0)
        if equity <= 0:
            state = self.exchange_safety.mark_failure("live_equity_zero", ["equity_zero"])
            self.store.insert("exchange_health", state.model_dump(mode="json"))
            raise RuntimeError("live_equity_zero_blocking_trading_cycle")
        return equity

    async def reload_runtime_config(self) -> None:
        old_mode = execution_mode_from_config(self.config)
        self.config = load_config(self.config_path)
        self.state = self.control.load_state([symbol.symbol for symbol in self.config.symbols])
        self.risk.config = self.config.risk
        self.risk.state = self.state
        self.deepseek_budget = DeepSeekBudgetGuard.from_config(self.store, self.config.ai)
        self._refresh_symbol_strategies()
        self.news.rss_sources = self.config.news.rss_sources
        self.news.scrape_sources = self.config.news.scrape_sources
        self.news.max_age = timedelta(hours=self.config.news.max_age_hours)
        self.data_health.stale_data_seconds = self.config.risk.stale_data_seconds
        self.data_health.news_max_age_hours = self.config.news.max_age_hours
        if old_mode != execution_mode_from_config(self.config):
            await self.execution.close()
            self.execution = create_exchange_gateway(self.config, account_slot=TREND_ACCOUNT_SLOT)
        self.orderflow_client.live_public_data = execution_mode_from_config(self.config) == "live"

    async def _refresh_exchange_safety(self, symbols: list[str]):
        live = execution_mode_from_config(self.config) == "live"
        if not live:
            state = self.exchange_safety.mark_success("mock_gateway_no_private_reconciliation_required")
            self.store.insert("exchange_health", state.model_dump(mode="json"))
            return state
        await run_read_only_reconciliation(
            gateway=self.execution,
            store=self.store,
            symbols=symbols,
            trend_state=self.trend_state,
            monitor=self.exchange_safety,
            stale_after_seconds=self.config.risk.stale_data_seconds,
            live=True,
        )
        await self._refresh_order_status_once(symbols)
        return self.exchange_safety.state

    async def _refresh_order_status_once(self, symbols: list[str]):
        try:
            return await self.order_lifecycle.refresh_recent_orders(
                self.execution,
                symbols=symbols,
                gateway_mode=execution_mode_from_config(self.config),
            )
        except Exception as exc:  # noqa: BLE001
            if execution_mode_from_config(self.config) == "live":
                state = self.exchange_safety.mark_failure("live_order_status_refresh_failed", [type(exc).__name__])
                self.store.insert("exchange_health", state.model_dump(mode="json"))
            logger.exception("order_status_refresh_failed")
            raise RuntimeError("order_status_refresh_failed") from exc

    async def _fetch_positions(self, symbols: list[str]) -> list[PositionSnapshot]:
        try:
            positions = await self.execution.fetch_positions(symbols)
            if execution_mode_from_config(self.config) == "live":
                self.exchange_safety.mark_success("live_positions_fetch_ok")
        except Exception as exc:  # noqa: BLE001
            if execution_mode_from_config(self.config) == "live":
                state = self.exchange_safety.mark_failure("live_position_fetch_failed", [type(exc).__name__])
                self.store.insert("exchange_health", state.model_dump(mode="json"))
                logger.exception("live_position_fetch_failed_blocking_trading_cycle")
                raise RuntimeError("live_position_fetch_failed_blocking_trading_cycle") from exc
            logger.warning("Failed to fetch exchange positions; using local flat snapshot. error=%s", exc)
            positions = [PositionSnapshot(symbol=symbol, side=Side.FLAT, qty=0.0, mark_price=0.0) for symbol in symbols]
        return positions

    async def _enforce_fixed_atr_stop_once(self, symbol: str, timeframe: str):
        positions = await self._fetch_positions([symbol])
        position = positions[0] if positions else PositionSnapshot(symbol=symbol, side=Side.FLAT, qty=0.0, mark_price=0.0)
        if position.side == Side.FLAT or abs(position.qty) <= 0:
            return None
        candles_1m = await self.market.fetch_ohlcv(symbol, "1m", limit=5, source="auto", closed_only=False)
        signal = self._fixed_atr_stop_signal(symbol, timeframe, candles_1m, position)
        if signal is None:
            return None
        order = await self.execution.close_position(symbol, reason="software_fixed_atr_stop")
        if order:
            await self._cancel_native_stop_order(symbol)
            self.trend_state.clear(symbol)
            self.store.insert("orders", order, symbol)
        return order

    async def minimum_order_checks_for_active_symbols(self) -> list[dict]:
        await self.reload_runtime_config()
        active_symbols = [s for s in self.config.symbols if self.state.should_report(s.symbol)]
        rows: list[dict] = []
        for symbol_cfg in active_symbols:
            candles = await self.market.fetch_ohlcv(symbol_cfg.symbol, symbol_cfg.timeframe)
            price = float(candles["close"].iloc[-1])
            rows.append(await self.execution.minimum_order_check(symbol_cfg.symbol, price))
        return rows

    def _refresh_symbol_strategies(self) -> None:
        raw_config = self.control.read_config()
        self.trend_strategies = {
            symbol_cfg.symbol: TrendStrategy(self.control.trend_config_for_symbol(raw_config, symbol_cfg.symbol))
            for symbol_cfg in self.config.symbols
        }

    def _generate_local_signal(
        self,
        symbol: str,
        timeframe: str,
        candles,
        position: PositionSnapshot,
        equity: float,
    ):
        active = get_active_strategy()
        if active and active.get("live_enabled", True) and symbol in set(active.get("symbols") or []):
            try:
                return generate_custom_signal(active["id"], candles, position, symbol, timeframe, equity)
            except StrategyCodeError as exc:
                logger.error("custom_strategy_failed_fallback_to_trend: %s", exc)
        signal = self.trend_strategies[symbol].generate_signal(
            symbol,
            timeframe,
            candles,
            position,
            equity,
            ai_multiplier=1.0,
            leverage=float(self.config.risk.max_total_leverage),
        )
        stop_signal = self._fixed_atr_stop_signal(symbol, timeframe, candles, position)
        return stop_signal or signal

    def _fixed_atr_stop_signal(self, symbol: str, timeframe: str, candles, position: PositionSnapshot):
        state = self.trend_state.get(symbol)
        if state is None or position.side == Side.FLAT or abs(position.qty) <= 0 or len(candles) == 0:
            return None
        last = candles.iloc[-1]
        current_price = float(last["close"])
        if position.side == Side.LONG and state.side == Side.LONG and float(last["low"]) <= state.stop_loss_price:
            return self.trend_strategies[symbol].generate_exit_signal(
                symbol,
                timeframe,
                current_price,
                SignalAction.EXIT_LONG,
                {
                    "reason": "atr_stop",
                    "stop_loss_price": state.stop_loss_price,
                    "entry_price": state.entry_price,
                    "atr": state.atr_value,
                    "atr_stop_multiple": state.atr_stop_multiple,
                },
            )
        if position.side == Side.SHORT and state.side == Side.SHORT and float(last["high"]) >= state.stop_loss_price:
            return self.trend_strategies[symbol].generate_exit_signal(
                symbol,
                timeframe,
                current_price,
                SignalAction.EXIT_SHORT,
                {
                    "reason": "atr_stop",
                    "stop_loss_price": state.stop_loss_price,
                    "entry_price": state.entry_price,
                    "atr": state.atr_value,
                    "atr_stop_multiple": state.atr_stop_multiple,
                },
            )
        return None

    def _record_trend_entry_state(self, symbol: str, signal, entry_price: float) -> TrendPositionState | None:
        atr_value = float(signal.technical_evidence.get("entry_stop_atr") or signal.technical_evidence.get("atr") or 0.0)
        atr_stop_multiple = float(signal.technical_evidence.get("atr_stop_multiple") or self.config.strategy.trend.atr_stop_multiple)
        if atr_value <= 0:
            logger.warning("trend_stop_state_not_recorded", extra={"symbol": symbol, "reason": "missing_atr"})
            return None
        side = Side.LONG if signal.action == SignalAction.LONG else Side.SHORT
        return self.trend_state.record_entry(symbol, side, float(entry_price), atr_value, atr_stop_multiple)

    async def _place_native_stop_loss(
        self,
        symbol: str,
        signal,
        state: TrendPositionState,
        amount: float,
    ) -> None:
        stop_side = "sell" if state.side == Side.LONG.value else "buy"
        try:
            request = OrderRequest(
                symbol=symbol,
                side=stop_side,
                amount=abs(float(amount)),
                reduce_only=True,
                client_order_id=f"aiq_stop_{uuid.uuid4().hex[:17]}",
                reason="native_fixed_atr_stop",
            )
            order = await self.order_lifecycle.submit_stop_loss_order(
                self.execution,
                request,
                state.stop_loss_price,
            )
            self.store.insert("orders", order, symbol)
            if order.exchange_order_id:
                self.trend_state.set_native_stop_order_id(symbol, order.exchange_order_id)
        except (OrderRejected, OrderSubmissionUncertain) as exc:
            if execution_mode_from_config(self.config) == "live":
                safety = self.exchange_safety.mark_failure(f"native_stop_submit_{type(exc).__name__.lower()}", [symbol])
                self.store.insert("exchange_health", safety.model_dump(mode="json"))
                raise RuntimeError("native_stop_loss_state_unknown_manual_gate_required") from exc
            logger.exception("native_stop_loss_submit_failed_emergency_close", extra={"symbol": symbol})
            close_order = await self.execution.close_position(symbol, reason="native_stop_submit_failed_emergency_close")
            if close_order:
                self.store.insert("orders", close_order, symbol)
            self.trend_state.clear(symbol)
            raise RuntimeError("native_stop_loss_submit_failed") from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("native_stop_loss_submit_failed_emergency_close", extra={"symbol": symbol})
            close_order = await self.execution.close_position(symbol, reason="native_stop_submit_failed_emergency_close")
            if close_order:
                self.store.insert("orders", close_order, symbol)
            self.trend_state.clear(symbol)
            raise RuntimeError("native_stop_loss_submit_failed") from exc

    async def _cancel_native_stop_order(self, symbol: str) -> None:
        state = self.trend_state.get(symbol)
        if state is None or not state.native_stop_order_id:
            return
        try:
            await self.order_lifecycle.cancel_order(
                self.execution,
                symbol=symbol,
                order_id=state.native_stop_order_id,
                client_order_id=f"aiq_cancel_{uuid.uuid4().hex[:16]}",
                trigger=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "native_stop_loss_cancel_failed",
                extra={"symbol": symbol, "order_id": state.native_stop_order_id, "error": type(exc).__name__},
            )

    def _opposite_position(self, signal, positions: list[PositionSnapshot]) -> PositionSnapshot | None:
        expected_side = Side.LONG if signal.action == SignalAction.LONG else Side.SHORT
        opposite_side = Side.SHORT if expected_side == Side.LONG else Side.LONG
        for position in positions:
            if position.symbol == signal.symbol and position.side == opposite_side and abs(position.qty) > 0:
                return position
        return None

    def _same_direction_trend_state_exists(self, symbol: str, signal) -> bool:
        state = self.trend_state.get(symbol)
        if state is None:
            return False
        expected_side = Side.LONG if signal.action == SignalAction.LONG else Side.SHORT
        return state.side == expected_side.value

    def _ai_enabled_for_symbol(self, symbol: str) -> bool:
        configured = self.config.ai.ai_enabled_symbols
        return symbol in configured if configured else True

    async def _analyze_with_deepseek_budget(
        self,
        call_type: str,
        signal: StrategySignal,
        orderflow: AggregatedOrderflow,
        dense_zone: DenseZone,
        pattern: PatternCandidate,
        news: NewsDigest,
        regime_pattern: RegimePattern | None = None,
        *,
        event_key: str | None = None,
    ) -> AiDecision:
        reservation = self.deepseek_budget.reserve(symbol=signal.symbol, call_type=call_type, event_key=event_key)
        if not reservation.allowed:
            reason = f"deepseek_budget_blocked:{reservation.reason}"
            logger.warning(
                "deepseek_budget_blocked",
                extra={"symbol": signal.symbol, "call_type": call_type, "reason": reservation.reason},
            )
            return self.brain.local_fallback_decision(signal, orderflow, dense_zone, pattern, news, reason, regime_pattern)
        try:
            decision = await self.brain.analyze_symbol(signal, orderflow, dense_zone, pattern, news, regime_pattern)
        except Exception as exc:  # noqa: BLE001
            self.deepseek_budget.record_failure(
                reservation.row_id,
                reason="deepseek_exception",
                error_type=type(exc).__name__,
            )
            raise
        error_reasons = [
            str(reason)
            for reason in decision.reason_codes
            if str(reason).startswith(("deepseek_error:", "missing_deepseek_api_key"))
        ]
        if error_reasons:
            self.deepseek_budget.record_failure(
                reservation.row_id,
                reason=error_reasons[0],
                error_type=error_reasons[0].split(":", 1)[-1],
            )
        else:
            self.deepseek_budget.record_success(reservation.row_id)
        return decision

    def _seconds_until_next_report(self) -> float:
        now = datetime.now(UTC)
        minute = self.config.runtime.hourly_report_minute
        next_run = now.replace(minute=minute, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(hours=1)
        return max((next_run - now).total_seconds(), 60.0)

    async def close(self) -> None:
        await self.market.close()
        await self.orderflow_client.close()
        await self.execution.close()
        self.store.close()


async def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="AI quant trading system")
    parser.add_argument("--once", action="store_true", help="Run one analysis cycle and exit")
    parser.add_argument("--news-worker", action="store_true", help="Run only the decoupled news collection worker")
    parser.add_argument("--trading-worker", action="store_true", help="Run only the decoupled trading worker and read cached news")
    parser.add_argument("--price-monitor-worker", action="store_true", help="Run only realtime price wakeup monitoring")
    parser.add_argument("--order-status-worker", action="store_true", help="Run only exchange order status polling and heartbeat")
    parser.add_argument("--equity", type=float, default=10_000.0, help="Estimated account equity for dry-run risk checks")
    args = parser.parse_args()
    app = TradingApp()
    try:
        if args.once:
            await app.run_once(equity=args.equity)
        elif args.news_worker:
            await app.run_news_worker()
        elif args.trading_worker:
            await app.run_trading_worker(equity=args.equity)
        elif args.price_monitor_worker:
            await app.run_price_monitor_worker(equity=args.equity)
        elif args.order_status_worker:
            await app.run_order_status_worker()
        else:
            await app.run_forever(equity=args.equity)
    finally:
        await app.close()


if __name__ == "__main__":
    asyncio.run(main())
