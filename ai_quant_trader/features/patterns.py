from __future__ import annotations

import pandas as pd

from ai_quant_trader.core.models import PatternCandidate, Side


class PatternDetector:
    """轻量形态识别器。

    第一版输出 AI 可读的候选形态，不把形态识别作为硬交易信号。
    """

    def detect(self, symbol: str, candles: pd.DataFrame) -> PatternCandidate:
        if len(candles) < 30:
            return PatternCandidate(symbol=symbol, pattern_type="insufficient_data")

        recent = candles.tail(60)
        highs = recent["high"]
        lows = recent["low"]
        closes = recent["close"]
        high_slope = (float(highs.iloc[-1]) - float(highs.iloc[0])) / max(float(highs.iloc[0]), 1e-9)
        low_slope = (float(lows.iloc[-1]) - float(lows.iloc[0])) / max(float(lows.iloc[0]), 1e-9)
        width_now = float(highs.tail(10).max() - lows.tail(10).min())
        width_then = float(highs.head(10).max() - lows.head(10).min())
        width_ratio = width_now / max(width_then, 1e-9)
        upper = float(highs.tail(20).max())
        lower = float(lows.tail(20).min())
        current = float(closes.iloc[-1])

        pattern = "range_rectangle"
        confidence = 0.45
        breakout = None

        if width_ratio < 0.72:
            if high_slope < -0.01 and low_slope > 0.01:
                pattern = "symmetrical_triangle"
                confidence = 0.68
            elif abs(high_slope) < 0.01 and low_slope > 0.01:
                pattern = "ascending_triangle"
                confidence = 0.63
                breakout = Side.LONG
            elif high_slope < -0.01 and abs(low_slope) < 0.01:
                pattern = "descending_triangle"
                confidence = 0.63
                breakout = Side.SHORT
            elif high_slope < -0.01 and low_slope < -0.01:
                pattern = "falling_wedge"
                confidence = 0.58
                breakout = Side.LONG
            elif high_slope > 0.01 and low_slope > 0.01:
                pattern = "rising_wedge"
                confidence = 0.58
                breakout = Side.SHORT
        elif width_ratio < 1.15:
            pattern = "range_rectangle"
            confidence = 0.55

        if current > upper:
            breakout = Side.LONG
        elif current < lower:
            breakout = Side.SHORT

        return PatternCandidate(
            symbol=symbol,
            pattern_type=pattern,
            confidence=confidence,
            upper_boundary=upper,
            lower_boundary=lower,
            breakout_direction=breakout,
            invalidation_price=lower if breakout == Side.LONG else upper if breakout == Side.SHORT else None,
        )

