from __future__ import annotations

from collections.abc import Mapping

from ai_quant_trader.core.models import AggregatedOrderflow, Alignment, OrderflowSummary


class OrderflowAggregator:
    """多交易所订单流共识聚合。

    只聚合公开行情摘要，不涉及任何外部交易所私钥或账户权限。
    """

    def __init__(self, weights: Mapping[str, float]):
        self.weights = dict(weights)

    def aggregate(self, symbol: str, summaries: list[OrderflowSummary]) -> AggregatedOrderflow:
        valid = [s for s in summaries if s.data_quality > 0]
        if not valid:
            return AggregatedOrderflow(
                symbol=symbol,
                alignment_hint=Alignment.UNKNOWN,
                warnings=["orderflow_unavailable"],
            )

        weighted: list[tuple[OrderflowSummary, float]] = []
        for item in valid:
            weight = self.weights.get(item.exchange, 1.0) * item.data_quality
            if weight > 0:
                weighted.append((item, weight))
        total_weight = sum(weight for _, weight in weighted)
        if total_weight <= 0:
            return AggregatedOrderflow(symbol=symbol, warnings=["orderflow_zero_weight"])

        def wavg(attr: str) -> float:
            return sum(getattr(item, attr) * weight for item, weight in weighted) / total_weight

        imbalance = wavg("bid_ask_imbalance")
        buy_sell_ratio = wavg("active_buy_sell_ratio")
        cvd_delta = wavg("cvd_delta")
        liquidity_shift = wavg("liquidity_shift")
        if imbalance > 0.12 and buy_sell_ratio > 1.1 and cvd_delta >= 0:
            alignment = Alignment.ALIGNED
        elif imbalance < -0.12 and buy_sell_ratio < 0.9 and cvd_delta <= 0:
            alignment = Alignment.CONFLICT
        else:
            alignment = Alignment.NEUTRAL

        return AggregatedOrderflow(
            symbol=symbol,
            bid_ask_imbalance=imbalance,
            active_buy_sell_ratio=buy_sell_ratio,
            cvd_delta=cvd_delta,
            spread_bps=wavg("spread_bps"),
            depth_usd=wavg("depth_usd"),
            large_trade_events=sum(item.large_trade_events for item, _ in weighted),
            liquidity_shift=liquidity_shift,
            alignment_hint=alignment,
            data_quality=min(1.0, sum(item.data_quality for item, _ in weighted) / len(weighted)),
            source_count=len(weighted),
            warnings=[] if len(weighted) >= 2 else ["low_orderflow_source_count"],
        )

