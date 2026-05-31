from __future__ import annotations

import math

import pandas as pd

from ai_quant_trader.core.models import Side
from ai_quant_trader.features.patterns import PatternDetector


def _from_lines(
    upper: list[float],
    lower: list[float],
    close_bias: float = 0.5,
    breakout: float | None = None,
) -> pd.DataFrame:
    rows = []
    for idx, (top, bottom) in enumerate(zip(upper, lower, strict=True)):
        close = bottom + (top - bottom) * close_bias
        if idx % 10 == 3:
            close = top * 0.998
        if idx % 10 == 7:
            close = bottom * 1.002
        rows.append(
            {
                "open": close * 0.999,
                "high": top,
                "low": bottom,
                "close": close,
                "volume": 1000.0 + (idx % 5) * 10.0,
            }
        )
    if breakout is not None:
        rows[-1]["close"] = breakout
        rows[-1]["high"] = max(float(rows[-1]["high"]), breakout * 1.002)
        rows[-1]["low"] = min(float(rows[-1]["low"]), breakout * 0.998)
    return pd.DataFrame(rows)


def _line(start: float, end: float, count: int = 90) -> list[float]:
    return [start + (end - start) * idx / (count - 1) for idx in range(count)]


def test_pattern_detector_identifies_ascending_triangle_with_breakout_context() -> None:
    candles = _from_lines(_line(105.0, 105.2), _line(95.0, 102.0), breakout=106.2)

    result = PatternDetector().detect("ETH/USDT:USDT", candles)

    assert result.pattern_type == "ascending_triangle"
    assert result.pattern_family == "compression"
    assert result.breakout_direction == Side.LONG
    assert result.upper_touches >= 2
    assert result.lower_touches >= 2
    assert result.invalidation_price == result.lower_boundary


def test_pattern_detector_identifies_descending_triangle_with_breakout_context() -> None:
    candles = _from_lines(_line(106.0, 99.0), _line(95.0, 94.8), breakout=93.9)

    result = PatternDetector().detect("ETH/USDT:USDT", candles)

    assert result.pattern_type == "descending_triangle"
    assert result.pattern_family == "compression"
    assert result.breakout_direction == Side.SHORT
    assert result.upper_touches >= 2
    assert result.lower_touches >= 2
    assert result.invalidation_price == result.upper_boundary


def test_pattern_detector_identifies_rising_and_falling_wedges() -> None:
    rising = _from_lines(_line(105.0, 112.0), _line(95.0, 109.0))
    falling = _from_lines(_line(112.0, 98.0), _line(105.0, 95.0))

    rising_result = PatternDetector().detect("ETH/USDT:USDT", rising)
    falling_result = PatternDetector().detect("ETH/USDT:USDT", falling)

    assert rising_result.pattern_type == "rising_wedge"
    assert rising_result.breakout_direction == Side.SHORT
    assert falling_result.pattern_type == "falling_wedge"
    assert falling_result.breakout_direction == Side.LONG


def test_pattern_detector_promotes_rectangle_breakout() -> None:
    candles = _from_lines(_line(105.0, 105.1), _line(95.0, 94.9), breakout=106.0)

    result = PatternDetector().detect("ETH/USDT:USDT", candles)

    assert result.pattern_type == "rectangle_breakout"
    assert result.pattern_family == "breakout"
    assert result.breakout_direction == Side.LONG
    assert result.confidence >= 0.68


def test_pattern_detector_identifies_double_top_and_bottom_candidates() -> None:
    top_prices = [100 + math.sin(idx / 4) * 1.2 for idx in range(40)]
    top_prices += [105, 103, 100, 97, 100, 103, 105.2, 103, 99, 96.8, 96.2]
    bottom_prices = [100 + math.sin(idx / 4) * 1.2 for idx in range(40)]
    bottom_prices += [95, 97, 100, 103, 100, 97, 94.8, 97, 101, 103.4, 103.8]

    top_result = PatternDetector().detect("ETH/USDT:USDT", _ohlc_from_close(top_prices))
    bottom_result = PatternDetector().detect("ETH/USDT:USDT", _ohlc_from_close(bottom_prices))

    assert top_result.pattern_type == "double_top"
    assert top_result.breakout_direction == Side.SHORT
    assert bottom_result.pattern_type == "double_bottom"
    assert bottom_result.breakout_direction == Side.LONG


def _ohlc_from_close(prices: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [price * 0.999 for price in prices],
            "high": [price * 1.004 for price in prices],
            "low": [price * 0.996 for price in prices],
            "close": prices,
            "volume": [1000.0 for _ in prices],
        }
    )
