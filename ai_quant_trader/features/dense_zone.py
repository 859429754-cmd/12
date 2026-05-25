from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ai_quant_trader.core.models import DenseZone


@dataclass(frozen=True)
class _VolumeZone:
    low: float
    high: float
    mid: float
    volume: float
    start_idx: int
    end_idx: int

    def contains(self, price: float) -> bool:
        return self.low <= price <= self.high


class DenseZoneAnalyzer:
    """用 1h K 线 VPVR 识别交易密集区、真空区、突破与回踩状态。"""

    def calculate(
        self,
        symbol: str,
        candles: pd.DataFrame,
        trade_prices: list[float] | None = None,
        bins: int = 48,
    ) -> DenseZone:
        if candles.empty:
            raise ValueError("candles is empty")

        recent = candles.tail(240).copy()
        low = float(recent["low"].min())
        high = float(recent["high"].max())
        if high <= low:
            high = low * 1.01 if low else 1.0

        step = (high - low) / bins
        volume_by_bin = self._build_volume_profile(recent, low, step, bins, trade_prices)
        total_volume = sum(volume_by_bin) or 1.0
        poc_idx = max(range(bins), key=lambda i: volume_by_bin[i])

        sorted_bins = sorted(range(bins), key=lambda i: volume_by_bin[i], reverse=True)
        value_bins = self._value_area_bins(sorted_bins, volume_by_bin, total_volume)

        def price_for(idx: int) -> float:
            return low + (idx + 0.5) * step

        poc = price_for(poc_idx)
        val = low + min(value_bins) * step
        vah = low + (max(value_bins) + 1) * step
        current = float(recent["close"].iloc[-1])
        previous_close = float(recent["close"].iloc[-2]) if len(recent) >= 2 else current

        zones = self._cluster_dense_zones(volume_by_bin, low, step, total_volume)
        current_zone = self._find_current_zone(zones, current)
        previous_zone = max((z for z in zones if z.high < current), key=lambda z: z.high, default=None)
        next_zone = min((z for z in zones if z.low > current), key=lambda z: z.low, default=None)

        zone_low = current_zone.low if current_zone else None
        zone_high = current_zone.high if current_zone else None
        zone_mid = current_zone.mid if current_zone else None
        if current_zone is None and previous_zone and next_zone:
            vacuum_low = previous_zone.high
            vacuum_high = next_zone.low
        else:
            vacuum_low = None
            vacuum_high = None

        current_position = self._value_area_position(current, vah, val)
        touch_count_vah = self._touch_count(recent, vah, tolerance=step * 0.8)
        touch_count_val = self._touch_count(recent, val, tolerance=step * 0.8)
        breakout_status, retest_status = self._structure_status(
            recent=recent,
            current=current,
            previous_close=previous_close,
            zone_low=zone_low,
            zone_high=zone_high,
            previous_zone=previous_zone,
            next_zone=next_zone,
            vacuum_low=vacuum_low,
            vacuum_high=vacuum_high,
            step=step,
        )
        trend_score, range_score, structure_label = self._score_structure(
            current=current,
            current_zone=current_zone,
            previous_zone=previous_zone,
            next_zone=next_zone,
            vacuum_low=vacuum_low,
            vacuum_high=vacuum_high,
            breakout_status=breakout_status,
            touch_count_vah=touch_count_vah,
            touch_count_val=touch_count_val,
            profile_strength=min(1.0, volume_by_bin[poc_idx] / total_volume * 8),
        )

        hvn = [price_for(i) for i in sorted_bins[:3]]
        lvn = [price_for(i) for i in sorted_bins[-3:]]
        support_candidates = [p for p in hvn if p <= current]
        resistance_candidates = [p for p in hvn if p >= current]

        return DenseZone(
            symbol=symbol,
            poc=poc,
            vah=vah,
            val=val,
            hvn=hvn,
            lvn=lvn,
            support=max(support_candidates) if support_candidates else val,
            resistance=min(resistance_candidates) if resistance_candidates else vah,
            current_position=current_position,
            strength=min(1.0, volume_by_bin[poc_idx] / total_volume * 8),
            zone_low=zone_low,
            zone_high=zone_high,
            zone_mid=zone_mid,
            previous_zone_low=previous_zone.low if previous_zone else None,
            previous_zone_high=previous_zone.high if previous_zone else None,
            next_zone_low=next_zone.low if next_zone else None,
            next_zone_high=next_zone.high if next_zone else None,
            vacuum_low=vacuum_low,
            vacuum_high=vacuum_high,
            breakout_status=breakout_status,
            retest_status=retest_status,
            touch_count_vah=touch_count_vah,
            touch_count_val=touch_count_val,
            trend_score=trend_score,
            range_score=range_score,
            structure_label=structure_label,
        )

    def _build_volume_profile(
        self,
        recent: pd.DataFrame,
        low: float,
        step: float,
        bins: int,
        trade_prices: list[float] | None,
    ) -> list[float]:
        volume_by_bin = [0.0 for _ in range(bins)]
        for _, row in recent.iterrows():
            typical = float((row["high"] + row["low"] + row["close"]) / 3)
            idx = min(bins - 1, max(0, int((typical - low) / step)))
            volume_by_bin[idx] += float(row["volume"])

        if trade_prices:
            for price in trade_prices[-1000:]:
                if low <= price <= low + step * bins:
                    idx = min(bins - 1, max(0, int((price - low) / step)))
                    volume_by_bin[idx] *= 1.03
        return volume_by_bin

    def _value_area_bins(
        self,
        sorted_bins: list[int],
        volume_by_bin: list[float],
        total_volume: float,
    ) -> set[int]:
        covered = 0.0
        value_bins: set[int] = set()
        for idx in sorted_bins:
            value_bins.add(idx)
            covered += volume_by_bin[idx]
            if covered / total_volume >= 0.70:
                break
        return value_bins

    def _cluster_dense_zones(
        self,
        volume_by_bin: list[float],
        low: float,
        step: float,
        total_volume: float,
    ) -> list[_VolumeZone]:
        sorted_volume = sorted(volume_by_bin)
        threshold = sorted_volume[int(len(sorted_volume) * 0.60)]
        threshold = max(threshold, total_volume / len(volume_by_bin) * 0.8)
        zones: list[_VolumeZone] = []
        start: int | None = None
        acc = 0.0

        for idx, volume in enumerate(volume_by_bin + [-1.0]):
            is_dense = idx < len(volume_by_bin) and volume >= threshold
            if is_dense:
                start = idx if start is None else start
                acc += volume
                continue
            if start is not None:
                end = idx - 1
                zone_low = low + start * step
                zone_high = low + (end + 1) * step
                zones.append(
                    _VolumeZone(
                        low=zone_low,
                        high=zone_high,
                        mid=(zone_low + zone_high) / 2,
                        volume=acc,
                        start_idx=start,
                        end_idx=end,
                    )
                )
                start = None
                acc = 0.0

        return self._merge_nearby_zones(sorted(zones, key=lambda z: z.low), step)

    def _merge_nearby_zones(self, zones: list[_VolumeZone], step: float) -> list[_VolumeZone]:
        if not zones:
            return []

        merged: list[_VolumeZone] = []
        current = zones[0]
        for zone in zones[1:]:
            gap = zone.low - current.high
            if gap <= step * 5:
                low = current.low
                high = zone.high
                current = _VolumeZone(
                    low=low,
                    high=high,
                    mid=(low + high) / 2,
                    volume=current.volume + zone.volume,
                    start_idx=current.start_idx,
                    end_idx=zone.end_idx,
                )
                continue
            merged.append(current)
            current = zone
        merged.append(current)
        return merged

    def _find_current_zone(self, zones: list[_VolumeZone], current: float) -> _VolumeZone | None:
        containing = [zone for zone in zones if zone.contains(current)]
        if containing:
            return max(containing, key=lambda zone: zone.volume)
        return None

    def _value_area_position(
        self,
        current: float,
        vah: float,
        val: float,
    ) -> str:
        if current > vah:
            return "above_value"
        if current < val:
            return "below_value"
        return "inside_value"

    def _touch_count(self, recent: pd.DataFrame, level: float, tolerance: float) -> int:
        window = recent.tail(80)
        touches = (
            ((window["low"] <= level + tolerance) & (window["high"] >= level - tolerance))
            | ((window["close"] - level).abs() <= tolerance)
        )
        return int(touches.sum())

    def _structure_status(
        self,
        recent: pd.DataFrame,
        current: float,
        previous_close: float,
        zone_low: float | None,
        zone_high: float | None,
        previous_zone: _VolumeZone | None,
        next_zone: _VolumeZone | None,
        vacuum_low: float | None,
        vacuum_high: float | None,
        step: float,
    ) -> tuple[str, str]:
        recent_tail = recent.tail(12)
        if zone_low is not None and zone_high is not None:
            if previous_close <= zone_high < current:
                return "breakout_up", "none"
            if previous_close >= zone_low > current:
                return "breakout_down", "none"
            return "inside_zone", "none"

        if previous_zone and current > previous_zone.high:
            retest = bool((recent_tail["low"] <= previous_zone.high + step).any())
            if retest:
                return "retest_support", "support_retest"
        if next_zone and current < next_zone.low:
            retest = bool((recent_tail["high"] >= next_zone.low - step).any())
            if retest:
                return "retest_resistance", "resistance_retest"
        if vacuum_low is not None and vacuum_high is not None and vacuum_low < current < vacuum_high:
            return "vacuum_travel", "none"
        return "unknown", "unknown"

    def _score_structure(
        self,
        current: float,
        current_zone: _VolumeZone | None,
        previous_zone: _VolumeZone | None,
        next_zone: _VolumeZone | None,
        vacuum_low: float | None,
        vacuum_high: float | None,
        breakout_status: str,
        touch_count_vah: int,
        touch_count_val: int,
        profile_strength: float,
    ) -> tuple[float, float, str]:
        touch_density = min(1.0, (touch_count_vah + touch_count_val) / 18)

        if current_zone:
            zone_width_pct = (current_zone.high - current_zone.low) / max(current, 1e-9)
            narrow_bonus = 1.0 - min(1.0, zone_width_pct / 0.12)
            range_score = min(1.0, 0.35 + profile_strength * 0.35 + touch_density * 0.20 + narrow_bonus * 0.10)
            trend_score = max(0.0, 0.35 - touch_density * 0.15)
            if current >= current_zone.mid:
                label = "密集区内偏强震荡"
            else:
                label = "密集区内偏弱震荡"
            return trend_score, range_score, label

        if vacuum_low is not None and vacuum_high is not None:
            vacuum_progress = (current - vacuum_low) / max(vacuum_high - vacuum_low, 1e-9)
            vacuum_progress = max(0.0, min(1.0, vacuum_progress))
            trend_score = min(1.0, 0.55 + abs(vacuum_progress - 0.5) * 0.45)
            range_score = max(0.0, 0.30 - trend_score * 0.15)
            label = "真空区趋势推进"
            return trend_score, range_score, label

        if breakout_status in {"breakout_up", "breakout_down", "retest_support", "retest_resistance"}:
            return 0.72, 0.18, "密集区突破或回踩"

        if previous_zone and not next_zone:
            return 0.58, 0.20, "上方真空区延伸"
        if next_zone and not previous_zone:
            return 0.58, 0.20, "下方真空区延伸"
        return 0.25, 0.25, "结构不明确"
