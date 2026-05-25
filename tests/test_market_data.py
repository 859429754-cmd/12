from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from ai_quant_trader.data.market import MarketDataClient


class StubMarketDataClient(MarketDataClient):
    def __init__(self, frame: pd.DataFrame, now_ms: int) -> None:
        super().__init__()
        self.frame = frame
        self.now_ms = now_ms

    def _now_ms(self) -> int:
        return self.now_ms

    async def fetch_ohlcv_history(self, **_kwargs):
        frame = self.frame.copy()
        frame.attrs["data_source"] = "stub"
        return frame


@pytest.mark.asyncio
async def test_latest_ohlcv_drops_unclosed_tail_by_default() -> None:
    now = datetime(2026, 5, 24, 2, 30, tzinfo=UTC)
    current_hour = datetime(2026, 5, 24, 2, 0, tzinfo=UTC)
    previous_hour = datetime(2026, 5, 24, 1, 0, tzinfo=UTC)
    frame = pd.DataFrame(
        {
            "timestamp": [previous_hour, current_hour],
            "open": [100.0, 110.0],
            "high": [105.0, 120.0],
            "low": [95.0, 108.0],
            "close": [102.0, 118.0],
            "volume": [10.0, 50.0],
        }
    )
    market = StubMarketDataClient(frame, int(now.timestamp() * 1000))

    candles = await market.fetch_ohlcv("ETH/USDT:USDT", "1h", limit=2)

    assert len(candles) == 1
    assert candles.iloc[-1]["timestamp"] == previous_hour
    assert candles.attrs["data_source"] == "stub"


@pytest.mark.asyncio
async def test_latest_ohlcv_can_keep_unclosed_tail_for_intrabar_monitors() -> None:
    now = datetime(2026, 5, 24, 2, 30, tzinfo=UTC)
    frame = pd.DataFrame(
        {
            "timestamp": [
                datetime(2026, 5, 24, 1, 0, tzinfo=UTC),
                datetime(2026, 5, 24, 2, 0, tzinfo=UTC),
            ],
            "open": [100.0, 110.0],
            "high": [105.0, 120.0],
            "low": [95.0, 108.0],
            "close": [102.0, 118.0],
            "volume": [10.0, 50.0],
        }
    )
    market = StubMarketDataClient(frame, int(now.timestamp() * 1000))

    candles = await market.fetch_ohlcv("ETH/USDT:USDT", "1h", limit=2, closed_only=False)

    assert len(candles) == 2
    assert candles.iloc[-1]["timestamp"] == datetime(2026, 5, 24, 2, 0, tzinfo=UTC)
