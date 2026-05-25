from __future__ import annotations

import pandas as pd

from ai_quant_trader.core.models import DenseZone, PatternCandidate, Side, SignalAction, StrategySignal
from ai_quant_trader.features.regime import RegimePatternAnalyzer


def _candles(prices: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    volumes = volumes or [1000.0 for _ in prices]
    return pd.DataFrame(
        {
            "open": prices,
            "high": [price * 1.004 for price in prices],
            "low": [price * 0.996 for price in prices],
            "close": prices,
            "volume": volumes,
        }
    )


def test_regime_pattern_routes_repeated_dense_zone_rotation_to_range() -> None:
    prices = [100.0 + ((idx % 12) - 6) * 0.35 for idx in range(160)]
    candles = _candles(prices)
    zone = DenseZone(
        symbol="ETH/USDT:USDT",
        poc=100.0,
        vah=102.5,
        val=97.5,
        zone_low=97.5,
        zone_high=102.5,
        zone_mid=100.0,
        current_position="inside_value",
        breakout_status="inside_zone",
        range_score=0.82,
        trend_score=0.18,
        structure_label="range_rotation",
    )
    pattern = PatternCandidate(symbol="ETH/USDT:USDT", pattern_type="range_rectangle", confidence=0.72)

    result = RegimePatternAnalyzer().analyze("ETH/USDT:USDT", candles, zone, pattern)

    assert result.regime_candidate == "range"
    assert result.strategy_allowed == "range"
    assert result.range_score > result.trend_score
    assert "range_score_dominant" in result.reason_codes


def test_regime_pattern_routes_confirmed_dense_zone_breakout_to_trend() -> None:
    base = [100.0 + (idx % 5) * 0.25 for idx in range(140)]
    breakout = [103.2, 103.8, 104.5, 105.4, 106.6, 107.2]
    prices = base + breakout
    volumes = [1000.0 for _ in base] + [2600.0 for _ in breakout]
    zone = DenseZone(
        symbol="BTC/USDT:USDT",
        poc=100.5,
        vah=102.0,
        val=99.0,
        zone_low=99.0,
        zone_high=102.0,
        zone_mid=100.5,
        current_position="above_value",
        breakout_status="breakout_up",
        range_score=0.10,
        trend_score=0.86,
        structure_label="breakout_up",
    )
    pattern = PatternCandidate(
        symbol="BTC/USDT:USDT",
        pattern_type="rectangle_breakout",
        confidence=0.76,
        breakout_direction=Side.LONG,
    )

    result = RegimePatternAnalyzer().analyze("BTC/USDT:USDT", _candles(prices, volumes), zone, pattern)

    assert result.regime_candidate == "trend"
    assert result.strategy_allowed == "trend"
    assert result.breakout_quality == "strong"
    assert result.trend_score > result.range_score


def test_regime_pattern_enriches_strategy_signal_for_ai_and_risk() -> None:
    signal = StrategySignal(
        symbol="SOL/USDT:USDT",
        timeframe="1h",
        action=SignalAction.LONG,
        current_price=100.0,
        suggested_qty=1.0,
        signal_strength=0.8,
    )
    regime = RegimePatternAnalyzer().analyze(
        "SOL/USDT:USDT",
        _candles([100.0 + ((idx % 8) - 4) * 0.25 for idx in range(140)]),
        DenseZone(
            symbol="SOL/USDT:USDT",
            poc=100.0,
            vah=102.0,
            val=98.0,
            zone_low=98.0,
            zone_high=102.0,
            zone_mid=100.0,
            current_position="inside_value",
            breakout_status="inside_zone",
            range_score=0.80,
            trend_score=0.20,
        ),
        PatternCandidate(symbol="SOL/USDT:USDT", pattern_type="range_rectangle", confidence=0.7),
    )

    enriched = RegimePatternAnalyzer().enrich_signal(signal, regime)

    assert enriched.technical_evidence["strategy_allowed"] == "range"
    assert enriched.technical_evidence["regime_candidate"] == "range"
    assert enriched.technical_evidence["regime_pattern_family"] == "range_rotation"
