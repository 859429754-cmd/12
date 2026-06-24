from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from ai_quant_trader.app import TradingApp
from ai_quant_trader.core.models import (
    AiDecision,
    Alignment,
    DataHealthReport,
    HealthStatus,
    MarketRegime,
    RegimePattern,
    Side,
    SignalAction,
    StrategySignal,
    VetoAction,
    OrderRequest,
    OrderLifecycleEvent,
    OrderLifecycleStatus,
)
from ai_quant_trader.execution.gateway.mock import MockExchangeGateway
from ai_quant_trader.strategy.trend_state import TrendStateStore


def _write_config(path: Path, db_path: Path, audit_path: Path) -> None:
    path.write_text(
        f"""
runtime:
  execution_mode: "mock"
  dry_run: true
  database_path: "{db_path.as_posix()}"
  audit_log_path: "{audit_path.as_posix()}"
risk:
  max_total_leverage: 4.0
  ai_full_size_confidence: 0.75
  min_confidence_to_trade: 0.55
symbols:
  - symbol: "ETH/USDT:USDT"
    timeframe: "1h"
strategy:
  trend:
    variant: "with_volume"
    kc_length: 20
    kc_scalar: 2.8
    atr_length: 14
    atr_stop_multiple: 1.5
    volume_multiple: 2.5
    position_fraction: 1.0
    use_ema_filter: false
    use_volume_filter: true
    momentum_filter: "kdj"
ai:
  ai_enabled_symbols:
    - "ETH/USDT:USDT"
news:
  jin10_enabled: false
  refresh_interval_minutes: 10
""",
        encoding="utf-8",
    )


def _write_two_symbol_config(path: Path, db_path: Path, audit_path: Path) -> None:
    path.write_text(
        f"""
runtime:
  execution_mode: "mock"
  dry_run: true
  database_path: "{db_path.as_posix()}"
  audit_log_path: "{audit_path.as_posix()}"
risk:
  max_total_leverage: 1.0
  ai_full_size_confidence: 0.75
  min_confidence_to_trade: 0.55
  ai_dynamic_position_sizing: true
symbols:
  - symbol: "ETH/USDT:USDT"
    timeframe: "1h"
  - symbol: "BTC/USDT:USDT"
    timeframe: "1h"
strategy:
  trend:
    variant: "with_volume"
    kc_length: 20
    kc_scalar: 2.8
    atr_length: 14
    atr_stop_multiple: 1.5
    volume_multiple: 2.5
    position_fraction: 1.0
    use_ema_filter: false
    use_volume_filter: true
    momentum_filter: "kdj"
ai:
  ai_enabled_symbols:
    - "ETH/USDT:USDT"
    - "BTC/USDT:USDT"
news:
  jin10_enabled: false
  refresh_interval_minutes: 10
""",
        encoding="utf-8",
    )


def _candles() -> pd.DataFrame:
    ts = pd.date_range("2026-01-01", periods=160, freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": [2000.0] * len(ts),
            "high": [2010.0] * len(ts),
            "low": [1990.0] * len(ts),
            "close": [2000.0] * len(ts),
            "volume": [2000.0] * len(ts),
        }
    )


def _ai(action: SignalAction) -> AiDecision:
    direction = "long" if action == SignalAction.LONG else "flat"
    return AiDecision(
        symbol="ETH/USDT:USDT",
        regime=MarketRegime.TREND,
        direction=direction,
        confidence=0.92,
        multiplier=1.0,
        news_alignment=Alignment.ALIGNED,
        orderflow_alignment=Alignment.ALIGNED,
        dense_zone_position="above_value",
        trend_confirmation_score=0.95,
        range_risk_score=0.05,
        news_risk_score=0.05,
        orderflow_confirmation_score=0.9,
        dense_zone_breakout_score=0.9,
        action_suggestion="open_long" if action == SignalAction.LONG else "hold",
        veto_action=VetoAction.ALLOW,
        brief_reason="mock full-chain smoke",
    )


def _ai_for_symbol(symbol: str, action: SignalAction) -> AiDecision:
    decision = _ai(action)
    decision.symbol = symbol
    return decision


@pytest.mark.asyncio
async def test_trading_cycle_opens_places_stop_then_exits_and_cancels_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    _write_config(config_path, db_path, audit_path)

    app = TradingApp(str(config_path))
    app.execution = MockExchangeGateway(str(tmp_path / "mock_exchange.json"))
    app.trend_state = TrendStateStore(str(tmp_path / "state_trend.json"))
    app.order_lifecycle.gateway_mode = "mock"
    app.control.authorize_opening(app.state, ["ETH/USDT:USDT"], operator_id="test", dry_run=True)
    app.control.enable_symbol_report(app.state, ["ETH/USDT:USDT"], operator_id="test")

    signal_action = SignalAction.LONG

    async def fake_news(live_news: bool):  # noqa: ANN001
        from ai_quant_trader.core.models import NewsDigest

        return NewsDigest(summary="mock neutral news", crypto_sentiment=Alignment.ALIGNED)

    async def fake_fetch_ohlcv(*args, **kwargs):  # noqa: ANN002, ANN003
        return _candles()

    async def fake_fetch_summaries(*args, **kwargs):  # noqa: ANN002, ANN003
        return []

    async def fake_analyze_symbol(signal, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        return _ai(signal.action)

    def fake_regime(*args, **kwargs):  # noqa: ANN002, ANN003
        return RegimePattern(
            symbol="ETH/USDT:USDT",
            regime_candidate="trend",
            strategy_allowed="trend",
            trend_score=0.9,
            range_score=0.1,
            reason_codes=["mock_trend_allowed"],
        )

    def fake_signal(*args, **kwargs):  # noqa: ANN002, ANN003
        if signal_action == SignalAction.LONG:
            return StrategySignal(
                symbol="ETH/USDT:USDT",
                timeframe="1h",
                action=SignalAction.LONG,
                current_price=2000.0,
                suggested_qty=0.02,
                signal_strength=0.95,
                technical_evidence={
                    "strategy_allowed": "trend",
                    "entry_stop_atr": 20.0,
                    "atr": 20.0,
                    "atr_stop_multiple": 1.5,
                },
            )
        return StrategySignal(
            symbol="ETH/USDT:USDT",
            timeframe="1h",
            action=SignalAction.EXIT_LONG,
            current_price=2000.0,
            suggested_qty=0.0,
            signal_strength=0.5,
            technical_evidence={"strategy_allowed": "trend", "reason": "mock_exit"},
        )

    monkeypatch.setattr(app, "_news_for_trading_cycle", fake_news)
    monkeypatch.setattr(app.market, "fetch_ohlcv", fake_fetch_ohlcv)
    monkeypatch.setattr(app.orderflow_client, "fetch_summaries", fake_fetch_summaries)
    monkeypatch.setattr(app.brain, "analyze_symbol", fake_analyze_symbol)
    monkeypatch.setattr(app.regime_patterns, "analyze", fake_regime)
    monkeypatch.setattr(app, "_generate_local_signal", fake_signal)
    monkeypatch.setattr(
        app.data_health,
        "evaluate_symbol",
        lambda **kwargs: DataHealthReport(symbol="ETH/USDT:USDT", status=HealthStatus.OK, can_open_new_entries=True),
    )

    try:
        await app.run_once(equity=1000.0, live_news=False)

        first_orders = app.store.fetch_payloads("orders", symbol="ETH/USDT:USDT", limit=10)
        first_lifecycle = app.store.fetch_payloads("order_lifecycle", symbol="ETH/USDT:USDT", limit=20)
        assert any(row["payload"].get("status") == "mock_created" for row in first_orders)
        assert any(row["payload"].get("status") == "mock_stop_created" for row in first_orders)
        assert any(row["payload"].get("order_type") == "stop_loss" for row in first_lifecycle)
        assert app.trend_state.get("ETH/USDT:USDT").native_stop_order_id

        signal_action = SignalAction.EXIT_LONG
        await app.run_once(equity=1000.0, live_news=False)

        final_positions = await app.execution.fetch_positions(["ETH/USDT:USDT"])
        assert final_positions[0].side == Side.FLAT
        latest_lifecycle = app.store.fetch_payloads("order_lifecycle", symbol="ETH/USDT:USDT", limit=20)
        assert any(row["payload"].get("order_type") == "cancel" for row in latest_lifecycle)
        assert app.trend_state.get("ETH/USDT:USDT") is None
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_run_once_refreshes_positions_after_entry_before_next_symbol_risk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    _write_two_symbol_config(config_path, db_path, audit_path)

    app = TradingApp(str(config_path))
    app.execution = MockExchangeGateway(str(tmp_path / "mock_exchange.json"))
    app.trend_state = TrendStateStore(str(tmp_path / "state_trend.json"))
    app.order_lifecycle.gateway_mode = "mock"
    app.control.authorize_opening(app.state, ["ETH/USDT:USDT", "BTC/USDT:USDT"], operator_id="test", dry_run=True)
    app.control.enable_symbol_report(app.state, ["ETH/USDT:USDT", "BTC/USDT:USDT"], operator_id="test")

    async def fake_news(live_news: bool):  # noqa: ANN001
        from ai_quant_trader.core.models import NewsDigest

        return NewsDigest(summary="mock neutral news", crypto_sentiment=Alignment.ALIGNED)

    async def fake_fetch_ohlcv(*args, **kwargs):  # noqa: ANN002, ANN003
        return _candles()

    async def fake_fetch_summaries(*args, **kwargs):  # noqa: ANN002, ANN003
        return []

    async def fake_market_leader_context(*args, **kwargs):  # noqa: ANN002, ANN003
        return None

    async def fake_analyze(call_type, signal, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        return _ai_for_symbol(signal.symbol, signal.action)

    def fake_regime(symbol, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        return RegimePattern(
            symbol=symbol,
            regime_candidate="trend",
            strategy_allowed="trend",
            trend_score=0.9,
            range_score=0.1,
            reason_codes=["mock_trend_allowed"],
        )

    def fake_signal(symbol, timeframe, candles, position, equity):  # noqa: ANN001
        return StrategySignal(
            symbol=symbol,
            timeframe=timeframe,
            action=SignalAction.LONG,
            current_price=2000.0,
            suggested_qty=0.5,
            signal_strength=0.95,
            technical_evidence={
                "strategy_allowed": "trend",
                "entry_stop_atr": 20.0,
                "atr": 20.0,
                "atr_stop_multiple": 1.5,
            },
        )

    monkeypatch.setattr(app, "_news_for_trading_cycle", fake_news)
    monkeypatch.setattr(app.market, "fetch_ohlcv", fake_fetch_ohlcv)
    monkeypatch.setattr(app.orderflow_client, "fetch_summaries", fake_fetch_summaries)
    monkeypatch.setattr(app, "_market_leader_context", fake_market_leader_context)
    monkeypatch.setattr(app, "_analyze_with_deepseek_budget", fake_analyze)
    monkeypatch.setattr(app.regime_patterns, "analyze", fake_regime)
    monkeypatch.setattr(app, "_generate_local_signal", fake_signal)
    monkeypatch.setattr(
        app.data_health,
        "evaluate_symbol",
        lambda symbol, **kwargs: DataHealthReport(symbol=symbol, status=HealthStatus.OK, can_open_new_entries=True),
    )

    try:
        await app.run_once(equity=1000.0, live_news=False)

        eth_orders = app.store.fetch_payloads("orders", symbol="ETH/USDT:USDT", limit=10)
        btc_orders = app.store.fetch_payloads("orders", symbol="BTC/USDT:USDT", limit=10)
        btc_lifecycle = app.store.fetch_payloads("order_lifecycle", symbol="BTC/USDT:USDT", limit=10)

        assert any(row["payload"].get("status") == "mock_created" for row in eth_orders)
        assert not any(row["payload"].get("status") == "mock_created" for row in btc_orders)
        assert any(row["payload"].get("state") == "blocked_before_submit" for row in btc_lifecycle)
        assert any(row["payload"].get("reason") == "max_total_leverage_reached" for row in btc_lifecycle)
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_software_atr_stop_mirrors_exit_to_follower(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    _write_config(config_path, db_path, audit_path)
    with config_path.open("a", encoding="utf-8") as handle:
        handle.write(
            """
followers:
  - enabled: true
    account_slot: "follower"
    label: "账号2：趋势跟随账户"
    follow_ratio: 1.0
    max_leverage: 4.0
    mirror_entries: true
    mirror_exits: true
"""
        )

    app = TradingApp(str(config_path))
    app.execution = MockExchangeGateway(str(tmp_path / "mock_trend.json"))
    app.follower_execution = MockExchangeGateway(str(tmp_path / "mock_follower.json"))
    app.order_lifecycle.gateway_mode = "mock"
    app.follower_order_lifecycle.gateway_mode = "mock"

    entry_signal = StrategySignal(
        symbol="ETH/USDT:USDT",
        timeframe="1h",
        action=SignalAction.LONG,
        current_price=2000.0,
        suggested_qty=0.02,
        signal_strength=0.95,
        technical_evidence={
            "strategy_allowed": "trend",
            "entry_stop_atr": 20.0,
            "atr": 20.0,
            "atr_stop_multiple": 1.5,
        },
    )

    async def stop_candles(*args, **kwargs):  # noqa: ANN002, ANN003
        return pd.DataFrame(
            {
                "timestamp": pd.date_range("2026-01-01", periods=5, freq="1min", tz="UTC"),
                "open": [2000.0] * 5,
                "high": [2005.0] * 5,
                "low": [1969.0] * 5,
                "close": [1970.0] * 5,
                "volume": [1000.0] * 5,
            }
        )

    monkeypatch.setattr(app.market, "fetch_ohlcv", stop_candles)

    try:
        for gateway in [app.execution, app.follower_execution]:
            await gateway.create_market_order(
                OrderRequest(
                    symbol="ETH/USDT:USDT",
                    side="buy",
                    amount=0.02,
                    reduce_only=False,
                    client_order_id=f"test_open_{id(gateway)}",
                    reason="test_seed_long",
                )
            )
        app._record_trend_entry_state("ETH/USDT:USDT", entry_signal, 2000.0)
        app._record_trend_entry_state_for_store(app.follower_trend_state, "ETH/USDT:USDT", entry_signal, 2000.0)

        order = await app._enforce_fixed_atr_stop_once("ETH/USDT:USDT", "1h")

        assert order is not None
        trend_position = (await app.execution.fetch_positions(["ETH/USDT:USDT"]))[0]
        follower_position = (await app.follower_execution.fetch_positions(["ETH/USDT:USDT"]))[0]
        assert trend_position.side == Side.FLAT
        assert follower_position.side == Side.FLAT
        follower_rows = app.store.fetch_payloads("follower_executions", symbol="ETH/USDT:USDT", limit=5)
        assert any(row["payload"].get("status") == "exit_mirrored" for row in follower_rows)
        assert any(row["payload"].get("reason") == "software_fixed_atr_stop" for row in follower_rows)
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_software_atr_stop_uses_exchange_mark_not_external_1m_wick_for_short(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    _write_config(config_path, db_path, audit_path)

    app = TradingApp(str(config_path))
    mock_state = tmp_path / "mock_trend.json"
    app.execution = MockExchangeGateway(str(mock_state))
    app.order_lifecycle.gateway_mode = "mock"

    async def wick_candles(*args, **kwargs):  # noqa: ANN002, ANN003
        return pd.DataFrame(
            {
                "timestamp": pd.date_range("2026-06-24 16:00:00", periods=5, freq="1min", tz="UTC"),
                "open": [1620.0] * 5,
                "high": [1650.0] * 5,
                "low": [1619.0] * 5,
                "close": [1620.28] * 5,
                "volume": [1000.0] * 5,
            }
        )

    monkeypatch.setattr(app.market, "fetch_ohlcv", wick_candles)

    try:
        await app.execution.create_market_order(
            OrderRequest(
                symbol="ETH/USDT:USDT",
                side="sell",
                amount=0.25,
                reduce_only=False,
                client_order_id="seed_short",
                reason="test_seed_short",
            )
        )
        state = json.loads(mock_state.read_text(encoding="utf-8"))
        state["positions"]["ETH/USDT:USDT"]["entry_price"] = 1620.2
        state["positions"]["ETH/USDT:USDT"]["mark_price"] = 1620.28
        mock_state.write_text(json.dumps(state), encoding="utf-8")
        app.trend_state.record_entry("ETH/USDT:USDT", Side.SHORT, 1620.2, atr_value=15.683218100864998, atr_stop_multiple=1.5)

        order = await app._enforce_fixed_atr_stop_once("ETH/USDT:USDT", "1h")

        assert order is None
        position = (await app.execution.fetch_positions(["ETH/USDT:USDT"]))[0]
        assert position.side == Side.SHORT
        assert position.qty == pytest.approx(0.25)
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_primary_native_stop_fill_mirrors_exit_to_follower(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    _write_config(config_path, db_path, audit_path)
    with config_path.open("a", encoding="utf-8") as handle:
        handle.write(
            """
followers:
  - enabled: true
    account_slot: "follower"
    label: "账号2：趋势跟随账户"
    follow_ratio: 1.0
    max_leverage: 4.0
    mirror_entries: true
    mirror_exits: true
"""
        )

    app = TradingApp(str(config_path))
    app.follower_execution = MockExchangeGateway(str(tmp_path / "mock_follower_native_stop.json"))
    app.follower_order_lifecycle.gateway_mode = "mock"
    entry_signal = StrategySignal(
        symbol="ETH/USDT:USDT",
        timeframe="1h",
        action=SignalAction.LONG,
        current_price=2000.0,
        suggested_qty=0.02,
        signal_strength=0.95,
        technical_evidence={"strategy_allowed": "trend", "entry_stop_atr": 20.0, "atr": 20.0, "atr_stop_multiple": 1.5},
    )

    try:
        await app.follower_execution.create_market_order(
            OrderRequest(
                symbol="ETH/USDT:USDT",
                side="buy",
                amount=0.02,
                reduce_only=False,
                client_order_id="test_follower_open_native_stop",
                reason="test_seed_follower_long",
            )
        )
        app._record_trend_entry_state("ETH/USDT:USDT", entry_signal, 2000.0)
        app._record_trend_entry_state_for_store(app.follower_trend_state, "ETH/USDT:USDT", entry_signal, 2000.0)

        event = OrderLifecycleEvent(
            client_order_id="primary_stop_1",
            symbol="ETH/USDT:USDT",
            status=OrderLifecycleStatus.FILLED,
            account_slot="trend",
            order_type="stop_loss",
            side="sell",
            amount=0.02,
            reduce_only=True,
            gateway_mode="mock",
            reason="exchange_order_status_refreshed",
            exchange_order_id="gate_stop_filled_1",
            order_status="filled",
            order={"price": 1970.0},
        )

        await app._mirror_primary_native_stop_fills_to_followers([event])

        follower_position = (await app.follower_execution.fetch_positions(["ETH/USDT:USDT"]))[0]
        assert follower_position.side == Side.FLAT
        assert app.trend_state.get("ETH/USDT:USDT") is None
        assert app.follower_trend_state.get("ETH/USDT:USDT") is None
        follower_rows = app.store.fetch_payloads("follower_executions", symbol="ETH/USDT:USDT", limit=10)
        assert any(row["payload"].get("status") == "exit_mirrored" for row in follower_rows)
        assert any(row["payload"].get("primary_stop_client_order_id") == "primary_stop_1" for row in follower_rows)
    finally:
        await app.close()
