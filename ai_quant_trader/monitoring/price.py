from __future__ import annotations

import pandas as pd

from ai_quant_trader.brain.wakeup import WakeupEngine
from ai_quant_trader.core.models import WakeupEvent


class PriceWakeupMonitor:
    """实时价格异动检测。

    用 1m K 线检测短时间涨跌幅和相对历史均值波动。它只负责触发 AI 复核，
    不直接下单，避免分钟级噪声绕过主策略和风控。
    """

    def __init__(
        self,
        threshold_pct: float = 1.0,
        volatility_multiplier: float = 1.8,
        min_relative_move_pct: float = 0.45,
    ):
        self.threshold_pct = threshold_pct
        self.volatility_multiplier = volatility_multiplier
        self.min_relative_move_pct = min_relative_move_pct
        self.engine = WakeupEngine(price_move_1m_pct=threshold_pct, price_move_5m_pct=max(threshold_pct * 1.8, 1.5))

    def evaluate(self, symbol: str, candles_1m: pd.DataFrame) -> WakeupEvent | None:
        if len(candles_1m) < 10:
            return None
        close = candles_1m["close"].astype(float)
        volume = candles_1m["volume"].astype(float)
        pct_1m = (close.iloc[-1] / close.iloc[-2] - 1.0) * 100
        pct_5m = (close.iloc[-1] / close.iloc[-6] - 1.0) * 100 if len(close) >= 6 else pct_1m
        returns = close.pct_change().abs().dropna() * 100
        avg_abs_1m = float(returns.iloc[-61:-1].mean()) if len(returns) >= 62 else float(returns.mean())
        recent_volume = float(volume.iloc[-1])
        avg_volume = float(volume.iloc[-61:-1].mean()) if len(volume) >= 62 else float(volume.mean())
        volume_ratio = recent_volume / avg_volume if avg_volume > 0 else 1.0

        relative_spike = bool(
            avg_abs_1m > 0
            and abs(pct_1m) >= max(self.min_relative_move_pct, avg_abs_1m * self.volatility_multiplier)
        )
        absolute_spike = bool(abs(pct_1m) >= self.threshold_pct or abs(pct_5m) >= self.threshold_pct * 1.8)
        if not relative_spike and not absolute_spike and volume_ratio < 2.0:
            return None

        event = self.engine.event_from_price_move(symbol, pct_1m=pct_1m, pct_5m=pct_5m, volume_ratio=volume_ratio)
        if event:
            event.raw.update(
                {
                    "avg_abs_1m_pct": avg_abs_1m,
                    "relative_spike": relative_spike,
                    "absolute_spike": absolute_spike,
                    "last_close": float(close.iloc[-1]),
                }
            )
        return event
