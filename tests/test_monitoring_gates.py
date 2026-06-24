from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from ai_quant_trader.core.models import AggregatedOrderflow, AiDecision, Alignment, HealthStatus, MarketRegime, NewsDigest, Side, VetoAction
from ai_quant_trader.monitoring.ai_drift import AIDriftMonitor
from ai_quant_trader.monitoring.data_health import DataHealthMonitor
from ai_quant_trader.storage.sqlite import SQLiteStore


def test_data_health_blocks_stale_ohlcv() -> None:
    candles = pd.DataFrame(
        [
            {
                "timestamp": datetime.now(UTC) - timedelta(hours=4),
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1.0,
            }
        ]
    )
    monitor = DataHealthMonitor(stale_data_seconds=300, news_max_age_hours=6)

    report = monitor.evaluate_symbol(
        symbol="ETH/USDT:USDT",
        timeframe="1h",
        candles=candles,
        news=NewsDigest(),
        orderflow=AggregatedOrderflow(symbol="ETH/USDT:USDT", data_quality=0.9, source_count=2),
    )

    assert report.status == HealthStatus.BLOCK
    assert report.can_open_new_entries is False
    assert "ohlcv_stale" in report.reason


def test_data_health_uses_candle_close_time_for_freshness() -> None:
    candles = pd.DataFrame(
        [
            {
                "timestamp": datetime.now(UTC) - timedelta(minutes=45),
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1.0,
            }
        ]
    )
    monitor = DataHealthMonitor(stale_data_seconds=300, news_max_age_hours=6)

    report = monitor.evaluate_symbol(
        symbol="ETH/USDT:USDT",
        timeframe="15m",
        candles=candles,
        news=NewsDigest(),
        orderflow=AggregatedOrderflow(symbol="ETH/USDT:USDT", data_quality=0.9, source_count=2),
    )

    assert report.status == HealthStatus.OK
    ohlcv_check = next(check for check in report.checks if check.name == "ohlcv")
    assert ohlcv_check.age_seconds is not None
    assert 25 * 60 <= ohlcv_check.age_seconds <= 31 * 60


def test_data_health_blocks_synthetic_ohlcv_source() -> None:
    candles = pd.DataFrame(
        [
            {
                "timestamp": datetime.now(UTC) - timedelta(hours=1),
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1.0,
            }
        ]
    )
    candles.attrs["data_source"] = "synthetic"
    candles.attrs["data_warning"] = "real exchanges unavailable"
    monitor = DataHealthMonitor(stale_data_seconds=300, news_max_age_hours=6)

    report = monitor.evaluate_symbol(
        symbol="ETH/USDT:USDT",
        timeframe="1h",
        candles=candles,
        news=NewsDigest(),
        orderflow=AggregatedOrderflow(symbol="ETH/USDT:USDT", data_quality=0.9, source_count=2),
    )

    assert report.status == HealthStatus.BLOCK
    assert report.can_open_new_entries is False
    assert "ohlcv_synthetic_source" in report.reason


def test_data_health_distinguishes_month_from_minute_timeframe() -> None:
    monitor = DataHealthMonitor(stale_data_seconds=300, news_max_age_hours=6)

    assert monitor._timeframe_seconds("15m") == 15 * 60
    assert monitor._timeframe_seconds("1M") == 30 * 24 * 60 * 60


def test_data_health_blocks_empty_orderflow() -> None:
    candles = pd.DataFrame(
        [
            {
                "timestamp": datetime.now(UTC),
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1.0,
            }
        ]
    )
    monitor = DataHealthMonitor(stale_data_seconds=300, news_max_age_hours=6)

    report = monitor.evaluate_symbol(
        symbol="ETH/USDT:USDT",
        timeframe="1h",
        candles=candles,
        news=NewsDigest(),
        orderflow=AggregatedOrderflow(symbol="ETH/USDT:USDT", data_quality=0.0, source_count=0),
    )

    assert report.status == HealthStatus.BLOCK
    assert "orderflow_source_empty" in report.reason


def make_ai(symbol: str, direction: Side, confidence: float, score: float) -> AiDecision:
    return AiDecision(
        symbol=symbol,
        regime=MarketRegime.TREND,
        direction=direction,
        confidence=confidence,
        multiplier=1.0,
        news_alignment=Alignment.ALIGNED,
        orderflow_alignment=Alignment.ALIGNED,
        trend_confirmation_score=score,
        range_risk_score=0.1,
        news_risk_score=0.1,
        orderflow_confirmation_score=score,
        dense_zone_breakout_score=score,
        veto_action=VetoAction.ALLOW,
    )


def test_ai_drift_blocks_extreme_direction_flip(tmp_path: Path) -> None:
    store = SQLiteStore(str(tmp_path / "trader.sqlite3"), str(tmp_path / "audit.jsonl"))
    symbol = "ETH/USDT:USDT"
    for _ in range(5):
        store.insert("ai_decisions", make_ai(symbol, Side.LONG, 0.82, 0.85), symbol)
    monitor = AIDriftMonitor(store, warn_delta=0.3, block_delta=0.55)

    report = monitor.evaluate(symbol, make_ai(symbol, Side.SHORT, 0.88, 0.1))

    assert report.status == HealthStatus.BLOCK
    assert report.reason == "ai_output_drift_block"
    assert report.baseline_direction == Side.LONG
    store.close()


def test_ai_drift_ignores_no_signal_fallback_decisions(tmp_path: Path) -> None:
    store = SQLiteStore(str(tmp_path / "trader.sqlite3"), str(tmp_path / "audit.jsonl"))
    symbol = "ETH/USDT:USDT"
    for _ in range(5):
        payload = make_ai(symbol, Side.LONG, 0.82, 0.85).model_dump(mode="json")
        payload["reason_codes"] = ["deepseek_skipped:no_signal_no_position", "fallback_conservative"]
        store.insert("ai_decisions", payload, symbol)
    monitor = AIDriftMonitor(store, warn_delta=0.3, block_delta=0.55)

    report = monitor.evaluate(symbol, make_ai(symbol, Side.SHORT, 0.88, 0.1))

    assert report.status == HealthStatus.OK
    assert report.reason == "ai_drift_insufficient_history"
    assert report.sample_size == 0
    store.close()


def test_ai_drift_ignores_flat_hold_decisions(tmp_path: Path) -> None:
    store = SQLiteStore(str(tmp_path / "trader.sqlite3"), str(tmp_path / "audit.jsonl"))
    symbol = "ETH/USDT:USDT"
    for _ in range(5):
        store.insert("ai_decisions", make_ai(symbol, Side.FLAT, 0.35, 0.1), symbol)
    monitor = AIDriftMonitor(store, warn_delta=0.3, block_delta=0.55)

    report = monitor.evaluate(symbol, make_ai(symbol, Side.SHORT, 0.65, 0.7))

    assert report.status == HealthStatus.OK
    assert report.reason == "ai_drift_insufficient_history"
    assert report.sample_size == 0
    store.close()


def test_ai_drift_ignores_nested_news_review_ai_payloads(tmp_path: Path) -> None:
    store = SQLiteStore(str(tmp_path / "trader.sqlite3"), str(tmp_path / "audit.jsonl"))
    symbol = "ETH/USDT:USDT"
    for _ in range(5):
        store.insert(
            "ai_decisions",
            {
                "review_type": "major_news_risk_review",
                "no_order_submitted": True,
                "ai": make_ai(symbol, Side.LONG, 0.82, 0.85).model_dump(mode="json"),
            },
            symbol,
        )
    monitor = AIDriftMonitor(store, warn_delta=0.3, block_delta=0.55)

    report = monitor.evaluate(symbol, make_ai(symbol, Side.SHORT, 0.88, 0.1))

    assert report.status == HealthStatus.OK
    assert report.reason == "ai_drift_insufficient_history"
    assert report.sample_size == 0
    store.close()
