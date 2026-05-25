from __future__ import annotations

import pandas as pd

from ai_quant_trader.features.dense_zone import DenseZoneAnalyzer


def _candles(prices: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    volumes = volumes or [1000.0 for _ in prices]
    return pd.DataFrame(
        {
            "open": prices,
            "high": [price * 1.006 for price in prices],
            "low": [price * 0.994 for price in prices],
            "close": prices,
            "volume": volumes,
        }
    )


def test_dense_zone_marks_inside_range_structure() -> None:
    prices = [98 + (idx % 8) * 0.55 for idx in range(220)]
    prices += [99.0, 100.5, 102.0, 101.2, 100.2, 99.4, 100.8, 101.6, 100.4, 99.8]
    volumes = [3000.0 if 98 <= price <= 103 else 500.0 for price in prices]

    zone = DenseZoneAnalyzer().calculate("ETH/USDT:USDT", _candles(prices, volumes))

    assert zone.zone_low is not None
    assert zone.zone_high is not None
    assert zone.zone_low <= prices[-1] <= zone.zone_high
    assert zone.range_score > zone.trend_score
    assert "震荡" in zone.structure_label
    assert zone.breakout_status == "inside_zone"


def test_dense_zone_marks_vacuum_trend_between_zones() -> None:
    lower_prices = [100 + (idx % 5) * 0.5 for idx in range(120)]
    vacuum_prices = [112 + idx * 0.12 for idx in range(35)]
    upper_prices = [126 + (idx % 5) * 0.5 for idx in range(80)]
    prices = lower_prices + upper_prices + vacuum_prices[-20:]
    volumes = [4000.0 for _ in lower_prices] + [4200.0 for _ in upper_prices] + [300.0 for _ in vacuum_prices[-20:]]

    zone = DenseZoneAnalyzer().calculate("BTC/USDT:USDT", _candles(prices, volumes))

    assert zone.previous_zone_high is not None
    assert zone.next_zone_low is not None
    assert zone.vacuum_low is not None
    assert zone.vacuum_high is not None
    assert zone.breakout_status in {"vacuum_travel", "retest_support", "retest_resistance"}
    assert zone.trend_score > zone.range_score
    assert "真空区" in zone.structure_label or "回踩" in zone.structure_label
