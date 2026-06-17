from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_quant_trader.brain.deepseek import DeepSeekBrain
from ai_quant_trader.core.models import (
    AggregatedOrderflow,
    Alignment,
    DenseZone,
    NewsDigest,
    NewsDirection,
    NewsItem,
    PatternCandidate,
    SignalAction,
    StrategySignal,
)
from ai_quant_trader.data.news_context import MarketNewsContextBuilder
from ai_quant_trader.storage.sqlite import SQLiteStore


def _store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(str(tmp_path / "trader.sqlite3"), str(tmp_path / "audit.jsonl"))


def _bearish_macro_digest(now: datetime) -> NewsDigest:
    return NewsDigest(
        items=[
            NewsItem(
                title="Fed signals higher rates as CPI comes in above expectations",
                source="test-wire",
                published_at=now - timedelta(hours=2),
                category="macro",
                credibility=0.95,
                summary="Powell said inflation remains too high and policy may stay restrictive.",
            )
        ],
        summary="Fed policy risk is back in focus.",
    )


def test_market_background_persists_high_impact_news_for_later_trading_cycle(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        now = datetime(2026, 6, 15, 12, tzinfo=UTC)
        builder = MarketNewsContextBuilder(store)

        first = builder.update_digest(_bearish_macro_digest(now), now=now)
        assert first.market_background is not None
        assert first.news_direction == NewsDirection.BEARISH
        assert first.market_background.risk_level == "high"
        assert store.fetch_payloads("news_events", limit=10)
        assert store.fetch_payloads("market_background_snapshots", limit=10)

        later = builder.attach_latest_background(
            NewsDigest(
                items=[
                    NewsItem(
                        title="Minor equity market update",
                        source="test-wire",
                        published_at=now + timedelta(hours=5),
                        category="market",
                        credibility=0.5,
                    )
                ],
                summary="No major realtime crypto news.",
            ),
            now=now + timedelta(hours=6),
        )

        assert later.market_background is not None
        assert later.news_direction == NewsDirection.BEARISH
        assert any("higher rates" in event.title for event in later.market_background.active_events)
        assert "Market background:" in later.summary
    finally:
        store.close()


def test_bearish_market_background_aligns_with_short_signal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        now = datetime(2026, 6, 15, 12, tzinfo=UTC)
        digest = MarketNewsContextBuilder(store).update_digest(_bearish_macro_digest(now), now=now)
        brain = DeepSeekBrain(model="deepseek-v4-flash")
        short_signal = StrategySignal(
            symbol="ETH/USDT:USDT",
            timeframe="1h",
            action=SignalAction.SHORT,
            current_price=1800.0,
        )
        long_signal = short_signal.model_copy(update={"action": SignalAction.LONG})

        assert brain._news_alignment_for_signal(digest, short_signal) == Alignment.ALIGNED
        assert brain._news_alignment_for_signal(digest, long_signal) == Alignment.CONFLICT
    finally:
        store.close()


def test_deepseek_payload_splits_market_background_from_realtime_news(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        now = datetime(2026, 6, 15, 12, tzinfo=UTC)
        digest = MarketNewsContextBuilder(store).update_digest(_bearish_macro_digest(now), now=now)
        brain = DeepSeekBrain(model="deepseek-v4-flash")
        signal = StrategySignal(
            symbol="ETH/USDT:USDT",
            timeframe="1h",
            action=SignalAction.SHORT,
            current_price=1800.0,
        )
        payload = brain._build_payload(
            signal,
            AggregatedOrderflow(symbol="ETH/USDT:USDT", alignment_hint=Alignment.ALIGNED, data_quality=0.8, source_count=2),
            DenseZone(symbol="ETH/USDT:USDT", poc=1800, vah=1820, val=1780),
            PatternCandidate(symbol="ETH/USDT:USDT"),
            digest,
        )
        compact = brain._compact_payload(payload)

        assert compact["market_background"]["background_direction"] == "bearish"
        assert compact["market_background"]["active_events"]
        assert "market_background" not in compact["news"]
        assert "active_news_events" not in compact["news"]
        assert compact["news_strategy_alignment_hint"] == Alignment.ALIGNED
    finally:
        store.close()
