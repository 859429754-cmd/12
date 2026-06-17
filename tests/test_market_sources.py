from __future__ import annotations

import pandas as pd
import pytest

from ai_quant_trader.data.market import AUTO_MARKET_SOURCES, MarketDataClient


def test_auto_market_source_priority_prefers_top_liquidity_exchanges() -> None:
    assert AUTO_MARKET_SOURCES == ["binance", "okx", "bybit", "gateio"]


@pytest.mark.asyncio
async def test_fetch_ticker_auto_falls_through_to_bybit_before_gate() -> None:
    calls: list[str] = []

    class Client(MarketDataClient):
        async def _fetch_public_ticker(self, source: str, symbol: str) -> dict:
            calls.append(source)
            if source in {"binance", "okx"}:
                raise RuntimeError(f"{source}_down")
            return {
                "symbol": symbol,
                "source": source,
                "last": 2500.0,
                "bid": 2499.0,
                "ask": 2501.0,
                "mark": 2500.0,
                "index": 2500.0,
                "timestamp": "2026-01-01T00:00:00+00:00",
                "warning": "",
            }

    ticker = await Client().fetch_ticker("ETH/USDT:USDT")

    assert ticker["source"] == "bybit"
    assert calls == ["binance", "okx", "bybit"]


@pytest.mark.asyncio
async def test_fetch_history_auto_falls_through_to_bybit_before_gate() -> None:
    calls: list[str] = []

    class Client(MarketDataClient):
        async def _fetch_from_exchange(
            self,
            source: str,
            symbol: str,
            timeframe: str,
            start_ms: int,
            end_ms: int,
            limit_per_call: int,
            max_candles: int,
        ) -> pd.DataFrame:
            calls.append(source)
            if source in {"binance", "okx"}:
                raise RuntimeError(f"{source}_down")
            return pd.DataFrame(
                [
                    {
                        "timestamp": pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(hours=i),
                        "open": 100.0 + i,
                        "high": 101.0 + i,
                        "low": 99.0 + i,
                        "close": 100.5 + i,
                        "volume": 10.0,
                    }
                    for i in range(12)
                ]
            )

    frame = await Client().fetch_ohlcv_history("ETH/USDT:USDT", source="auto", max_candles=12)

    assert frame.attrs["data_source"] == "bybit"
    assert calls == ["binance", "okx", "bybit"]


@pytest.mark.asyncio
async def test_bybit_kline_rest_payload_parses_chronologically() -> None:
    client = MarketDataClient()

    def fake_payload(url: str, params: dict) -> dict:
        assert url == "https://api.bybit.com/v5/market/kline"
        assert params["category"] == "linear"
        assert params["symbol"] == "ETHUSDT"
        assert params["interval"] == "60"
        return {
            "retCode": 0,
            "result": {
                "list": [
                    ["3600000", "101", "102", "100", "101.5", "12", "1218"],
                    ["0", "100", "101", "99", "100.5", "10", "1005"],
                ]
            },
        }

    client._requests_payload = fake_payload  # type: ignore[method-assign]

    frame = await client._fetch_bybit_rest(
        "ETH/USDT:USDT",
        "1h",
        start_ms=0,
        end_ms=7_200_000,
        limit_per_call=10,
        max_candles=10,
    )

    assert frame["open"].tolist() == [100.0, 101.0]
    assert frame["close"].tolist() == [100.5, 101.5]
