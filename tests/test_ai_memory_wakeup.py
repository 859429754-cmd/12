from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from ai_quant_trader.brain.knowledge import TradingKnowledgeBase
from ai_quant_trader.brain.wakeup import WakeupEngine
from ai_quant_trader.app import TradingApp
from ai_quant_trader.core.models import (
    AggregatedOrderflow,
    AiDecision,
    AiCandidateTradePlan,
    Alignment,
    MacroEntity,
    MarketRegime,
    NewsDigest,
    NewsDirection,
    NewsItem,
    SignalAction,
    Side,
    StrategySignal,
    WakeupEvent,
    WakeupSeverity,
    VetoAction,
)
from ai_quant_trader.data.macro_entities import MacroEntityStore


def _write_config(path: Path, db_path: Path, audit_path: Path) -> None:
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
    kc_length: 10
    kc_scalar: 2.0
    vma_length: 10
    atr_length: 10
    volume_multiple: 1.5
ai:
  decision_model: "deepseek-v4-pro"
  report_model: "deepseek-v4-pro"
""",
        encoding="utf-8",
    )


def test_trading_knowledge_base_loads_context(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "risk_control.md").write_text("# 风控\n总杠杆不得超过4倍。", encoding="utf-8")
    kb = TradingKnowledgeBase(str(root))

    context = kb.build_context(["risk_control"])

    assert "总杠杆不得超过4倍" in context


def test_macro_entity_store_upsert_and_context(tmp_path: Path) -> None:
    store = MacroEntityStore(str(tmp_path / "entities.json"))
    store.upsert(
        MacroEntity(
            name="示例主席",
            role="Federal Reserve Chair",
            region="US",
            source="test",
            observed_at=datetime.now(UTC),
            confidence=0.9,
        )
    )

    context = store.context_text()

    assert "Federal Reserve Chair" in context
    assert "示例主席" in context


def test_wakeup_engine_news_and_price_triggers() -> None:
    digest = NewsDigest(
        items=[
            NewsItem(
                title="突发：美国政府关门风险升高",
                source="test",
                category="macro",
                summary="债务谈判失败导致政府关门风险升高。",
            )
        ]
    )
    engine = WakeupEngine()

    events = engine.events_from_news(digest)
    price_event = engine.event_from_price_move("BTC/USDT:USDT", pct_1m=1.0, pct_5m=2.5, volume_ratio=2.2)

    assert events
    assert events[0].severity in {WakeupSeverity.HIGH, WakeupSeverity.CRITICAL}
    assert price_event is not None
    assert price_event.should_escalate_to_pro is True


@pytest.mark.asyncio
async def test_major_news_review_skips_deepseek_without_signal_or_position(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl")
    app = TradingApp(str(config_path))
    try:
        app.state.enable_report("ETH/USDT:USDT")

        closes = [100.0 + index * 0.1 for index in range(80)]
        candles = pd.DataFrame(
            {
                "open": closes,
                "high": [price + 1.0 for price in closes],
                "low": [price - 1.0 for price in closes],
                "close": closes,
                "volume": [1000.0 for _ in closes],
            }
        )

        async def fake_fetch_ohlcv(symbol: str, timeframe: str, *args, **kwargs):  # noqa: ANN001
            return candles

        async def fake_fetch_positions(symbols: list[str]):  # noqa: ANN001
            return []

        async def fake_fetch_summaries(symbol: str):  # noqa: ANN001
            raise AssertionError("Orderflow fetch must not run when major news review is locally skipped.")

        async def fake_analyze_symbol(signal, orderflow, dense_zone, pattern, news, regime_pattern=None, **kwargs):  # noqa: ANN001, ANN003
            assert signal.action == SignalAction.HOLD
            assert signal.technical_evidence["news_risk_review"] is True
            return AiDecision(
                symbol=signal.symbol,
                regime=MarketRegime.UNCERTAIN,
                direction=Side.FLAT,
                confidence=0.35,
                multiplier=0.5,
                news_alignment=Alignment.CONFLICT,
                orderflow_alignment=Alignment.UNKNOWN,
                trend_confirmation_score=0.2,
                range_risk_score=0.7,
                news_risk_score=0.9,
                orderflow_confirmation_score=0.2,
                dense_zone_breakout_score=0.2,
                action_suggestion="hold",
                veto_action=VetoAction.BLOCK,
                brief_reason="重大新闻风险复评，只落库不下单。",
            )

        monkeypatch.setattr(app.market, "fetch_ohlcv", fake_fetch_ohlcv)
        monkeypatch.setattr(app, "_fetch_positions", fake_fetch_positions)
        monkeypatch.setattr(app.orderflow_client, "fetch_summaries", fake_fetch_summaries)
        monkeypatch.setattr(app.brain, "analyze_symbol", fake_analyze_symbol)

        digest = NewsDigest(
            items=[
                NewsItem(
                    title="突发：交易所暂停提现引发市场风险",
                    source="test",
                    category="crypto",
                    summary="市场出现交易所风险事件。",
                )
            ]
        )

        await app._handle_news_risk_reviews(digest)

        rows = app.store.fetch_payloads("ai_decisions", symbol="ETH/USDT:USDT", limit=5)
        reviews = app.store.fetch_payloads("news_risk_reviews", symbol="ETH/USDT:USDT", limit=5)
        orders = app.store.fetch_payloads("orders", limit=5)

        assert rows == []
        assert reviews
        payload = reviews[0]["payload"]
        assert payload["review_type"] == "major_news_risk_review"
        assert payload["status"] == "skipped"
        assert payload["deepseek_called"] is False
        assert payload["skip_reason"] == "no_signal_no_position"
        assert payload["no_order_submitted"] is True
        assert payload["signal"]["action"] == "hold"
        assert payload["signal"]["technical_evidence"]["major_news_context"] is True
        assert reviews[0]["payload"]["event_key"] == payload["event_key"]
        assert payload["risk"]["allowed"] is False
        assert payload["risk"]["reason"] == "major_news_without_strategy_signal"
        assert orders == []
        budget = app.store.fetch_payloads("ai_call_budget_events", symbol="ETH/USDT:USDT", limit=5)
        assert budget[0]["payload"]["status"] == "skipped"
        assert budget[0]["payload"]["call_type"] == "major_news_risk_review"
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_reload_runtime_config_refreshes_deepseek_credentials_from_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_BACKUP_API_KEY", raising=False)
    app = TradingApp(str(config_path))
    try:
        app.brain.api_key = None
        app.brain.backup_api_key = None
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-primary-after-reload")
        monkeypatch.setenv("DEEPSEEK_BACKUP_API_KEY", "sk-backup-after-reload")

        await app.reload_runtime_config()

        assert app.brain.api_key == "sk-primary-after-reload"
        assert app.brain.backup_api_key == "sk-backup-after-reload"
        assert app.brain.base_url == app.config.ai.base_url.rstrip("/")
        assert app.brain.model == app.config.ai.decision_model
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_major_news_review_with_strategy_signal_stays_out_of_trade_decisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl")
    app = TradingApp(str(config_path))
    try:
        candles = pd.DataFrame(
            {
                "open": [100.0] * 80,
                "high": [102.0] * 80,
                "low": [98.0] * 80,
                "close": [100.0] * 80,
                "volume": [1000.0] * 80,
            }
        )

        async def fake_fetch_ohlcv(symbol: str, timeframe: str, *args, **kwargs):  # noqa: ANN001
            return candles

        async def fake_fetch_positions(symbols: list[str]):  # noqa: ANN001
            return []

        def fake_generate_signal(symbol, timeframe, candles_arg, position, equity):  # noqa: ANN001
            return StrategySignal(
                symbol=symbol,
                timeframe=timeframe,
                action=SignalAction.SHORT,
                current_price=100.0,
                suggested_qty=1.0,
                signal_strength=0.9,
                technical_evidence={"short_condition": True},
            )

        async def fake_fetch_summaries(symbol: str):  # noqa: ANN001
            return []

        def fake_aggregate(symbol, summaries):  # noqa: ANN001
            return AggregatedOrderflow(symbol=symbol, alignment_hint=Alignment.ALIGNED, data_quality=0.9, source_count=2)

        async def fake_analyze_symbol(signal, orderflow, dense_zone, pattern, news, regime_pattern=None, **kwargs):  # noqa: ANN001, ANN003
            assert signal.action == SignalAction.HOLD
            assert signal.technical_evidence["original_strategy_action"] == "short"
            assert news.news_direction == NewsDirection.UNKNOWN
            assert news.crypto_sentiment == Alignment.UNKNOWN
            return AiDecision(
                symbol=signal.symbol,
                regime=MarketRegime.TREND,
                direction=Side.SHORT,
                confidence=0.6,
                multiplier=0.5,
                news_alignment=Alignment.ALIGNED,
                orderflow_alignment=Alignment.ALIGNED,
                trend_confirmation_score=0.7,
                range_risk_score=0.4,
                news_risk_score=0.8,
                orderflow_confirmation_score=0.7,
                dense_zone_breakout_score=0.5,
                action_suggestion="reduce",
                veto_action=VetoAction.REDUCE,
                brief_reason="重大新闻复评仅审计，不提交订单。",
            )

        monkeypatch.setattr(app.market, "fetch_ohlcv", fake_fetch_ohlcv)
        monkeypatch.setattr(app, "_fetch_positions", fake_fetch_positions)
        monkeypatch.setattr(app, "_generate_local_signal", fake_generate_signal)
        monkeypatch.setattr(app.orderflow_client, "fetch_summaries", fake_fetch_summaries)
        monkeypatch.setattr(app.orderflow_aggregator, "aggregate", fake_aggregate)
        monkeypatch.setattr(app.brain, "analyze_symbol", fake_analyze_symbol)

        await app._review_major_news_for_symbol(
            WakeupEvent(
                event_type="news",
                severity=WakeupSeverity.HIGH,
                title="突发：宏观风险资产遭遇利空",
                summary="该消息偏空风险资产。",
                source="test",
            ),
            "ETH/USDT:USDT",
            "1h",
        )

        assert app.store.fetch_payloads("ai_decisions", symbol="ETH/USDT:USDT", limit=5) == []
        reviews = app.store.fetch_payloads("news_risk_reviews", symbol="ETH/USDT:USDT", limit=5)
        assert reviews
        assert reviews[0]["payload"]["review_type"] == "major_news_risk_review"
        assert reviews[0]["payload"]["no_order_submitted"] is True
    finally:
        await app.close()


def test_ai_candidate_trade_plan_requires_approval() -> None:
    plan = AiCandidateTradePlan(
        symbol="ETH/USDT:USDT",
        direction=Side.LONG,
        confidence=0.72,
        entry_zone_low=3000,
        entry_zone_high=3020,
        tp_estimate=3150,
        sl_estimate=2940,
        expected_regime=MarketRegime.TREND,
        trigger_evidence=["订单流同向", "突破VAH", "成交量放大"],
    )

    assert plan.approval_required is True
    assert plan.status == "pending"
