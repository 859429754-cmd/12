from __future__ import annotations

import pandas as pd

from ai_quant_trader.monitoring.price import PriceWakeupMonitor


def test_price_monitor_wakes_on_one_minute_one_percent_move() -> None:
    prices = [100.0] * 89 + [101.2]
    candles = pd.DataFrame(
        {
            "open": prices,
            "high": [p * 1.001 for p in prices],
            "low": [p * 0.999 for p in prices],
            "close": prices,
            "volume": [100.0] * 90,
        }
    )
    event = PriceWakeupMonitor(threshold_pct=1.0).evaluate("ETH/USDT:USDT", candles)
    assert event is not None
    assert event.event_type == "price_move"
    assert event.raw["pct_1m"] > 1.0


def test_price_monitor_wakes_on_relative_volatility_spike() -> None:
    prices = [100.0 + i * 0.01 for i in range(89)] + [101.0]
    candles = pd.DataFrame(
        {
            "open": prices,
            "high": [p * 1.001 for p in prices],
            "low": [p * 0.999 for p in prices],
            "close": prices,
            "volume": [100.0] * 89 + [250.0],
        }
    )
    event = PriceWakeupMonitor(threshold_pct=1.5, volatility_multiplier=1.8, min_relative_move_pct=0.05).evaluate("BTC/USDT:USDT", candles)
    assert event is not None
    assert event.raw["relative_spike"] is True
