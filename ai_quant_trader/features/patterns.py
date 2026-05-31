from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import pandas as pd

from ai_quant_trader.core.models import PatternCandidate, Side


@dataclass(frozen=True)
class _LineFit:
    slope: float
    intercept: float
    r2: float
    values: list[float]


@dataclass(frozen=True)
class _Pivot:
    index: int
    price: float


class PatternDetector:
    """Auditable local chart-pattern detector.

    The detector produces evidence for DeepSeek and RiskManager context. It is
    not an entry signal and must not bypass the strategy signal contract.
    """

    _WINDOW = 90
    _MIN_BARS = 40

    def detect(self, symbol: str, candles: pd.DataFrame) -> PatternCandidate:
        if len(candles) < self._MIN_BARS:
            return PatternCandidate(symbol=symbol, pattern_type="insufficient_data", sample_bars=len(candles))
        self._validate(candles)

        recent = candles.tail(self._WINDOW).reset_index(drop=True)
        highs = recent["high"].astype(float)
        lows = recent["low"].astype(float)
        closes = recent["close"].astype(float)
        sample_bars = len(recent)

        reversal = self._detect_reversal(symbol, closes, highs, lows)
        if reversal is not None:
            return reversal.model_copy(update={"sample_bars": sample_bars})

        # Fit boundaries on completed structure, then extrapolate to the latest
        # bar. Otherwise a breakout candle lifts the boundary it is supposed to
        # break and hides the breakout from the detector.
        upper_fit = self._fit_line(highs.iloc[:-1])
        lower_fit = self._fit_line(lows.iloc[:-1])
        upper_line = self._extend_line(upper_fit, sample_bars)
        lower_line = self._extend_line(lower_fit, sample_bars)
        mean_price = max(float(closes.mean()), 1e-9)
        tolerance = self._line_tolerance(recent)
        upper_touches = self._touch_count(highs, upper_line.values, tolerance)
        lower_touches = self._touch_count(lows, lower_line.values, tolerance)
        upper_now = float(upper_line.values[-1])
        lower_now = float(lower_line.values[-1])
        current = float(closes.iloc[-1])

        upper_slope = upper_line.slope * (sample_bars - 1) / mean_price
        lower_slope = lower_line.slope * (sample_bars - 1) / mean_price
        width_start = max(float(upper_line.values[0] - lower_line.values[0]), 1e-9)
        width_now = max(float(upper_now - lower_now), 1e-9)
        width_ratio = width_now / width_start

        breakout_direction: Side | None = None
        breakout_buffer = min(tolerance * 0.35, mean_price * 0.0025)
        if current > upper_now + breakout_buffer:
            breakout_direction = Side.LONG
        elif current < lower_now - breakout_buffer:
            breakout_direction = Side.SHORT

        pattern_type, family, expected_direction, codes = self._classify_lines(
            upper_slope=upper_slope,
            lower_slope=lower_slope,
            width_ratio=width_ratio,
            upper_touches=upper_touches,
            lower_touches=lower_touches,
            breakout_direction=breakout_direction,
            recent=recent,
        )
        direction = breakout_direction or expected_direction
        confidence = self._confidence(
            pattern_type=pattern_type,
            upper_touches=upper_touches,
            lower_touches=lower_touches,
            width_ratio=width_ratio,
            breakout_direction=breakout_direction,
            upper_r2=upper_line.r2,
            lower_r2=lower_line.r2,
        )
        invalidation = lower_now if direction == Side.LONG else upper_now if direction == Side.SHORT else None

        return PatternCandidate(
            symbol=symbol,
            pattern_type=pattern_type,
            pattern_family=family,
            confidence=confidence,
            upper_boundary=upper_now,
            lower_boundary=lower_now,
            breakout_direction=direction,
            invalidation_price=invalidation,
            upper_slope=upper_slope,
            lower_slope=lower_slope,
            width_ratio=width_ratio,
            upper_touches=upper_touches,
            lower_touches=lower_touches,
            sample_bars=sample_bars,
            evidence_codes=codes,
        )

    def _validate(self, candles: pd.DataFrame) -> None:
        missing = {"high", "low", "close"} - set(candles.columns)
        if missing:
            raise ValueError(f"candles_missing_pattern_columns:{','.join(sorted(missing))}")

    def _classify_lines(
        self,
        *,
        upper_slope: float,
        lower_slope: float,
        width_ratio: float,
        upper_touches: int,
        lower_touches: int,
        breakout_direction: Side | None,
        recent: pd.DataFrame,
    ) -> tuple[str, str, Side | None, list[str]]:
        flat_eps = 0.025
        trend_eps = 0.035
        compression = width_ratio < 0.88
        expansion = width_ratio > 1.18
        upper_flat = abs(upper_slope) <= flat_eps
        lower_flat = abs(lower_slope) <= flat_eps
        upper_up = upper_slope > trend_eps
        upper_down = upper_slope < -trend_eps
        lower_up = lower_slope > trend_eps
        lower_down = lower_slope < -trend_eps
        enough_touches = upper_touches >= 2 and lower_touches >= 2
        codes = [
            f"upper_touches_{upper_touches}",
            f"lower_touches_{lower_touches}",
            f"width_ratio_{width_ratio:.2f}",
        ]
        if breakout_direction:
            codes.append(f"breakout_{breakout_direction.value}")
        if compression:
            codes.append("compression")
        if expansion:
            codes.append("expansion")

        flag = self._flag_pattern(recent, upper_slope, lower_slope, width_ratio)
        if flag is not None:
            pattern_type, direction = flag
            return pattern_type, "continuation", direction, [*codes, "impulse_flag_structure"]

        if upper_flat and lower_flat and enough_touches:
            if breakout_direction:
                return "rectangle_breakout", "breakout", breakout_direction, [*codes, "flat_range_breakout"]
            return "range_rectangle", "range", None, [*codes, "flat_range_rotation"]

        if compression and upper_flat and lower_up:
            return "ascending_triangle", "compression", Side.LONG, [*codes, "flat_resistance_rising_support"]
        if compression and upper_down and lower_flat:
            return "descending_triangle", "compression", Side.SHORT, [*codes, "falling_resistance_flat_support"]
        if compression and upper_down and lower_up:
            return "symmetrical_triangle", "compression", None, [*codes, "converging_resistance_support"]
        if compression and upper_up and lower_up and lower_slope > upper_slope:
            return "rising_wedge", "compression", Side.SHORT, [*codes, "rising_converging_wedge"]
        if compression and upper_down and lower_down and upper_slope < lower_slope:
            return "falling_wedge", "compression", Side.LONG, [*codes, "falling_converging_wedge"]

        parallel = abs(upper_slope - lower_slope) <= 0.04
        if parallel and upper_up and lower_up:
            return "ascending_channel", "channel", breakout_direction, [*codes, "parallel_rising_channel"]
        if parallel and upper_down and lower_down:
            return "descending_channel", "channel", breakout_direction, [*codes, "parallel_falling_channel"]

        if breakout_direction:
            return "generic_breakout", "breakout", breakout_direction, [*codes, "unclassified_line_breakout"]
        return "unknown", "unknown", None, [*codes, "no_stable_pattern"]

    def _detect_reversal(
        self,
        symbol: str,
        closes: pd.Series,
        highs: pd.Series,
        lows: pd.Series,
    ) -> PatternCandidate | None:
        high_pivots = self._pivots(highs, "high")
        low_pivots = self._pivots(lows, "low")
        double_top = self._double_top(symbol, closes, high_pivots, low_pivots)
        if double_top is not None:
            return double_top
        double_bottom = self._double_bottom(symbol, closes, high_pivots, low_pivots)
        if double_bottom is not None:
            return double_bottom
        head_shoulders = self._head_shoulders(symbol, closes, high_pivots, low_pivots)
        if head_shoulders is not None:
            return head_shoulders
        return None

    def _double_top(
        self,
        symbol: str,
        closes: pd.Series,
        high_pivots: list[_Pivot],
        low_pivots: list[_Pivot],
    ) -> PatternCandidate | None:
        if len(high_pivots) < 2 or not low_pivots:
            return None
        first, second = self._nearest_similar_pair(high_pivots, tolerance_pct=0.018, min_gap=5)
        if first is None or second is None:
            return None
        valley = self._lowest_between(low_pivots, first.index, second.index)
        if valley is None:
            return None
        peak = (first.price + second.price) / 2
        if peak <= valley.price * 1.035:
            return None
        if float(closes.iloc[-1]) > valley.price * 1.01:
            return None
        return PatternCandidate(
            symbol=symbol,
            pattern_type="double_top",
            pattern_family="reversal",
            confidence=0.72,
            upper_boundary=peak,
            lower_boundary=valley.price,
            breakout_direction=Side.SHORT,
            invalidation_price=max(first.price, second.price),
            upper_touches=2,
            lower_touches=1,
            evidence_codes=["double_top_two_similar_peaks", "neckline_breakdown"],
        )

    def _double_bottom(
        self,
        symbol: str,
        closes: pd.Series,
        high_pivots: list[_Pivot],
        low_pivots: list[_Pivot],
    ) -> PatternCandidate | None:
        if len(low_pivots) < 2 or not high_pivots:
            return None
        first, second = self._nearest_similar_pair(low_pivots, tolerance_pct=0.018, min_gap=5)
        if first is None or second is None:
            return None
        peak = self._highest_between(high_pivots, first.index, second.index)
        if peak is None:
            return None
        trough = (first.price + second.price) / 2
        if peak.price <= trough * 1.035:
            return None
        if float(closes.iloc[-1]) < peak.price * 0.99:
            return None
        return PatternCandidate(
            symbol=symbol,
            pattern_type="double_bottom",
            pattern_family="reversal",
            confidence=0.72,
            upper_boundary=peak.price,
            lower_boundary=trough,
            breakout_direction=Side.LONG,
            invalidation_price=min(first.price, second.price),
            upper_touches=1,
            lower_touches=2,
            evidence_codes=["double_bottom_two_similar_troughs", "neckline_breakout"],
        )

    def _head_shoulders(
        self,
        symbol: str,
        closes: pd.Series,
        high_pivots: list[_Pivot],
        low_pivots: list[_Pivot],
    ) -> PatternCandidate | None:
        if len(high_pivots) >= 3 and len(low_pivots) >= 2:
            left, head, right = high_pivots[-3:]
            shoulders_close = abs(left.price - right.price) / max((left.price + right.price) / 2, 1e-9) <= 0.03
            head_higher = head.price >= max(left.price, right.price) * 1.025
            neckline = self._lowest_between(low_pivots, left.index, right.index)
            if shoulders_close and head_higher and neckline is not None and float(closes.iloc[-1]) <= neckline.price * 1.005:
                return PatternCandidate(
                    symbol=symbol,
                    pattern_type="head_shoulders",
                    pattern_family="reversal",
                    confidence=0.66,
                    upper_boundary=head.price,
                    lower_boundary=neckline.price,
                    breakout_direction=Side.SHORT,
                    invalidation_price=head.price,
                    upper_touches=3,
                    lower_touches=2,
                    evidence_codes=["head_shoulders_three_peaks", "neckline_pressure"],
                )
        if len(low_pivots) >= 3 and len(high_pivots) >= 2:
            left, head, right = low_pivots[-3:]
            shoulders_close = abs(left.price - right.price) / max((left.price + right.price) / 2, 1e-9) <= 0.03
            head_lower = head.price <= min(left.price, right.price) * 0.975
            neckline = self._highest_between(high_pivots, left.index, right.index)
            if shoulders_close and head_lower and neckline is not None and float(closes.iloc[-1]) >= neckline.price * 0.995:
                return PatternCandidate(
                    symbol=symbol,
                    pattern_type="inverse_head_shoulders",
                    pattern_family="reversal",
                    confidence=0.66,
                    upper_boundary=neckline.price,
                    lower_boundary=head.price,
                    breakout_direction=Side.LONG,
                    invalidation_price=head.price,
                    upper_touches=2,
                    lower_touches=3,
                    evidence_codes=["inverse_head_shoulders_three_troughs", "neckline_pressure"],
                )
        return None

    def _flag_pattern(self, recent: pd.DataFrame, upper_slope: float, lower_slope: float, width_ratio: float) -> tuple[str, Side] | None:
        closes = recent["close"].astype(float).reset_index(drop=True)
        if len(closes) < 50:
            return None
        impulse = float(closes.iloc[-45] - closes.iloc[-65]) / max(float(closes.iloc[-65]), 1e-9) if len(closes) >= 66 else 0.0
        consolidation_return = float(closes.iloc[-1] - closes.iloc[-20]) / max(float(closes.iloc[-20]), 1e-9)
        parallel = abs(upper_slope - lower_slope) <= 0.04
        if not parallel or width_ratio > 1.25:
            return None
        if impulse >= 0.055 and consolidation_return <= 0.025:
            return "bull_flag", Side.LONG
        if impulse <= -0.055 and consolidation_return >= -0.025:
            return "bear_flag", Side.SHORT
        return None

    def _fit_line(self, values: pd.Series) -> _LineFit:
        y = [float(item) for item in values]
        n = len(y)
        x_mean = (n - 1) / 2
        y_mean = sum(y) / max(n, 1)
        denom = sum((idx - x_mean) ** 2 for idx in range(n)) or 1e-9
        slope = sum((idx - x_mean) * (price - y_mean) for idx, price in enumerate(y)) / denom
        intercept = y_mean - slope * x_mean
        fitted = [intercept + slope * idx for idx in range(n)]
        total = sum((price - y_mean) ** 2 for price in y)
        residual = sum((price - fitted[idx]) ** 2 for idx, price in enumerate(y))
        r2 = 1.0 - residual / total if total > 1e-9 else 1.0
        return _LineFit(slope=slope, intercept=intercept, r2=max(0.0, min(1.0, r2)), values=fitted)

    def _extend_line(self, fit: _LineFit, length: int) -> _LineFit:
        return _LineFit(
            slope=fit.slope,
            intercept=fit.intercept,
            r2=fit.r2,
            values=[fit.intercept + fit.slope * idx for idx in range(length)],
        )

    def _line_tolerance(self, candles: pd.DataFrame) -> float:
        high = candles["high"].astype(float)
        low = candles["low"].astype(float)
        close = candles["close"].astype(float)
        prev_close = close.shift(1).fillna(close)
        true_range = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        atr = float(true_range.tail(14).mean()) if len(true_range) else 0.0
        return max(atr * 0.32, float(close.tail(20).mean()) * 0.004)

    def _touch_count(self, values: pd.Series, line: list[float], tolerance: float) -> int:
        touches = [idx for idx, price in enumerate(values.astype(float)) if abs(float(price) - line[idx]) <= tolerance]
        clustered = 0
        last = -999
        for idx in touches:
            if idx - last >= 4:
                clustered += 1
                last = idx
        return clustered

    def _pivots(self, values: pd.Series, kind: Literal["high", "low"], left: int = 2, right: int = 2) -> list[_Pivot]:
        items = [float(item) for item in values]
        pivots: list[_Pivot] = []
        for idx in range(left, len(items) - right):
            window = items[idx - left : idx + right + 1]
            price = items[idx]
            if kind == "high" and price >= max(window):
                pivots.append(_Pivot(idx, price))
            if kind == "low" and price <= min(window):
                pivots.append(_Pivot(idx, price))
        return pivots[-8:]

    def _nearest_similar_pair(self, pivots: list[_Pivot], tolerance_pct: float, min_gap: int) -> tuple[_Pivot | None, _Pivot | None]:
        pairs: list[tuple[_Pivot, _Pivot]] = []
        for left_index, left in enumerate(pivots):
            for right in pivots[left_index + 1:]:
                if right.index - left.index < min_gap:
                    continue
                distance = abs(left.price - right.price) / max((left.price + right.price) / 2, 1e-9)
                if distance <= tolerance_pct:
                    pairs.append((left, right))
        if not pairs:
            return None, None
        return max(pairs, key=lambda pair: (pair[1].index, pair[0].index))

    def _lowest_between(self, pivots: list[_Pivot], start: int, end: int) -> _Pivot | None:
        candidates = [pivot for pivot in pivots if start < pivot.index < end]
        return min(candidates, key=lambda pivot: pivot.price, default=None)

    def _highest_between(self, pivots: list[_Pivot], start: int, end: int) -> _Pivot | None:
        candidates = [pivot for pivot in pivots if start < pivot.index < end]
        return max(candidates, key=lambda pivot: pivot.price, default=None)

    def _confidence(
        self,
        *,
        pattern_type: str,
        upper_touches: int,
        lower_touches: int,
        width_ratio: float,
        breakout_direction: Side | None,
        upper_r2: float,
        lower_r2: float,
    ) -> float:
        if pattern_type == "unknown":
            return 0.25
        base = 0.48
        if pattern_type in {"rectangle_breakout", "generic_breakout"}:
            base += 0.14
        if pattern_type in {"ascending_triangle", "descending_triangle", "symmetrical_triangle", "rising_wedge", "falling_wedge"}:
            base += 0.10
        base += min(0.16, (upper_touches + lower_touches) * 0.025)
        base += min(0.10, max(0.0, 1.0 - width_ratio) * 0.18)
        base += min(0.08, (upper_r2 + lower_r2) * 0.04)
        if breakout_direction:
            base += 0.06
        return max(0.0, min(0.88, base))
