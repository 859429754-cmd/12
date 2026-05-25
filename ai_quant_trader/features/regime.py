from __future__ import annotations

import math

import pandas as pd

from ai_quant_trader.core.models import DenseZone, PatternCandidate, RegimePattern, Side


class RegimePatternAnalyzer:
    """Classify whether the current structure should allow trend or range strategies.

    This is a local, auditable gate before DeepSeek. It does not predict direction and
    does not create orders; it decides which strategy family is allowed to participate.
    """

    def analyze(
        self,
        symbol: str,
        candles: pd.DataFrame,
        dense_zone: DenseZone,
        pattern: PatternCandidate,
    ) -> RegimePattern:
        if len(candles) < 120:
            return RegimePattern(
                symbol=symbol,
                regime_candidate="unknown",
                strategy_allowed="none",
                pattern_family="insufficient_data",
                pattern_name=pattern.pattern_type,
                breakout_quality="unknown",
                risk_score=0.65,
                reason_codes=["insufficient_regime_history"],
            )

        recent = candles.tail(120).copy()
        close = recent["close"].astype(float)
        high = recent["high"].astype(float)
        low = recent["low"].astype(float)
        volume = recent["volume"].astype(float)
        current = float(close.iloc[-1])

        atr14 = self._atr(recent, 14)
        atr_now = float(atr14.iloc[-1]) if not math.isnan(float(atr14.iloc[-1])) else 0.0
        atr_base = float(atr14.tail(60).median()) if len(atr14.dropna()) else 0.0
        atr_expansion = self._clip((atr_now / max(atr_base, 1e-9) - 1.0) / 0.8)

        kc_width = self._kc_width_pct(recent)
        kc_width_now = kc_width.iloc[-1] if len(kc_width) else 0.0
        kc_width_base = float(kc_width.tail(60).median()) if len(kc_width.dropna()) else 0.0
        kc_expansion = self._clip((kc_width_now / max(kc_width_base, 1e-9) - 1.0) / 0.8)
        kc_compression = self._clip((1.0 - kc_width_now / max(kc_width_base, 1e-9)) / 0.45)

        volume_ma = float(volume.tail(20).mean())
        volume_multiple = float(volume.iloc[-1]) / max(volume_ma, 1e-9)
        volume_expansion = self._clip((volume_multiple - 1.0) / 1.2)

        close_outside_zone = self._close_outside_zone(close, dense_zone)
        edge_rejections = self._edge_rejection_count(recent, dense_zone)
        midline_chop = self._midline_cross_count(close, dense_zone)

        trend_score = self._clip(
            dense_zone.trend_score * 0.38
            + close_outside_zone * 0.18
            + kc_expansion * 0.16
            + atr_expansion * 0.10
            + volume_expansion * 0.12
            + self._pattern_trend_bonus(pattern) * 0.06
        )
        range_score = self._clip(
            dense_zone.range_score * 0.40
            + min(edge_rejections / 5, 1.0) * 0.20
            + min(midline_chop / 8, 1.0) * 0.16
            + kc_compression * 0.12
            + self._pattern_range_bonus(pattern) * 0.12
        )
        risk_score = self._risk_score(recent, atr_now, atr_base, dense_zone)
        breakout_quality = self._breakout_quality(dense_zone, pattern, trend_score, volume_expansion, close_outside_zone)
        regime_candidate, strategy_allowed = self._route(trend_score, range_score, risk_score, breakout_quality)

        reason_codes = self._reason_codes(
            dense_zone,
            pattern,
            trend_score,
            range_score,
            risk_score,
            breakout_quality,
            edge_rejections,
            midline_chop,
            close_outside_zone,
        )
        return RegimePattern(
            symbol=symbol,
            regime_candidate=regime_candidate,
            strategy_allowed=strategy_allowed,
            pattern_family=self._pattern_family(regime_candidate, pattern),
            pattern_name=pattern.pattern_type,
            breakout_quality=breakout_quality,
            trend_score=trend_score,
            range_score=range_score,
            risk_score=risk_score,
            position_context=dense_zone.structure_label or dense_zone.breakout_status,
            reason_codes=reason_codes,
            notes=f"trend={trend_score:.2f}, range={range_score:.2f}, risk={risk_score:.2f}",
        )

    def enrich_signal(self, signal, regime_pattern: RegimePattern):
        evidence = dict(signal.technical_evidence)
        evidence.update(
            {
                "regime_candidate": regime_pattern.regime_candidate,
                "strategy_allowed": regime_pattern.strategy_allowed,
                "regime_pattern_family": regime_pattern.pattern_family,
                "regime_pattern_name": regime_pattern.pattern_name,
                "breakout_quality": regime_pattern.breakout_quality,
                "regime_trend_score": regime_pattern.trend_score,
                "regime_range_score": regime_pattern.range_score,
                "regime_risk_score": regime_pattern.risk_score,
                "regime_reason_codes": ",".join(regime_pattern.reason_codes[:8]),
            }
        )
        return signal.model_copy(update={"technical_evidence": evidence})

    def _route(self, trend_score: float, range_score: float, risk_score: float, breakout_quality: str) -> tuple[str, str]:
        if risk_score >= 0.75:
            return "high_risk", "none"
        if trend_score >= 0.62 and trend_score >= range_score + 0.12 and breakout_quality in {"strong", "pending"}:
            return "trend", "trend"
        if range_score >= 0.58 and range_score >= trend_score + 0.08:
            return "range", "range"
        if trend_score >= 0.50 or range_score >= 0.50:
            return "transition", "none"
        return "unknown", "none"

    def _breakout_quality(
        self,
        dense_zone: DenseZone,
        pattern: PatternCandidate,
        trend_score: float,
        volume_expansion: float,
        close_outside_zone: float,
    ) -> str:
        if dense_zone.breakout_status == "failed_breakout":
            return "failed"
        if dense_zone.breakout_status in {"breakout_up", "breakout_down", "vacuum_travel", "retest_support", "retest_resistance"}:
            if trend_score >= 0.68 and volume_expansion >= 0.45 and close_outside_zone >= 0.4:
                return "strong"
            return "weak"
        if pattern.breakout_direction in {Side.LONG, Side.SHORT}:
            return "pending"
        if dense_zone.breakout_status == "inside_zone":
            return "none"
        return "unknown"

    def _pattern_family(self, regime_candidate: str, pattern: PatternCandidate) -> str:
        if regime_candidate == "trend":
            return "trend_continuation"
        if regime_candidate == "range":
            return "range_rotation"
        if pattern.pattern_type in {"symmetrical_triangle", "ascending_triangle", "descending_triangle"}:
            return "compression_transition"
        return "uncertain_structure"

    def _reason_codes(
        self,
        dense_zone: DenseZone,
        pattern: PatternCandidate,
        trend_score: float,
        range_score: float,
        risk_score: float,
        breakout_quality: str,
        edge_rejections: int,
        midline_chop: int,
        close_outside_zone: float,
    ) -> list[str]:
        codes: list[str] = []
        if trend_score > range_score:
            codes.append("trend_score_dominant")
        if range_score > trend_score:
            codes.append("range_score_dominant")
        if risk_score >= 0.75:
            codes.append("high_risk_structure")
        if dense_zone.breakout_status:
            codes.append(f"dense_zone_{dense_zone.breakout_status}")
        if breakout_quality != "unknown":
            codes.append(f"breakout_{breakout_quality}")
        if close_outside_zone >= 0.4:
            codes.append("multi_close_outside_dense_zone")
        if edge_rejections >= 3:
            codes.append("repeated_edge_rejection")
        if midline_chop >= 5:
            codes.append("dense_zone_midline_chop")
        if pattern.pattern_type != "unknown":
            codes.append(f"pattern_{pattern.pattern_type}")
        return codes[:10]

    def _close_outside_zone(self, close: pd.Series, dense_zone: DenseZone) -> float:
        if dense_zone.zone_low is None or dense_zone.zone_high is None:
            if dense_zone.breakout_status in {"vacuum_travel", "breakout_up", "breakout_down", "retest_support", "retest_resistance"}:
                return 1.0
            return 0.0
        recent = close.tail(5)
        outside = ((recent > dense_zone.zone_high) | (recent < dense_zone.zone_low)).sum()
        return float(outside) / max(len(recent), 1)

    def _edge_rejection_count(self, candles: pd.DataFrame, dense_zone: DenseZone) -> int:
        if dense_zone.zone_low is None or dense_zone.zone_high is None:
            return 0
        window = candles.tail(80)
        width = max(dense_zone.zone_high - dense_zone.zone_low, 1e-9)
        tolerance = width * 0.12
        upper_reject = (window["high"].astype(float) >= dense_zone.zone_high - tolerance) & (window["close"].astype(float) < dense_zone.zone_high - tolerance)
        lower_reject = (window["low"].astype(float) <= dense_zone.zone_low + tolerance) & (window["close"].astype(float) > dense_zone.zone_low + tolerance)
        return int((upper_reject | lower_reject).sum())

    def _midline_cross_count(self, close: pd.Series, dense_zone: DenseZone) -> int:
        if dense_zone.zone_mid is None:
            return 0
        values = close.tail(80).astype(float)
        above = values > dense_zone.zone_mid
        return int((above != above.shift(1)).sum())

    def _risk_score(self, candles: pd.DataFrame, atr_now: float, atr_base: float, dense_zone: DenseZone) -> float:
        current = float(candles["close"].iloc[-1])
        atr_pct = atr_now / max(current, 1e-9)
        atr_shock = self._clip((atr_now / max(atr_base, 1e-9) - 1.6) / 1.2)
        high_vol = self._clip((atr_pct - 0.055) / 0.05)
        unclear = 0.25 if dense_zone.trend_score < 0.35 and dense_zone.range_score < 0.35 else 0.0
        return self._clip(atr_shock * 0.45 + high_vol * 0.35 + unclear)

    def _pattern_trend_bonus(self, pattern: PatternCandidate) -> float:
        if pattern.pattern_type in {"ascending_triangle", "descending_triangle", "falling_wedge", "rising_wedge"} and pattern.breakout_direction:
            return 1.0
        if pattern.breakout_direction:
            return 0.65
        return 0.0

    def _pattern_range_bonus(self, pattern: PatternCandidate) -> float:
        return 1.0 if pattern.pattern_type == "range_rectangle" and pattern.breakout_direction is None else 0.0

    def _kc_width_pct(self, candles: pd.DataFrame) -> pd.Series:
        close = candles["close"].astype(float)
        middle = close.ewm(span=20, adjust=False, min_periods=20).mean()
        width = self._atr(candles, 14) * 2.8 * 2
        return width / middle.abs().clip(lower=1e-9)

    def _atr(self, candles: pd.DataFrame, length: int) -> pd.Series:
        high = candles["high"].astype(float)
        low = candles["low"].astype(float)
        close = candles["close"].astype(float)
        previous_close = close.shift(1).fillna(close)
        true_range = pd.concat([(high - low), (high - previous_close).abs(), (low - previous_close).abs()], axis=1).max(axis=1)
        return true_range.ewm(span=length, adjust=False, min_periods=length).mean()

    def _clip(self, value: float) -> float:
        if not math.isfinite(value):
            return 0.0
        return max(0.0, min(1.0, float(value)))
