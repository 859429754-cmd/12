from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from ai_quant_trader.core.models import PositionSnapshot, StrategySignal


class BaseStrategy(ABC):
    """策略基类。

    新策略只需要实现 `generate_signal`，并返回结构化候选信号。
    真正能否下单由 AI 共识与风控层决定。
    """

    @abstractmethod
    def generate_signal(
        self,
        symbol: str,
        timeframe: str,
        candles: pd.DataFrame,
        position: PositionSnapshot,
        equity: float,
        ai_multiplier: float,
    ) -> StrategySignal:
        raise NotImplementedError

