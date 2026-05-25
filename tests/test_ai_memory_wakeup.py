from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from ai_quant_trader.brain.knowledge import TradingKnowledgeBase
from ai_quant_trader.brain.wakeup import WakeupEngine
from ai_quant_trader.app import TradingApp
from ai_quant_trader.core.models import (
    AiDecision,
    AiCandidateTradePlan,
    Alignment,
    MacroEntity,
    MarketRegime,
    NewsDigest,
    NewsItem,
    SignalAction,
    Side,
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
    ema_length: 20
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
async def test_major_news_review_records_audit_without_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
            return []

        async def fake_analyze_symbol(signal, orderflow, dense_zone, pattern, news, regime_pattern=None):  # noqa: ANN001
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

        assert rows
        payload = rows[0]["payload"]
        assert payload["review_type"] == "major_news_risk_review"
        assert payload["no_order_submitted"] is True
        assert payload["signal"]["action"] == "hold"
        assert payload["signal"]["technical_evidence"]["major_news_context"] is True
        assert reviews
        assert reviews[0]["payload"]["event_key"] == payload["event_key"]
        assert payload["risk"]["allowed"] is False
        assert payload["risk"]["reason"] == "major_news_without_strategy_signal"
        assert orders == []
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
