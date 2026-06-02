from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from ai_quant_trader.app import TradingApp
from ai_quant_trader.brain.budget import DeepSeekBudgetGuard
from ai_quant_trader.brain.deepseek import DeepSeekBrain
from ai_quant_trader.core.models import (
    AggregatedOrderflow,
    AiDecision,
    Alignment,
    DenseZone,
    MarketRegime,
    NewsDigest,
    PatternCandidate,
    PositionSnapshot,
    RegimePattern,
    SignalAction,
    Side,
    StrategySignal,
    VetoAction,
)
from ai_quant_trader.storage.sqlite import SQLiteStore


def make_store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(str(tmp_path / "trader.sqlite3"), str(tmp_path / "audit.jsonl"))


def make_signal(action: SignalAction = SignalAction.LONG) -> StrategySignal:
    return StrategySignal(
        symbol="ETH/USDT:USDT",
        timeframe="1h",
        action=action,
        current_price=2200.0,
        suggested_qty=0.1,
        signal_strength=0.9,
    )


def make_inputs() -> tuple[StrategySignal, AggregatedOrderflow, DenseZone, PatternCandidate, NewsDigest, RegimePattern]:
    return (
        make_signal(),
        AggregatedOrderflow(symbol="ETH/USDT:USDT", alignment_hint=Alignment.ALIGNED, data_quality=0.95, source_count=3),
        DenseZone(
            symbol="ETH/USDT:USDT",
            poc=2190.0,
            vah=2210.0,
            val=2170.0,
            current_position="above_value",
            strength=0.8,
            trend_score=0.8,
        ),
        PatternCandidate(symbol="ETH/USDT:USDT", pattern_type="breakout", confidence=0.8),
        NewsDigest(summary="quiet"),
        RegimePattern(
            symbol="ETH/USDT:USDT",
            regime_candidate="trend",
            strategy_allowed="trend",
            trend_score=0.85,
            range_score=0.15,
            reason_codes=["trend_score_dominant"],
        ),
    )


def test_deepseek_budget_blocks_hourly_limit(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    try:
        guard = DeepSeekBudgetGuard(store, max_calls_per_hour=2, max_calls_per_day=10, failure_cooldown_minutes=0)

        first = guard.reserve(symbol="ETH/USDT:USDT", call_type="trading_cycle")
        second = guard.reserve(symbol="ETH/USDT:USDT", call_type="trading_cycle")
        third = guard.reserve(symbol="ETH/USDT:USDT", call_type="trading_cycle")

        assert first.allowed is True
        assert second.allowed is True
        assert third.allowed is False
        assert third.reason == "hourly_limit_exceeded"
    finally:
        store.close()


def test_deepseek_budget_deduplicates_news_event_across_restarts(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    try:
        guard = DeepSeekBudgetGuard(store, failure_cooldown_minutes=0)

        first = guard.reserve(symbol="ETH/USDT:USDT", call_type="major_news_risk_review", event_key="source|time|title")
        guard.record_success(first.row_id)
        second_guard = DeepSeekBudgetGuard(store, failure_cooldown_minutes=0)
        second = second_guard.reserve(symbol="ETH/USDT:USDT", call_type="major_news_risk_review", event_key="source|time|title")

        assert first.allowed is True
        assert second.allowed is False
        assert second.reason == "duplicate_event_key"
    finally:
        store.close()


def test_deepseek_budget_failure_cooldown_blocks_followup_calls(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    try:
        guard = DeepSeekBudgetGuard(store, failure_cooldown_minutes=20)

        first = guard.reserve(symbol="ETH/USDT:USDT", call_type="trading_cycle")
        guard.record_failure(first.row_id, reason="deepseek_error:HTTPError", error_type="HTTPError")
        second = guard.reserve(symbol="ETH/USDT:USDT", call_type="price_wakeup")

        assert second.allowed is False
        assert second.reason == "failure_cooldown_active"
    finally:
        store.close()


def test_budget_block_reason_cannot_allow_strong_entry_signal() -> None:
    brain = DeepSeekBrain(api_key="test")
    signal, orderflow, zone, pattern, news, regime = make_inputs()

    decision = brain.local_fallback_decision(
        signal,
        orderflow,
        zone,
        pattern,
        news,
        "deepseek_budget_blocked:hourly_limit_exceeded",
        regime,
    )

    assert decision.veto_action == "block"
    assert decision.action_suggestion == "block"
    assert decision.confidence == 0.0


def write_config(path: Path, db_path: Path, audit_path: Path) -> None:
    path.write_text(
        f"""
runtime:
  dry_run: true
  database_path: "{db_path.as_posix()}"
  audit_log_path: "{audit_path.as_posix()}"
symbols:
  - symbol: "ETH/USDT:USDT"
    timeframe: "1h"
strategy:
  trend:
    variant: with_volume
    volume_multiple: 2.5
ai:
  decision_model: "deepseek-v4-pro"
  report_model: "deepseek-v4-pro"
  max_calls_per_hour: 1
  max_calls_per_day: 10
  failure_cooldown_minutes: 0
""",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_trading_app_budget_wrapper_blocks_second_ai_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl")
    app = TradingApp(str(config_path))
    signal, orderflow, zone, pattern, news, regime = make_inputs()
    calls = 0

    async def fake_analyze_symbol(*args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal calls
        calls += 1
        return AiDecision(
            symbol="ETH/USDT:USDT",
            regime=MarketRegime.TREND,
            direction=Side.LONG,
            confidence=0.8,
            multiplier=1.0,
            news_alignment=Alignment.ALIGNED,
            orderflow_alignment=Alignment.ALIGNED,
            trend_confirmation_score=0.8,
            range_risk_score=0.2,
            news_risk_score=0.2,
            orderflow_confirmation_score=0.8,
            dense_zone_breakout_score=0.8,
            action_suggestion="open_long",
            veto_action=VetoAction.ALLOW,
            brief_reason="ok",
        )

    monkeypatch.setattr(app.brain, "analyze_symbol", fake_analyze_symbol)
    try:
        first = await app._analyze_with_deepseek_budget("trading_cycle", signal, orderflow, zone, pattern, news, regime)
        second = await app._analyze_with_deepseek_budget("trading_cycle", signal, orderflow, zone, pattern, news, regime)

        assert first.veto_action == "allow"
        assert second.veto_action == "block"
        assert "deepseek_budget_blocked:hourly_limit_exceeded" in second.reason_codes
        assert calls == 1
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_hourly_cycle_skips_deepseek_without_signal_or_position(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl")
    app = TradingApp(str(config_path))
    app.state.enable_report("ETH/USDT:USDT")

    candles = pd.DataFrame(
        {
            "open": [100.0] * 80,
            "high": [101.0] * 80,
            "low": [99.0] * 80,
            "close": [100.0] * 80,
            "volume": [1000.0] * 80,
        }
    )

    async def fake_news(live_news: bool) -> NewsDigest:
        return NewsDigest(summary="no major news")

    async def fake_refresh_exchange_safety(symbols: list[str]):  # noqa: ANN001
        return None

    async def fake_fetch_positions(symbols: list[str]):  # noqa: ANN001
        return [PositionSnapshot(symbol=symbols[0], side=Side.FLAT, qty=0.0, mark_price=100.0)]

    async def fake_fetch_ohlcv(symbol: str, timeframe: str, *args, **kwargs):  # noqa: ANN001
        return candles

    async def fake_fetch_summaries(symbol: str):  # noqa: ANN001
        return []

    async def fake_analyze_symbol(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("DeepSeek must not be called for a flat no-signal hourly cycle.")

    monkeypatch.setattr(app, "_news_for_trading_cycle", fake_news)
    monkeypatch.setattr(app, "_refresh_exchange_safety", fake_refresh_exchange_safety)
    monkeypatch.setattr(app, "_fetch_positions", fake_fetch_positions)
    monkeypatch.setattr(app.market, "fetch_ohlcv", fake_fetch_ohlcv)
    monkeypatch.setattr(app.orderflow_client, "fetch_summaries", fake_fetch_summaries)
    monkeypatch.setattr(app, "_generate_local_signal", lambda *args, **kwargs: make_signal(SignalAction.HOLD))
    monkeypatch.setattr(app.brain, "analyze_symbol", fake_analyze_symbol)

    try:
        await app.run_once(equity=1000.0, live_news=False)

        budget = app.store.fetch_payloads("ai_call_budget_events", symbol="ETH/USDT:USDT", limit=5)
        decisions = app.store.fetch_payloads("ai_decisions", symbol="ETH/USDT:USDT", limit=5)
        assert budget[0]["payload"]["status"] == "skipped"
        assert budget[0]["payload"]["call_type"] == "trading_cycle"
        assert budget[0]["payload"]["reason"] == "no_signal_no_position"
        assert decisions
        assert "deepseek_skipped:no_signal_no_position" in decisions[0]["payload"]["reason_codes"]
    finally:
        await app.close()
