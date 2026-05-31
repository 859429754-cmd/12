from __future__ import annotations

import pandas as pd

from ai_quant_trader.core.models import PositionSnapshot, SignalAction, StrategySignal
from ai_quant_trader.strategy.base import BaseStrategy


class EmptyRangeStrategy(BaseStrategy):
    """震荡策略占位。

    第一版不主动交易震荡行情，后续可以在这里接入箱体高抛低吸等策略。
    """

    def generate_signal(
        self,
        symbol: str,
        timeframe: str,
        candles: pd.DataFrame,
        position: PositionSnapshot,
        equity: float,
        ai_multiplier: float,
        leverage: float = 4.0,
    ) -> StrategySignal:
        price = float(candles["close"].iloc[-1]) if len(candles) else 0.0
        return StrategySignal(
            symbol=symbol,
            timeframe=timeframe,
            action=SignalAction.HOLD,
            current_price=price,
            technical_evidence={"reason": "range_strategy_not_enabled"},
        )
