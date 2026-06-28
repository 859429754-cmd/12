from __future__ import annotations

import argparse
import asyncio
import logging
import os
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
    AiDriftReport,
    Alignment,
    DenseZone,
    FollowerAccountConfig,
    HealthStatus,
    MarketLeaderContext,
    NewsDigest,
    NewsDirection,
    OrderRequest,
    PatternCandidate,
    PositionReviewDecision,
    PositionSnapshot,
    RegimePattern,
    RiskDecision,
    Side,
    SignalAction,
    StrategySignal,
)
from ai_quant_trader.data.market import MarketDataClient
from ai_quant_trader.data.news import NewsCollector
from ai_quant_trader.data.news_context import MarketNewsContextBuilder
from ai_quant_trader.data.news_memory import DailyNewsFlashStore, NewsMemoryStore
from ai_quant_trader.data.orderflow import MultiExchangeOrderflowClient
from ai_quant_trader.execution.gateway import create_exchange_gateway, execution_mode_from_config
from ai_quant_trader.execution.lifecycle import OrderLifecycleManager, OrderRejected, OrderSubmissionUncertain
from ai_quant_trader.execution.position_stop import PositionStopManager
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
from ai_quant_trader.research.live_factor_archive import build_live_factor_snapshot
from ai_quant_trader.risk.manager import RiskManager
from ai_quant_trader.risk.position_review import PositionReviewEngine
from ai_quant_trader.storage.sqlite import SQLiteStore
from ai_quant_trader.strategy.lab import StrategyCodeError, generate_custom_signal, get_active_strategy
from ai_quant_trader.strategy.trend import TrendStrategy
from ai_quant_trader.strategy.trend_state import TrendPositionState, TrendStateStore

logger = logging.getLogger(__name__)

TREND_ACCOUNT_SLOT = "trend"
FOLLOWER_ACCOUNT_SLOT = "follower"


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
        self.news_context = MarketNewsContextBuilder(self.store)
        self.brain = DeepSeekBrain(
            base_url=self.config.ai.base_url,
            model=self.config.ai.decision_model,
            store=self.store,
        )
        self.brain.backup_api_key = os.getenv(self.config.ai.backup_api_key_env)
        self.deepseek_budget = DeepSeekBudgetGuard.from_config(self.store, self.config.ai)
        self.risk = RiskManager(self.config.risk, self.state)
        self.position_review = PositionReviewEngine(self.config.risk.position_review)
        self.execution = create_exchange_gateway(self.config, account_slot=TREND_ACCOUNT_SLOT)
        self.exchange_safety = ExchangeSafetyMonitor(self.config.risk.stale_data_seconds)
        self.order_lifecycle = OrderLifecycleManager(
            self.store,
            gateway_mode=execution_mode_from_config(self.config),
            account_slot=TREND_ACCOUNT_SLOT,
        )
        self.follower_execution = create_exchange_gateway(self.config, account_slot=FOLLOWER_ACCOUNT_SLOT)
        self.follower_order_lifecycle = OrderLifecycleManager(
            self.store,
            gateway_mode=execution_mode_from_config(self.config),
            account_slot=FOLLOWER_ACCOUNT_SLOT,
        )
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
        self.follower_trend_state = TrendStateStore("data/state_trend_follower.json")
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
            should_call_deepseek = self._ai_enabled_for_symbol(symbol_cfg.symbol) and self._should_call_deepseek_for_signal(signal, position)
            market_leader_context = (
                await self._market_leader_context(symbol_cfg.symbol, symbol_cfg.timeframe, signal, candles)
                if should_call_deepseek
                else None
            )
            if should_call_deepseek:
                ai = await self._analyze_with_deepseek_budget(
                    "trading_cycle",
                    signal,
                    aggregated,
                    zone,
                    pattern,
                    news_digest,
                    regime_pattern,
                    market_leader_context=market_leader_context,
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
                    market_leader_context,
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
                    market_leader_context,
                )
            drift = self._evaluate_ai_drift_for_signal(symbol_cfg.symbol, signal, position, ai)
            data_health = self.data_health.evaluate_symbol(
                symbol=symbol_cfg.symbol,
                timeframe=symbol_cfg.timeframe,
                candles=candles,
                news=news_digest,
                orderflow=aggregated,
            )
            risk = self.risk.evaluate(signal, ai, equity, positions)
            position_review = self._review_open_trend_position(
                signal,
                position,
                ai,
                aggregated,
                zone,
                pattern,
                data_health.status,
            )

            self.store.insert("orderflow_summaries", aggregated, symbol_cfg.symbol)
            self.store.insert("dense_zones", zone, symbol_cfg.symbol)
            self.store.insert("data_health", data_health, symbol_cfg.symbol)
            self.store.insert("ai_drift_checks", drift, symbol_cfg.symbol)
            self.store.insert("ai_decisions", ai, symbol_cfg.symbol)
            if position_review is not None:
                self.store.insert("position_reviews", position_review, symbol_cfg.symbol)
                addon_order = await self._maybe_execute_position_review_addon(
                    symbol_cfg.symbol,
                    signal,
                    position_review,
                    ai,
                )
                if addon_order is not None:
                    positions = await self._fetch_positions(risk_symbols)
            if signal.action != SignalAction.HOLD:
                self.store.insert(
                    "live_factor_snapshots",
                    build_live_factor_snapshot(
                        signal=signal,
                        ai=ai,
                        risk=risk,
                        orderflow=aggregated,
                        source="trading_cycle",
                    ),
                    symbol_cfg.symbol,
                )

            if not risk.allowed and signal.action in {SignalAction.LONG, SignalAction.SHORT}:
                self.store.insert(
                    "order_lifecycle",
                    {
                        "state": "blocked_before_submit",
                        "symbol": symbol_cfg.symbol,
                        "reason": risk.reason,
                        "gateway_mode": execution_mode_from_config(self.config),
                        "signal": signal.model_dump(mode="json"),
                        "ai_decision": ai.model_dump(mode="json"),
                        "risk_decision": risk.model_dump(mode="json"),
                    },
                    symbol_cfg.symbol,
                )

            if risk.allowed and signal.action in {SignalAction.EXIT_LONG, SignalAction.EXIT_SHORT}:
                order = await self.order_lifecycle.close_position(self.execution, symbol_cfg.symbol, reason=risk.reason)
                if order:
                    await self._cancel_native_stop_order(symbol_cfg.symbol)
                    self.trend_state.clear(symbol_cfg.symbol)
                    self.store.insert("orders", order, symbol_cfg.symbol)
                    await self._mirror_exit_to_followers(symbol_cfg.symbol, signal, ai, risk.reason)
                    positions = await self._fetch_positions(risk_symbols)

            if risk.allowed and signal.action in {SignalAction.LONG, SignalAction.SHORT}:
                if not data_health.can_open_new_entries:
                    risk.allowed = False
                    risk.reason = f"data_health_blocks_new_entry:{data_health.status.value}:{data_health.reason}"
                    risk.warnings.append("market/news/orderflow freshness gate blocked this entry")
                    rows.append((signal, ai, aggregated, zone, risk))
                    continue
                if drift.status == HealthStatus.BLOCK:
                    risk.allowed = False
                    risk.reason = f"ai_drift_blocks_new_entry:{drift.reason}"
                    risk.warnings.append("AI output drift gate blocked this entry; wait for the next actionable signal or review manually.")
                    self.store.insert(
                        "order_lifecycle",
                        {
                            "state": "blocked_before_submit",
                            "symbol": symbol_cfg.symbol,
                            "reason": risk.reason,
                            "gateway_mode": execution_mode_from_config(self.config),
                            "risk_state": drift.model_dump(mode="json"),
                        },
                        symbol_cfg.symbol,
                    )
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
                    close_order = await self.order_lifecycle.close_position(
                        self.execution,
                        symbol_cfg.symbol,
                        reason="reverse_signal_close_first",
                    )
                    if close_order is None:
                        logger.warning("reverse_close_skipped_entry", extra={"symbol": symbol_cfg.symbol})
                        rows.append((signal, ai, aggregated, zone, risk))
                        continue
                    await self._cancel_native_stop_order(symbol_cfg.symbol)
                    self.trend_state.clear(symbol_cfg.symbol)
                    self.store.insert("orders", close_order, symbol_cfg.symbol)
                    positions = await self._fetch_positions(risk_symbols)
                entry_request = OrderRequest(
                    symbol=symbol_cfg.symbol,
                    side="buy" if signal.action == SignalAction.LONG else "sell",
                    amount=risk.clipped_qty,
                    reduce_only=False,
                    client_order_id=f"aiq_{uuid.uuid4().hex[:20]}",
                    reason=risk.reason,
                    metadata=self._order_risk_metadata(signal, ai, risk, role="primary"),
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
                    close_order = await self.order_lifecycle.close_position(
                        self.execution,
                        symbol_cfg.symbol,
                        reason="missing_trend_stop_state_emergency_close",
                    )
                    if close_order:
                        self.store.insert("orders", close_order, symbol_cfg.symbol)
                    raise RuntimeError("live_entry_missing_trend_stop_state")
                if entry_state is not None:
                    await self._place_native_stop_loss(symbol_cfg.symbol, signal, entry_state, risk.clipped_qty)
                    await self._mirror_entry_to_followers(symbol_cfg.symbol, signal, ai, risk, order)
                    positions = await self._fetch_positions(risk_symbols)

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
        await self._run_trading_cycle_with_heartbeat(
            equity=equity,
            live_news=live_news,
            heartbeat_reason="trading_startup_cycle_ok",
        )
        while True:
            await asyncio.sleep(self._seconds_until_next_report())
            await self._run_trading_cycle_with_heartbeat(
                equity=equity,
                live_news=live_news,
                heartbeat_reason="trading_cycle_ok",
            )

    async def _run_trading_cycle_with_heartbeat(self, equity: float, live_news: bool, heartbeat_reason: str) -> None:
        try:
            await self.run_once(equity=equity, live_news=live_news)
            self.heartbeat.ok(
                "trading_worker",
                reason=heartbeat_reason,
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
            await asyncio.sleep(interval)

    async def _refresh_reconciliation_and_order_status_once(self, symbols: list[str]):
        """Refresh private exchange truth used by live readiness.

        The order-status worker is the only high-frequency private worker that
        is always expected in live deployments. It must refresh both order
        lifecycle state and account reconciliation; otherwise live readiness can
        become stale between hourly trading cycles.
        """
        await self._refresh_order_status_once(symbols)
        state = await self._refresh_exchange_safety(symbols)
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
        original_signal = self._attach_major_news_context(signal, self.wakeup_engine.events_from_news(news_digest))
        review_signal = original_signal.model_copy(
            update={
                "action": SignalAction.HOLD,
                "suggested_qty": 0.0,
                "signal_strength": 0.0,
                "technical_evidence": {
                    **original_signal.technical_evidence,
                    "price_wakeup_review": True,
                    "review_only_no_order": True,
                    "original_strategy_action": original_signal.action.value,
                    "price_wakeup_event_type": event.event_type,
                    "price_wakeup_event_created_at": event.created_at.isoformat(),
                    "price_wakeup_reason": "price_move_review_does_not_submit_orders",
                },
            }
        )
        orderflow_summaries = await self.orderflow_client.fetch_summaries(event.symbol)
        aggregated = self.orderflow_aggregator.aggregate(event.symbol, orderflow_summaries)
        zone = self.dense_zone.calculate(event.symbol, candles)
        pattern = self.patterns.detect(event.symbol, candles)
        regime_pattern = self.regime_patterns.analyze(event.symbol, candles, zone, pattern)
        review_signal = self.regime_patterns.enrich_signal(review_signal, regime_pattern)
        market_leader_context = await self._market_leader_context(event.symbol, symbol_cfg.timeframe, original_signal, candles)
        if self._should_call_deepseek_for_price_wakeup(event, original_signal, position):
            ai = await self._analyze_with_deepseek_budget(
                "price_wakeup",
                review_signal,
                aggregated,
                zone,
                pattern,
                news_digest,
                regime_pattern,
                market_leader_context=market_leader_context,
            )
        else:
            self.deepseek_budget.record_skipped(
                symbol=event.symbol,
                call_type="price_wakeup",
                reason="local_only_no_position_no_signal",
            )
            ai = self.brain.local_fallback_decision(
                review_signal,
                aggregated,
                zone,
                pattern,
                news_digest,
                "deepseek_skipped:price_wakeup_local_only",
                regime_pattern,
                market_leader_context,
            )
        drift = self.ai_drift.evaluate(event.symbol, ai)
        data_health = self.data_health.evaluate_symbol(
            symbol=event.symbol,
            timeframe=symbol_cfg.timeframe,
            candles=candles,
            news=news_digest,
            orderflow=aggregated,
        )
        risk = self.risk.evaluate(review_signal, ai, equity, positions)
        payload = {
            "review_type": "price_wakeup_review",
            "no_order_submitted": True,
            "event": event.model_dump(mode="json"),
            "original_signal": original_signal.model_dump(mode="json"),
            "signal": review_signal.model_dump(mode="json"),
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
        digest = self.news_context.update_digest(digest)
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
        event_digest = self._enrich_digest_with_recent_news_context(event_digest)
        market_leader_context = await self._market_leader_context(symbol, timeframe, signal, candles)
        ai = await self._analyze_with_deepseek_budget(
            "major_news_risk_review",
            review_signal,
            aggregated,
            zone,
            pattern,
            event_digest,
            regime_pattern,
            market_leader_context=market_leader_context,
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

    def _should_call_deepseek_for_price_wakeup(self, event, signal: StrategySignal, position: PositionSnapshot) -> bool:
        if self._should_call_deepseek_for_signal(signal, position):
            return True
        severity = str(getattr(event, "severity", "") or "").lower()
        if severity.endswith("high") or severity.endswith("critical"):
            return True
        raw = getattr(event, "raw", {}) or {}
        try:
            pct_1m = abs(float(raw.get("pct_1m") or raw.get("move_1m_pct") or 0.0))
            pct_5m = abs(float(raw.get("pct_5m") or raw.get("move_5m_pct") or 0.0))
        except (TypeError, ValueError):
            return False
        threshold = max(float(self.config.runtime.price_wakeup_threshold_pct or 1.0), 0.1)
        return pct_1m >= threshold * 3.0 or pct_5m >= threshold * 4.0

    def _evaluate_ai_drift_for_signal(
        self,
        symbol: str,
        signal: StrategySignal,
        position: PositionSnapshot,
        ai: AiDecision,
    ) -> AiDriftReport:
        if not self._should_call_deepseek_for_signal(signal, position):
            return AiDriftReport(
                symbol=symbol,
                status=HealthStatus.OK,
                reason="ai_drift_skipped_no_signal_no_position",
                sample_size=0,
                latest_confidence=ai.confidence,
                latest_direction=ai.direction,
            )
        return self.ai_drift.evaluate(symbol, ai)

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
        digest = self.news_context.attach_latest_background(digest)
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
        self.position_review.config = self.config.risk.position_review
        self.deepseek_budget = DeepSeekBudgetGuard.from_config(self.store, self.config.ai)
        self.brain.backup_api_key = os.getenv(self.config.ai.backup_api_key_env)
        self._refresh_symbol_strategies()
        self.news.rss_sources = self.config.news.rss_sources
        self.news.scrape_sources = self.config.news.scrape_sources
        self.news.max_age = timedelta(hours=self.config.news.max_age_hours)
        self.data_health.stale_data_seconds = self.config.risk.stale_data_seconds
        self.data_health.news_max_age_hours = self.config.news.max_age_hours
        if old_mode != execution_mode_from_config(self.config):
            await self.execution.close()
            self.execution = create_exchange_gateway(self.config, account_slot=TREND_ACCOUNT_SLOT)
            await self.follower_execution.close()
            self.follower_execution = create_exchange_gateway(self.config, account_slot=FOLLOWER_ACCOUNT_SLOT)
            self.order_lifecycle.gateway_mode = execution_mode_from_config(self.config)
            self.follower_order_lifecycle.gateway_mode = execution_mode_from_config(self.config)
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
            primary_updates = await self.order_lifecycle.refresh_recent_orders(
                self.execution,
                symbols=symbols,
                gateway_mode=execution_mode_from_config(self.config),
            )
            await self._mirror_primary_native_stop_fills_to_followers(primary_updates)
            updates = list(primary_updates)
            if self._active_followers():
                try:
                    updates.extend(
                        await self.follower_order_lifecycle.refresh_recent_orders(
                            self.follower_execution,
                            symbols=symbols,
                            gateway_mode=execution_mode_from_config(self.config),
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("follower_order_status_refresh_failed")
                    self.store.insert(
                        "follower_executions",
                        {
                            "status": "order_status_refresh_failed",
                            "account_slot": FOLLOWER_ACCOUNT_SLOT,
                            "symbols": symbols,
                            "error_type": type(exc).__name__,
                        },
                    )
            return updates
        except Exception as exc:  # noqa: BLE001
            if execution_mode_from_config(self.config) == "live":
                state = self.exchange_safety.mark_failure("live_order_status_refresh_failed", [type(exc).__name__])
                self.store.insert("exchange_health", state.model_dump(mode="json"))
            logger.exception("order_status_refresh_failed")
            raise RuntimeError("order_status_refresh_failed") from exc

    async def _mirror_primary_native_stop_fills_to_followers(self, updates) -> None:
        for event in updates:
            if getattr(event, "account_slot", TREND_ACCOUNT_SLOT) != TREND_ACCOUNT_SLOT:
                continue
            if getattr(event, "order_type", "") != "stop_loss":
                continue
            if str(getattr(event, "status", "")) not in {"filled", "OrderLifecycleStatus.FILLED"}:
                continue
            symbol = str(getattr(event, "symbol", "") or "")
            if not symbol:
                continue
            client_order_id = str(getattr(event, "client_order_id", "") or "")
            if client_order_id and self.store.fetch_latest_payload_by_value(
                "follower_executions",
                "primary_stop_client_order_id",
                client_order_id,
                symbol=symbol,
                limit=500,
            ):
                continue
            side = str(getattr(event, "side", "") or "").lower()
            action = SignalAction.EXIT_LONG if side == "sell" else SignalAction.EXIT_SHORT
            order_payload = getattr(event, "order", None) or {}
            price = float(order_payload.get("price") or order_payload.get("stop_price") or 0.0)
            signal = StrategySignal(
                symbol=symbol,
                timeframe="1h",
                action=action,
                current_price=price,
                suggested_qty=0.0,
                signal_strength=1.0,
                technical_evidence={
                    "strategy_allowed": "trend",
                    "reason": "primary_native_stop_filled",
                    "primary_stop_client_order_id": client_order_id,
                    "primary_stop_exchange_order_id": getattr(event, "exchange_order_id", None),
                },
            )
            ai = AiDecision(
                symbol=symbol,
                regime="trend",
                direction=Side.FLAT,
                confidence=1.0,
                multiplier=1.0,
                news_alignment="neutral",
                orderflow_alignment="neutral",
                dense_zone_position="primary_native_stop_filled",
                pattern_type="stop_loss_exit",
                trend_confirmation_score=1.0,
                range_risk_score=0.0,
                news_risk_score=0.0,
                orderflow_confirmation_score=0.0,
                dense_zone_breakout_score=0.0,
                action_suggestion="close",
                veto_action="allow",
                brief_reason="账户1 Gate 原生止损成交，账户2按账户1退出动作同步平仓。",
                reason_codes=["primary_native_stop_filled", "follower_exit_mirror"],
            )
            await self._mirror_exit_to_followers(symbol, signal, ai, "primary_native_stop_filled")
            self.trend_state.clear(symbol)
            if client_order_id:
                self.store.insert(
                    "follower_executions",
                    {
                        "status": "primary_native_stop_fill_processed",
                        "account_slot": FOLLOWER_ACCOUNT_SLOT,
                        "symbol": symbol,
                        "primary_stop_client_order_id": client_order_id,
                    },
                    symbol,
                )

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
        signal = self._fixed_atr_stop_signal(
            symbol,
            timeframe,
            candles_1m,
            position,
            trigger_price=position.mark_price,
            use_candle_extremes=False,
        )
        if signal is None:
            return None
        order = await self.order_lifecycle.close_position(self.execution, symbol, reason="software_fixed_atr_stop")
        if order:
            await self._cancel_native_stop_order(symbol)
            self.trend_state.clear(symbol)
            self.store.insert("orders", order, symbol)
            await self._mirror_exit_to_followers(
                symbol,
                signal,
                AiDecision(
                    symbol=symbol,
                    regime="trend",
                    direction=Side.FLAT,
                    confidence=1.0,
                    multiplier=1.0,
                    news_alignment="neutral",
                    orderflow_alignment="neutral",
                    dense_zone_position="software_fixed_atr_stop",
                    pattern_type="stop_loss_exit",
                    trend_confirmation_score=1.0,
                    range_risk_score=0.0,
                    news_risk_score=0.0,
                    orderflow_confirmation_score=0.0,
                    dense_zone_breakout_score=0.0,
                    action_suggestion="close",
                    veto_action="allow",
                    brief_reason="软件 ATR 止损触发，账户2按账户1退出动作同步平仓。",
                    reason_codes=["software_fixed_atr_stop", "follower_exit_mirror"],
                ),
                "software_fixed_atr_stop",
            )
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

    def _review_open_trend_position(
        self,
        signal: StrategySignal,
        position: PositionSnapshot,
        ai: AiDecision,
        aggregated: AggregatedOrderflow,
        zone: DenseZone,
        pattern: PatternCandidate,
        data_health_status: HealthStatus,
    ):
        if not self.config.risk.position_review.enabled:
            return None
        if position.side == Side.FLAT or abs(position.qty) <= 0:
            return None
        state = self.trend_state.get(signal.symbol)
        return self.position_review.evaluate(
            signal,
            position,
            state,
            ai,
            aggregated,
            zone,
            pattern,
            self.exchange_safety.state,
            data_health_status=data_health_status,
        )

    async def _maybe_execute_position_review_addon(
        self,
        symbol: str,
        signal: StrategySignal,
        decision: PositionReviewDecision,
        ai: AiDecision,
    ):
        cfg = self.config.risk.position_review
        if decision.action != "add_candidate" or not decision.can_add or cfg.mode != "live_addon":
            return None
        if cfg.max_additions_per_position <= 0:
            self._record_position_review_execution_block(decision, "max_additions_disabled")
            return None
        if self._position_review_addon_count(symbol, decision.review_key) >= cfg.max_additions_per_position:
            self._record_position_review_execution_block(decision, "max_additions_per_position_reached")
            return None
        state = self.trend_state.get(symbol)
        if state is None:
            self._record_position_review_execution_block(decision, "missing_trend_state_at_execution")
            return None
        side_value = decision.side.value if isinstance(decision.side, Side) else str(decision.side)
        if state.side != side_value:
            self._record_position_review_execution_block(decision, "trend_state_side_changed")
            return None
        qty = float(decision.add_qty)
        if qty <= 0:
            self._record_position_review_execution_block(decision, "addon_qty_zero")
            return None
        if not await self._qty_meets_exchange_minimum(self.execution, symbol, qty, float(decision.current_price or signal.current_price)):
            self._record_position_review_execution_block(decision, "addon_qty_below_exchange_minimum")
            return None

        order_side = "buy" if side_value == Side.LONG.value else "sell"
        metadata = {
            "role": "position_review_addon",
            "position_review_key": decision.review_key,
            "position_review_created_at": decision.created_at.isoformat(),
            "add_fraction": float(decision.add_fraction),
            "r_multiple": float(decision.r_multiple),
            "atr_profit_multiple": float(decision.atr_profit_multiple),
            "native_stop_price": float(state.stop_loss_price),
            "signal_current_price": float(decision.current_price or signal.current_price),
        }
        request = OrderRequest(
            symbol=symbol,
            side=order_side,
            amount=qty,
            reduce_only=False,
            client_order_id=f"aiq_addon_{uuid.uuid4().hex[:16]}",
            reason=decision.reason or "position_review_live_addon",
            metadata=metadata,
        )
        try:
            order = await self.order_lifecycle.submit_market_order(self.execution, request)
        except (OrderRejected, OrderSubmissionUncertain) as exc:
            if execution_mode_from_config(self.config) == "live":
                safety = self.exchange_safety.mark_failure(f"position_review_addon_{type(exc).__name__.lower()}", [symbol])
                self.store.insert("exchange_health", safety.model_dump(mode="json"))
            raise RuntimeError("position_review_addon_submit_state_unknown") from exc
        self.store.insert("orders", {**order.model_dump(mode="json"), "role": "position_review_addon", "account_slot": TREND_ACCOUNT_SLOT}, symbol)

        try:
            stop_order = await self._primary_position_stop_manager().replace_for_net_position(
                self.execution,
                symbol=symbol,
                state=state,
                reason="position_review_addon_net_position_stop_replace",
                metadata={
                    "position_review_key": decision.review_key,
                    "addon_exchange_order_id": order.exchange_order_id,
                    "add_fraction": float(decision.add_fraction),
                },
            )
        except (OrderRejected, OrderSubmissionUncertain) as exc:
            if execution_mode_from_config(self.config) == "live":
                safety = self.exchange_safety.mark_failure(f"position_review_addon_stop_{type(exc).__name__.lower()}", [symbol])
                self.store.insert("exchange_health", safety.model_dump(mode="json"))
                raise RuntimeError("position_review_addon_stop_state_unknown_manual_gate_required") from exc
            raise
        except Exception as exc:  # noqa: BLE001
            if execution_mode_from_config(self.config) == "live":
                safety = self.exchange_safety.mark_failure(f"position_review_addon_stop_{type(exc).__name__.lower()}", [symbol])
                self.store.insert("exchange_health", safety.model_dump(mode="json"))
                raise RuntimeError("position_review_addon_stop_state_unknown_manual_gate_required") from exc
            raise
        self.store.insert(
            "position_reviews",
            decision.model_copy(
                update={
                    "action": "add_executed",
                    "can_add": True,
                    "shadow_only": False,
                    "addon_order_id": order.exchange_order_id or request.client_order_id,
                    "addon_stop_order_id": stop_order.exchange_order_id,
                    "reason": "position_review_live_addon_executed",
                }
            ),
            symbol,
        )
        await self._mirror_addon_to_followers(symbol, signal, ai, decision, order)
        return order

    def _record_position_review_execution_block(self, decision: PositionReviewDecision, reason: str) -> None:
        self.store.insert(
            "position_reviews",
            decision.model_copy(
                update={
                    "action": "blocked",
                    "can_add": False,
                    "shadow_only": False,
                    "reason": f"position_review_execution_blocked:{reason}",
                    "reason_codes": [*decision.reason_codes, reason],
                }
            ),
            decision.symbol,
        )

    def _position_review_addon_count(self, symbol: str, review_key: str) -> int:
        if not review_key:
            return 0
        client_order_ids: set[str] = set()
        for row in self.store.fetch_payloads("order_lifecycle", symbol=symbol, limit=500):
            payload = row.get("payload") or {}
            metadata = payload.get("metadata") or {}
            if payload.get("order_type") != "market":
                continue
            if payload.get("account_slot") != TREND_ACCOUNT_SLOT:
                continue
            if metadata.get("role") != "position_review_addon":
                continue
            if metadata.get("position_review_key") != review_key:
                continue
            client_order_id = payload.get("client_order_id")
            if client_order_id:
                client_order_ids.add(str(client_order_id))
        return len(client_order_ids)

    async def _qty_meets_exchange_minimum(self, gateway, symbol: str, qty: float, price: float) -> bool:
        min_amount = await gateway.minimum_order_amount(symbol, price)
        contract_size = await gateway.contract_size(symbol)
        min_base_qty = float(min_amount) * max(float(contract_size or 1.0), 1e-12)
        return float(qty) >= min_base_qty

    def _fixed_atr_stop_signal(
        self,
        symbol: str,
        timeframe: str,
        candles,
        position: PositionSnapshot,
        *,
        trigger_price: float | None = None,
        use_candle_extremes: bool = True,
    ):
        state = self.trend_state.get(symbol)
        if state is None or position.side == Side.FLAT or abs(position.qty) <= 0 or len(candles) == 0:
            return None
        last = candles.iloc[-1]
        current_price = float(trigger_price or position.mark_price or last["close"])
        long_touched = float(last["low"]) <= state.stop_loss_price if use_candle_extremes else current_price <= state.stop_loss_price
        short_touched = float(last["high"]) >= state.stop_loss_price if use_candle_extremes else current_price >= state.stop_loss_price
        if position.side == Side.LONG and state.side == Side.LONG and long_touched:
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
        if position.side == Side.SHORT and state.side == Side.SHORT and short_touched:
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
        return self._record_trend_entry_state_for_store(self.trend_state, symbol, signal, entry_price)

    def _record_trend_entry_state_for_store(
        self,
        trend_store: TrendStateStore,
        symbol: str,
        signal,
        entry_price: float,
    ) -> TrendPositionState | None:
        atr_value = float(signal.technical_evidence.get("entry_stop_atr") or signal.technical_evidence.get("atr") or 0.0)
        atr_stop_multiple = float(signal.technical_evidence.get("atr_stop_multiple") or self.config.strategy.trend.atr_stop_multiple)
        if atr_value <= 0:
            logger.warning("trend_stop_state_not_recorded", extra={"symbol": symbol, "reason": "missing_atr"})
            return None
        side = Side.LONG if signal.action == SignalAction.LONG else Side.SHORT
        return trend_store.record_entry(symbol, side, float(entry_price), atr_value, atr_stop_multiple)

    async def _place_native_stop_loss(
        self,
        symbol: str,
        signal,
        state: TrendPositionState,
        amount: float,
    ) -> None:
        try:
            order = await self._primary_position_stop_manager().replace_for_net_position(
                self.execution,
                symbol=symbol,
                state=state,
                reason="native_fixed_atr_net_position_stop",
                metadata={"entry_stop_amount_hint": abs(float(amount))},
            )
            if order.exchange_order_id:
                self.trend_state.set_native_stop_order_id(symbol, order.exchange_order_id)
        except (OrderRejected, OrderSubmissionUncertain) as exc:
            if execution_mode_from_config(self.config) == "live":
                safety = self.exchange_safety.mark_failure(f"native_stop_submit_{type(exc).__name__.lower()}", [symbol])
                self.store.insert("exchange_health", safety.model_dump(mode="json"))
                raise RuntimeError("native_stop_loss_state_unknown_manual_gate_required") from exc
            logger.exception("native_stop_loss_submit_failed_emergency_close", extra={"symbol": symbol})
            close_order = await self.order_lifecycle.close_position(
                self.execution,
                symbol,
                reason="native_stop_submit_failed_emergency_close",
            )
            if close_order:
                self.store.insert("orders", close_order, symbol)
            self.trend_state.clear(symbol)
            raise RuntimeError("native_stop_loss_submit_failed") from exc
        except Exception as exc:  # noqa: BLE001
            if execution_mode_from_config(self.config) == "live":
                safety = self.exchange_safety.mark_failure(f"native_stop_submit_{type(exc).__name__.lower()}", [symbol])
                self.store.insert("exchange_health", safety.model_dump(mode="json"))
                raise RuntimeError("native_stop_loss_state_unknown_manual_gate_required") from exc
            logger.exception("native_stop_loss_submit_failed_emergency_close", extra={"symbol": symbol})
            close_order = await self.order_lifecycle.close_position(
                self.execution,
                symbol,
                reason="native_stop_submit_failed_emergency_close",
            )
            if close_order:
                self.store.insert("orders", close_order, symbol)
            self.trend_state.clear(symbol)
            raise RuntimeError("native_stop_loss_submit_failed") from exc

    async def _cancel_native_stop_order(self, symbol: str) -> None:
        await self._primary_position_stop_manager().cancel_all_managed_stops(
            self.execution,
            symbol,
            reason="primary_position_exit_or_reverse",
        )

    def _primary_position_stop_manager(self) -> PositionStopManager:
        return PositionStopManager(
            self.store,
            self.order_lifecycle,
            self.trend_state,
            account_slot=TREND_ACCOUNT_SLOT,
            stop_role="net_position_stop",
            legacy_stop_roles={"position_review_addon_stop"},
        )

    def _follower_position_stop_manager(self) -> PositionStopManager:
        return PositionStopManager(
            self.store,
            self.follower_order_lifecycle,
            self.follower_trend_state,
            account_slot=FOLLOWER_ACCOUNT_SLOT,
            stop_role="follower_net_position_stop",
            legacy_stop_roles={"follower_position_review_addon_stop"},
        )

    def _active_followers(self) -> list[FollowerAccountConfig]:
        return [follower for follower in self.config.followers if follower.enabled and self._follower_route_configured(follower)]

    def _follower_route_configured(self, follower: FollowerAccountConfig) -> bool:
        account_slot = self._canonical_follower_slot(follower.account_slot)
        if account_slot != FOLLOWER_ACCOUNT_SLOT:
            return False
        if execution_mode_from_config(self.config) != "live":
            return True
        has_follower_pair = bool(os.getenv("GATEIO_FOLLOWER_API_KEY", "").strip()) and bool(
            os.getenv("GATEIO_FOLLOWER_API_SECRET", "").strip()
        )
        return has_follower_pair

    async def _mirror_exit_to_followers(
        self,
        symbol: str,
        signal: StrategySignal,
        ai: AiDecision,
        reason: str,
    ) -> None:
        for follower in self._active_followers():
            if not follower.mirror_exits:
                continue
            await self._mirror_exit_to_follower(follower, symbol, signal, ai, reason)

    async def _mirror_exit_to_follower(
        self,
        follower: FollowerAccountConfig,
        symbol: str,
        signal: StrategySignal,
        ai: AiDecision,
        reason: str,
    ) -> None:
        account_slot = self._canonical_follower_slot(follower.account_slot)
        try:
            order = await self.follower_order_lifecycle.close_position(
                self.follower_execution,
                symbol,
                reason=f"follower_mirror_exit:{reason}",
            )
            await self._cancel_follower_native_stop_order(symbol)
            self.follower_trend_state.clear(symbol)
            self._record_follower_execution(
                status="exit_mirrored" if order else "exit_no_position",
                account_slot=account_slot,
                symbol=symbol,
                signal=signal,
                ai=ai,
                reason=reason,
                order=order.model_dump(mode="json") if order else None,
            )
            if order:
                self.store.insert("orders", {**order.model_dump(mode="json"), "account_slot": account_slot, "role": "follower"}, symbol)
        except Exception as exc:  # noqa: BLE001
            logger.exception("follower_exit_failed", extra={"symbol": symbol, "account_slot": account_slot})
            self._record_follower_execution(
                status="exit_failed",
                account_slot=account_slot,
                symbol=symbol,
                signal=signal,
                ai=ai,
                reason=reason,
                error_type=type(exc).__name__,
            )

    async def _mirror_entry_to_followers(
        self,
        symbol: str,
        signal: StrategySignal,
        ai: AiDecision,
        risk: RiskDecision,
        primary_order,
    ) -> None:
        for follower in self._active_followers():
            if not follower.mirror_entries:
                continue
            await self._mirror_entry_to_follower(follower, symbol, signal, ai, risk, primary_order)

    async def _mirror_addon_to_followers(
        self,
        symbol: str,
        signal: StrategySignal,
        ai: AiDecision,
        decision: PositionReviewDecision,
        primary_order,
    ) -> None:
        for follower in self._active_followers():
            if not follower.mirror_entries:
                continue
            await self._mirror_addon_to_follower(follower, symbol, signal, ai, decision, primary_order)

    async def _mirror_addon_to_follower(
        self,
        follower: FollowerAccountConfig,
        symbol: str,
        signal: StrategySignal,
        ai: AiDecision,
        decision: PositionReviewDecision,
        primary_order,
    ) -> None:
        account_slot = self._canonical_follower_slot(follower.account_slot)
        try:
            side_value = decision.side.value if isinstance(decision.side, Side) else str(decision.side)
            positions = await self.follower_execution.fetch_positions([symbol])
            same = next(
                (
                    item
                    for item in positions
                    if item.symbol == symbol
                    and (item.side.value if isinstance(item.side, Side) else str(item.side)) == side_value
                    and abs(item.qty) > 0
                ),
                None,
            )
            if same is None:
                self._record_follower_execution(
                    status="addon_skipped_no_same_direction_position",
                    account_slot=account_slot,
                    symbol=symbol,
                    signal=signal,
                    ai=ai,
                    reason="follower_no_same_direction_position_for_addon",
                    primary_order=primary_order.model_dump(mode="json") if hasattr(primary_order, "model_dump") else None,
                )
                return
            follower_state = self.follower_trend_state.get(symbol)
            if follower_state is None or follower_state.side != side_value or not follower_state.native_stop_order_id:
                self._record_follower_execution(
                    status="addon_blocked_missing_follower_stop_state",
                    account_slot=account_slot,
                    symbol=symbol,
                    signal=signal,
                    ai=ai,
                    reason="follower_addon_requires_verified_native_stop",
                    primary_order=primary_order.model_dump(mode="json") if hasattr(primary_order, "model_dump") else None,
                )
                return
            qty = abs(float(same.qty)) * float(decision.add_fraction)
            if qty <= 0 or not await self._qty_meets_exchange_minimum(
                self.follower_execution,
                symbol,
                qty,
                float(decision.current_price or signal.current_price),
            ):
                self._record_follower_execution(
                    status="addon_blocked_by_follower_sizing",
                    account_slot=account_slot,
                    symbol=symbol,
                    signal=signal,
                    ai=ai,
                    reason="follower_addon_qty_below_minimum",
                    primary_order=primary_order.model_dump(mode="json") if hasattr(primary_order, "model_dump") else None,
                )
                return
            order_side = "buy" if side_value == Side.LONG.value else "sell"
            entry_request = OrderRequest(
                symbol=symbol,
                side=order_side,
                amount=qty,
                reduce_only=False,
                client_order_id=f"aiq_fol_addon_{uuid.uuid4().hex[:12]}",
                reason=f"follower_mirror_position_review_addon:{decision.reason}",
                metadata={
                    "role": "follower_position_review_addon",
                    "position_review_key": decision.review_key,
                    "add_fraction": float(decision.add_fraction),
                },
            )
            order = await self.follower_order_lifecycle.submit_market_order(self.follower_execution, entry_request)
            self.store.insert("orders", {**order.model_dump(mode="json"), "account_slot": account_slot, "role": "follower_position_review_addon"}, symbol)
            stop_order = await self._follower_position_stop_manager().replace_for_net_position(
                self.follower_execution,
                symbol=symbol,
                state=follower_state,
                reason="follower_position_review_addon_net_position_stop_replace",
                metadata={
                    "position_review_key": decision.review_key,
                    "addon_exchange_order_id": order.exchange_order_id,
                    "add_fraction": float(decision.add_fraction),
                },
            )
            self._record_follower_execution(
                status="addon_mirrored",
                account_slot=account_slot,
                symbol=symbol,
                signal=signal,
                ai=ai,
                reason="follower_mirrored_position_review_addon",
                order=order.model_dump(mode="json"),
                primary_order=primary_order.model_dump(mode="json") if hasattr(primary_order, "model_dump") else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("follower_addon_failed", extra={"symbol": symbol, "account_slot": account_slot})
            self._record_follower_execution(
                status="addon_failed",
                account_slot=account_slot,
                symbol=symbol,
                signal=signal,
                ai=ai,
                reason="follower_addon_exception",
                error_type=type(exc).__name__,
                primary_order=primary_order.model_dump(mode="json") if hasattr(primary_order, "model_dump") else None,
            )

    def _order_risk_metadata(
        self,
        signal: StrategySignal,
        ai: AiDecision,
        risk: RiskDecision,
        *,
        role: str,
        sizing_reason: str | None = None,
    ) -> dict[str, object]:
        return {
            "role": role,
            "strategy_action": signal.action.value,
            "strategy_symbol": signal.symbol,
            "strategy_timeframe": signal.timeframe,
            "signal_current_price": float(signal.current_price),
            "risk_position_tier": risk.position_tier,
            "risk_position_scale": float(risk.position_scale),
            "risk_sizing_policy": risk.sizing_policy,
            "legacy_position_tier": risk.legacy_position_tier,
            "legacy_position_scale": risk.legacy_position_scale,
            "calibrated_position_tier": risk.calibrated_position_tier,
            "calibrated_position_scale": risk.calibrated_position_scale,
            "calibrated_edge_score": risk.calibrated_edge_score,
            "risk_decision_score": float(risk.decision_score),
            "risk_clipped_qty": float(risk.clipped_qty),
            "risk_target_qty": float(risk.target_qty),
            "strategy_baseline_notional": float(risk.strategy_baseline_notional),
            "ai_desired_notional": float(risk.ai_desired_notional),
            "sizing_basis": risk.sizing_basis,
            "risk_reason": risk.reason,
            "ai_confidence": float(ai.confidence),
            "ai_action_suggestion": ai.action_suggestion,
            "ai_direction": ai.direction,
            "sizing_reason": sizing_reason,
        }

    async def _mirror_entry_to_follower(
        self,
        follower: FollowerAccountConfig,
        symbol: str,
        signal: StrategySignal,
        ai: AiDecision,
        risk: RiskDecision,
        primary_order,
    ) -> None:
        account_slot = self._canonical_follower_slot(follower.account_slot)
        try:
            positions = await self.follower_execution.fetch_positions([symbol])
            same = self._same_direction_position_from_signal(signal, positions)
            if same is not None:
                self._record_follower_execution(
                    status="entry_skipped_same_direction_position",
                    account_slot=account_slot,
                    symbol=symbol,
                    signal=signal,
                    ai=ai,
                    risk=risk,
                    reason="same_direction_position_exists",
                )
                return

            opposite = self._opposite_position(signal, positions)
            if opposite is not None:
                close_order = await self.follower_order_lifecycle.close_position(
                    self.follower_execution,
                    symbol,
                    reason="follower_reverse_signal_close_first",
                )
                await self._cancel_follower_native_stop_order(symbol)
                self.follower_trend_state.clear(symbol)
                if close_order:
                    self.store.insert("orders", {**close_order.model_dump(mode="json"), "account_slot": account_slot, "role": "follower"}, symbol)

            qty, sizing_reason = await self._follower_entry_qty(follower, symbol, signal, risk)
            if qty <= 0:
                self._record_follower_execution(
                    status="entry_blocked_by_follower_sizing",
                    account_slot=account_slot,
                    symbol=symbol,
                    signal=signal,
                    ai=ai,
                    risk=risk,
                    reason=sizing_reason,
                )
                return

            entry_request = OrderRequest(
                symbol=symbol,
                side="buy" if signal.action == SignalAction.LONG else "sell",
                amount=qty,
                reduce_only=False,
                client_order_id=f"aiq_fol_{uuid.uuid4().hex[:18]}",
                reason=f"follower_mirror_entry:{risk.reason}",
                metadata=self._order_risk_metadata(signal, ai, risk, role="follower", sizing_reason=sizing_reason),
            )
            order = await self.follower_order_lifecycle.submit_market_order(self.follower_execution, entry_request)
            self.store.insert("orders", {**order.model_dump(mode="json"), "account_slot": account_slot, "role": "follower"}, symbol)
            entry_state = self._record_trend_entry_state_for_store(
                self.follower_trend_state,
                symbol,
                signal,
                float(order.price or signal.current_price),
            )
            if entry_state is None:
                raise RuntimeError("follower_entry_missing_trend_stop_state")
            await self._place_follower_native_stop_loss(symbol, signal, entry_state, qty)
            self._record_follower_execution(
                status="entry_mirrored",
                account_slot=account_slot,
                symbol=symbol,
                signal=signal,
                ai=ai,
                risk=risk,
                reason=sizing_reason,
                order=order.model_dump(mode="json"),
                primary_order=primary_order.model_dump(mode="json") if hasattr(primary_order, "model_dump") else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("follower_entry_failed", extra={"symbol": symbol, "account_slot": account_slot})
            self._record_follower_execution(
                status="entry_failed",
                account_slot=account_slot,
                symbol=symbol,
                signal=signal,
                ai=ai,
                risk=risk,
                reason="follower_entry_exception",
                error_type=type(exc).__name__,
            )

    async def _follower_entry_qty(
        self,
        follower: FollowerAccountConfig,
        symbol: str,
        signal: StrategySignal,
        risk: RiskDecision,
    ) -> tuple[float, str]:
        balance = await self.follower_execution.fetch_balance_summary()
        if not balance.get("ok"):
            return 0.0, "follower_balance_unavailable"
        equity = float(balance.get("usdt_total") or balance.get("usdt_free") or 0.0)
        if equity <= 0:
            return 0.0, "follower_equity_zero"
        positions = await self.follower_execution.fetch_positions([symbol])
        used_notional = sum(position.notional for position in positions)
        max_notional = equity * follower.max_leverage
        remaining = max(max_notional - used_notional, 0.0)
        target_notional = max_notional * float(risk.position_scale or 0.0) * follower.follow_ratio
        clipped_notional = min(max(target_notional, 0.0), remaining)
        qty = clipped_notional / signal.current_price if signal.current_price > 0 else 0.0
        min_amount = await self.follower_execution.minimum_order_amount(symbol, signal.current_price)
        contract_size = await self.follower_execution.contract_size(symbol)
        min_base_qty = float(min_amount) * max(float(contract_size or 1.0), 1e-12)
        if qty < min_base_qty:
            return 0.0, f"follower_qty_below_minimum:{qty:.8f}<{min_base_qty:.8f}"
        return qty, "follower_sized_from_shared_ai_decision"

    async def _place_follower_native_stop_loss(
        self,
        symbol: str,
        signal: StrategySignal,
        state: TrendPositionState,
        amount: float,
    ) -> None:
        order = await self._follower_position_stop_manager().replace_for_net_position(
            self.follower_execution,
            symbol=symbol,
            state=state,
            reason="follower_native_fixed_atr_net_position_stop",
            metadata={"entry_stop_amount_hint": abs(float(amount))},
        )
        if order.exchange_order_id:
            self.follower_trend_state.set_native_stop_order_id(symbol, order.exchange_order_id)

    async def _cancel_follower_native_stop_order(self, symbol: str) -> None:
        await self._follower_position_stop_manager().cancel_all_managed_stops(
            self.follower_execution,
            symbol,
            reason="follower_position_exit_or_reverse",
        )

    def _same_direction_position_from_signal(
        self,
        signal: StrategySignal,
        positions: list[PositionSnapshot],
    ) -> PositionSnapshot | None:
        expected_side = Side.LONG if signal.action == SignalAction.LONG else Side.SHORT
        for position in positions:
            if position.symbol == signal.symbol and position.side == expected_side and abs(position.qty) > 0:
                return position
        return None

    def _canonical_follower_slot(self, account_slot: str) -> str:
        return FOLLOWER_ACCOUNT_SLOT if account_slot == "follower" else account_slot

    def _record_follower_execution(
        self,
        *,
        status: str,
        account_slot: str,
        symbol: str,
        signal: StrategySignal,
        ai: AiDecision,
        reason: str,
        risk: RiskDecision | None = None,
        order: dict | None = None,
        primary_order: dict | None = None,
        error_type: str | None = None,
    ) -> None:
        self.store.insert(
            "follower_executions",
            {
                "status": status,
                "account_slot": account_slot,
                "symbol": symbol,
                "reason": reason,
                "signal": signal.model_dump(mode="json"),
                "ai_decision": ai.model_dump(mode="json"),
                "risk_decision": risk.model_dump(mode="json") if risk else None,
                "order": order,
                "primary_order": primary_order,
                "error_type": error_type,
            },
            symbol,
        )

    def _opposite_position(self, signal, positions: list[PositionSnapshot]) -> PositionSnapshot | None:
        expected_side = Side.LONG if signal.action == SignalAction.LONG else Side.SHORT
        opposite_side = Side.SHORT if expected_side == Side.LONG else Side.LONG
        for position in positions:
            if position.symbol == signal.symbol and position.side == opposite_side and abs(position.qty) > 0:
                return position
        return None

    async def _market_leader_context(
        self,
        symbol: str,
        timeframe: str,
        signal: StrategySignal,
        symbol_candles=None,
    ) -> MarketLeaderContext:
        leader_symbol = "BTC/USDT:USDT"
        try:
            candles = await self.market.fetch_ohlcv(leader_symbol, timeframe, limit=72, source="auto", closed_only=True)
            return self._build_market_leader_context(leader_symbol, timeframe, candles, signal, symbol_candles)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "market_leader_context_unavailable",
                extra={"symbol": symbol, "leader_symbol": leader_symbol, "error_type": type(exc).__name__},
            )
            return MarketLeaderContext(
                symbol=leader_symbol,
                timeframe=timeframe,
                available=False,
                strategy_alignment_hint=Alignment.UNKNOWN,
                summary=f"BTC leader context unavailable: {type(exc).__name__}",
                warnings=["btc_leader_context_unavailable"],
            )

    def _build_market_leader_context(
        self,
        leader_symbol: str,
        timeframe: str,
        candles,
        signal: StrategySignal,
        symbol_candles=None,
    ) -> MarketLeaderContext:
        if candles is None or len(candles) < 5 or "close" not in candles:
            return MarketLeaderContext(
                symbol=leader_symbol,
                timeframe=timeframe,
                available=False,
                summary="BTC leader context unavailable: insufficient candles",
                warnings=["btc_leader_context_insufficient_candles"],
            )
        closes = [float(value) for value in candles["close"].tolist() if float(value) > 0]
        if len(closes) < 5:
            return MarketLeaderContext(
                symbol=leader_symbol,
                timeframe=timeframe,
                available=False,
                summary="BTC leader context unavailable: insufficient valid closes",
                warnings=["btc_leader_context_insufficient_valid_closes"],
            )
        last = closes[-1]
        change_1h = self._pct_change_from(closes, 1)
        change_4h = self._pct_change_from(closes, 4)
        change_24h = self._pct_change_from(closes, 24)
        symbol_closes = self._valid_closes_from_candles(symbol_candles)
        symbol_change_1h = self._pct_change_from(symbol_closes, 1) if len(symbol_closes) >= 2 else 0.0
        symbol_change_4h = self._pct_change_from(symbol_closes, 4) if len(symbol_closes) >= 5 else 0.0
        relative_strength_1h = round(symbol_change_1h - change_1h, 4)
        relative_strength_4h = round(symbol_change_4h - change_4h, 4)
        direction = self._leader_direction(change_1h, change_4h, change_24h)
        leader_regime = self._leader_regime(
            change_1h,
            change_4h,
            change_24h,
            relative_strength_1h,
            relative_strength_4h,
            signal,
        )
        alignment = self._leader_alignment_for_signal(direction, signal, leader_regime)
        impact = self._leader_impact_score(change_1h, change_4h, change_24h)
        rotation_score = self._eth_btc_rotation_score(
            relative_strength_1h,
            relative_strength_4h,
            leader_regime,
            signal,
        )
        warnings: list[str] = []
        if leader_regime in {"leader_downtrend", "distribution_risk"}:
            warnings.append(f"btc_leader_{leader_regime}")
        elif leader_regime in {"rotation_lag", "leader_pullback"}:
            warnings.append(f"btc_eth_{leader_regime}")
        return MarketLeaderContext(
            symbol=leader_symbol,
            timeframe=timeframe,
            available=True,
            price=last,
            change_1h_pct=change_1h,
            change_4h_pct=change_4h,
            change_24h_pct=change_24h,
            relative_strength_1h_pct=relative_strength_1h,
            relative_strength_4h_pct=relative_strength_4h,
            market_direction=direction,
            strategy_alignment_hint=alignment,
            leader_regime=leader_regime,
            eth_btc_rotation_score=rotation_score,
            impact_score=impact,
            summary=(
                f"BTC {timeframe} leader context: direction={direction.value}, regime={leader_regime}, "
                f"alignment={alignment.value}, impact={impact:.2f}, rotation={rotation_score:.2f}, "
                f"btc_chg1h={change_1h:.2f}%, btc_chg4h={change_4h:.2f}%, btc_chg24h={change_24h:.2f}%, "
                f"eth_rel1h={relative_strength_1h:.2f}%, eth_rel4h={relative_strength_4h:.2f}%."
            ),
            warnings=warnings,
        )

    def _valid_closes_from_candles(self, candles) -> list[float]:
        if candles is None or "close" not in candles:
            return []
        closes: list[float] = []
        for value in candles["close"].tolist():
            try:
                close = float(value)
            except (TypeError, ValueError):
                continue
            if close > 0:
                closes.append(close)
        return closes

    def _pct_change_from(self, closes: list[float], bars_back: int) -> float:
        if len(closes) <= bars_back or closes[-bars_back - 1] <= 0:
            return 0.0
        return round((closes[-1] / closes[-bars_back - 1] - 1.0) * 100.0, 4)

    def _leader_direction(self, change_1h: float, change_4h: float, change_24h: float) -> NewsDirection:
        weighted = change_1h * 0.45 + change_4h * 0.35 + change_24h * 0.20
        if weighted >= 0.35:
            return NewsDirection.BULLISH
        if weighted <= -0.35:
            return NewsDirection.BEARISH
        return NewsDirection.NEUTRAL

    def _leader_alignment_for_signal(
        self,
        direction: NewsDirection,
        signal: StrategySignal,
        leader_regime: str = "unknown",
    ) -> Alignment:
        if signal.action == SignalAction.LONG and leader_regime in {"rotation_lag", "leader_pullback"}:
            return Alignment.ALIGNED
        if direction == NewsDirection.NEUTRAL:
            return Alignment.NEUTRAL
        if direction == NewsDirection.UNKNOWN:
            return Alignment.UNKNOWN
        if signal.action == SignalAction.LONG:
            return Alignment.ALIGNED if direction == NewsDirection.BULLISH else Alignment.CONFLICT
        if signal.action == SignalAction.SHORT:
            return Alignment.ALIGNED if direction == NewsDirection.BEARISH else Alignment.CONFLICT
        return Alignment.UNKNOWN

    def _leader_impact_score(self, change_1h: float, change_4h: float, change_24h: float) -> float:
        weighted_abs = abs(change_1h) * 0.45 + abs(change_4h) * 0.35 + abs(change_24h) * 0.20
        return round(max(0.0, min(1.0, weighted_abs / 4.0)), 4)

    def _leader_regime(
        self,
        change_1h: float,
        change_4h: float,
        change_24h: float,
        relative_strength_1h: float,
        relative_strength_4h: float,
        signal: StrategySignal,
    ) -> str:
        if change_4h <= -1.2 or change_24h <= -2.8:
            return "leader_downtrend"
        if signal.action == SignalAction.LONG:
            if change_1h <= -0.35 and change_4h <= -0.35 and relative_strength_4h <= 0.0:
                return "distribution_risk"
            if -0.70 <= change_1h <= 0.15 and change_4h >= -0.60 and relative_strength_1h >= 0.35 and relative_strength_4h >= 0.20:
                return "rotation_lag"
            if change_1h < 0.0 and change_4h >= 0.0 and change_24h >= 0.0 and relative_strength_4h >= -0.10:
                return "leader_pullback"
            if change_4h > 0.35 or change_24h > 0.80:
                return "leader_uptrend"
        if signal.action == SignalAction.SHORT:
            if change_4h <= -0.35 or change_24h <= -0.80:
                return "leader_downtrend"
            if change_1h > 0.35 and change_4h >= 0.0:
                return "distribution_risk"
        if change_4h > 0.35 or change_24h > 0.80:
            return "leader_uptrend"
        if change_4h < -0.35 or change_24h < -0.80:
            return "leader_downtrend"
        return "unknown"

    def _eth_btc_rotation_score(
        self,
        relative_strength_1h: float,
        relative_strength_4h: float,
        leader_regime: str,
        signal: StrategySignal,
    ) -> float:
        if signal.action != SignalAction.LONG:
            return 0.0
        if leader_regime not in {"rotation_lag", "leader_pullback", "leader_uptrend"}:
            return 0.0
        raw = 0.45 + max(relative_strength_1h, 0.0) * 0.18 + max(relative_strength_4h, 0.0) * 0.22
        if leader_regime == "rotation_lag":
            raw += 0.15
        elif leader_regime == "leader_pullback":
            raw += 0.05
        return round(max(0.0, min(1.0, raw)), 4)

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
        market_leader_context: MarketLeaderContext | None = None,
        event_key: str | None = None,
    ) -> AiDecision:
        reservation = self.deepseek_budget.reserve(symbol=signal.symbol, call_type=call_type, event_key=event_key)
        if not reservation.allowed:
            reason = f"deepseek_budget_blocked:{reservation.reason}"
            logger.warning(
                "deepseek_budget_blocked",
                extra={"symbol": signal.symbol, "call_type": call_type, "reason": reservation.reason},
            )
            return self.brain.local_fallback_decision(
                signal,
                orderflow,
                dense_zone,
                pattern,
                news,
                reason,
                regime_pattern,
                market_leader_context,
            )
        try:
            decision = await self.brain.analyze_symbol(
                signal,
                orderflow,
                dense_zone,
                pattern,
                news,
                regime_pattern,
                market_leader_context=market_leader_context,
                call_type=call_type,
            )
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
        close_errors: list[BaseException] = []
        for resource in [self.market, self.orderflow_client, self.execution, self.follower_execution]:
            try:
                await resource.close()
            except Exception as exc:  # noqa: BLE001
                close_errors.append(exc)
                logger.warning("trading_app_resource_close_failed", extra={"resource": resource.__class__.__name__, "error_type": type(exc).__name__})
        self.store.close()
        if close_errors:
            raise RuntimeError("trading_app_resource_close_failed") from close_errors[0]


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
        except asyncio.CancelledError:
            logger.info("shutdown_requested")
    finally:
        await app.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("shutdown_requested")
