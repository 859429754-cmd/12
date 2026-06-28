from __future__ import annotations

import pytest

from ai_quant_trader.core.models import Alignment, OrderflowSummary
from ai_quant_trader.data.orderflow import MultiExchangeOrderflowClient
from ai_quant_trader.features.orderflow import OrderflowAggregator


def test_orderflow_aggregates_weighted_sources() -> None:
    aggregator = OrderflowAggregator({"binance": 2, "okx": 1, "bybit": 1})
    result = aggregator.aggregate(
        "BTC/USDT:USDT",
        [
            OrderflowSummary(symbol="BTC/USDT:USDT", exchange="binance", bid_ask_imbalance=0.3, active_buy_sell_ratio=1.4, cvd_delta=10),
            OrderflowSummary(symbol="BTC/USDT:USDT", exchange="okx", bid_ask_imbalance=0.2, active_buy_sell_ratio=1.2, cvd_delta=5),
            OrderflowSummary(symbol="BTC/USDT:USDT", exchange="bybit", bid_ask_imbalance=0.1, active_buy_sell_ratio=1.1, cvd_delta=2),
        ],
    )
    assert result.source_count == 3
    assert result.alignment_hint == Alignment.ALIGNED
    assert result.bid_ask_imbalance > 0.2


@pytest.mark.asyncio
async def test_okx_orderflow_uses_public_rest_without_ccxt_market_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MultiExchangeOrderflowClient(["okx"], live_public_data=True)

    def fail_ccxt(name: str) -> None:
        raise AssertionError(f"ccxt should not be used for {name}")

    calls: list[tuple[str, dict[str, object]]] = []

    def fake_requests_json(url: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((url, params))
        if url.endswith("/books"):
            return {
                "data": [
                    {
                        "bids": [["1700", "2"], ["1699", "1"]],
                        "asks": [["1701", "3"]],
                    }
                ]
            }
        if url.endswith("/trades"):
            return {
                "data": [
                    {"px": "1701", "sz": "1.5", "side": "buy"},
                    {"px": "1700", "sz": "0.5", "side": "sell"},
                ]
            }
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(client, "_new_exchange", fail_ccxt)
    monkeypatch.setattr(client, "_requests_json", fake_requests_json, raising=False)

    summaries = await client.fetch_summaries("ETH/USDT:USDT")

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.exchange == "okx"
    assert summary.data_quality == 1.0
    assert summary.depth_usd > 0
    assert summary.active_buy_sell_ratio > 1.0
    assert [params["instId"] for _, params in calls] == ["ETH-USDT-SWAP", "ETH-USDT-SWAP"]


@pytest.mark.asyncio
async def test_failed_live_orderflow_source_is_neutral_degraded() -> None:
    class FailingExchange:
        async def fetch_order_book(self, symbol: str, limit: int) -> dict[str, object]:
            raise RuntimeError("public source down")

        async def fetch_trades(self, symbol: str, limit: int) -> list[dict[str, object]]:
            raise AssertionError("fetch_trades should not run after order book failure")

        async def close(self) -> None:
            return None

    class Client(MultiExchangeOrderflowClient):
        def _new_exchange(self, name: str) -> FailingExchange:
            return FailingExchange()

    client = Client(["binance"], live_public_data=True)

    summaries = await client.fetch_summaries("ETH/USDT:USDT")

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.exchange == "binance"
    assert summary.data_quality == 0.25
    assert summary.bid_ask_imbalance == 0.0
    assert summary.active_buy_sell_ratio == 1.0
    assert summary.cvd_delta == 0.0
