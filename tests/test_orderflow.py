from __future__ import annotations

from ai_quant_trader.core.models import Alignment, OrderflowSummary
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

