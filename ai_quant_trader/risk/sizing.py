from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

PositionTier = Literal["block", "weak", "normal", "strong", "full"]

POSITION_TIER_SCALE: dict[PositionTier, float] = {
    "block": 0.0,
    "weak": 0.25,
    "normal": 0.5,
    "strong": 0.75,
    "full": 1.0,
}
POSITION_TIER_ORDER: tuple[PositionTier, ...] = ("block", "weak", "normal", "strong", "full")

FACTOR_RANKED_SCORE_WEIGHTS = {
    "technical_signal_score": 0.18,
    "orderflow_confirmation_score": 0.20,
    "news_direction_alignment_score": 0.14,
    "pattern_confirmation_score": 0.12,
    "range_safety_score": 0.11,
    "trend_confirmation_score": 0.10,
    "dense_zone_breakout_score": 0.08,
    "news_safety_score": 0.04,
    "btc_leader_score": 0.02,
    "eth_btc_rotation_score": 0.01,
}


@dataclass(frozen=True)
class SizingPolicyResult:
    policy: str
    score: float
    tier: PositionTier
    warnings: list[str] = field(default_factory=list)
    coverage: float = 1.0
    loss_risk_score: float | None = None


def tier_index(tier: str) -> int:
    return POSITION_TIER_ORDER.index(tier) if tier in POSITION_TIER_ORDER else 0


def min_tier(tier: PositionTier, cap: PositionTier) -> PositionTier:
    return POSITION_TIER_ORDER[min(tier_index(tier), tier_index(cap))]


def max_tier(tier: PositionTier, floor: PositionTier) -> PositionTier:
    return POSITION_TIER_ORDER[max(tier_index(tier), tier_index(floor))]


def limit_tier_lift(candidate: PositionTier, baseline: PositionTier, max_lift: int) -> PositionTier:
    allowed_index = min(tier_index(baseline) + max(max_lift, 0), len(POSITION_TIER_ORDER) - 1)
    return POSITION_TIER_ORDER[min(tier_index(candidate), allowed_index)]


def score_to_tier(score: float, min_trade_score: float) -> PositionTier:
    if score >= 0.81:
        return "full"
    if score >= 0.70:
        return "strong"
    if score >= 0.62:
        return "normal"
    if score >= min_trade_score:
        return "weak"
    return "block"


def factor_ranked_policy(score_inputs: dict[str, float], *, min_trade_score: float) -> SizingPolicyResult:
    score = min(
        max(
            sum(float(score_inputs.get(key, 0.0)) * weight for key, weight in FACTOR_RANKED_SCORE_WEIGHTS.items()),
            0.0,
        ),
        1.0,
    )
    return SizingPolicyResult(
        policy="legacy_factor_ranked",
        score=score,
        tier=score_to_tier(score, min_trade_score),
    )


def calibrated_v1_policy(
    score_inputs: dict[str, float],
    *,
    min_trade_score: float,
    min_factor_coverage: float,
) -> SizingPolicyResult:
    """Entry-time sizing calibration from structured factors.

    This is deliberately deterministic: DeepSeek extracts structured factors,
    while local code turns them into a risk-adjusted edge score. It is not a
    learned model yet; real-trade shadow outcomes must decide whether it earns
    more freedom later.
    """

    core_keys = (
        "technical_signal_score",
        "orderflow_confirmation_score",
        "pattern_confirmation_score",
        "dense_zone_breakout_score",
        "range_safety_score",
        "trend_confirmation_score",
    )
    present = sum(1 for key in core_keys if float(score_inputs.get(key, 0.0)) > 0.05)
    coverage = present / len(core_keys)

    technical = float(score_inputs.get("technical_signal_score", 0.0))
    orderflow = float(score_inputs.get("orderflow_confirmation_score", 0.0))
    pattern = float(score_inputs.get("pattern_confirmation_score", 0.0))
    dense = float(score_inputs.get("dense_zone_breakout_score", 0.0))
    trend = float(score_inputs.get("trend_confirmation_score", 0.0))
    news_direction = float(score_inputs.get("news_direction_alignment_score", 0.0))
    range_safety = float(score_inputs.get("range_safety_score", 0.0))
    news_safety = float(score_inputs.get("news_safety_score", 0.0))
    btc = float(score_inputs.get("btc_leader_score", 0.0))
    rotation = float(score_inputs.get("eth_btc_rotation_score", 0.0))

    quality = (
        technical * 0.18
        + orderflow * 0.24
        + pattern * 0.14
        + dense * 0.13
        + trend * 0.10
        + news_direction * 0.09
        + range_safety * 0.05
        + btc * 0.04
        + rotation * 0.03
    )
    tail_penalty = (
        (1.0 - range_safety) * 0.18
        + (1.0 - news_safety) * 0.10
        + max(0.0, 0.55 - orderflow) * 0.18
        + max(0.0, 0.50 - pattern) * 0.12
        + max(0.0, 0.45 - dense) * 0.10
    )
    edge_score = min(max(quality - tail_penalty + 0.18, 0.0), 1.0)

    warnings: list[str] = []
    if coverage < min_factor_coverage:
        warnings.append("calibrated_factor_coverage_low_fallback_legacy")
        return SizingPolicyResult(
            policy="calibrated_v1_controlled",
            score=edge_score,
            tier="block",
            warnings=warnings,
            coverage=coverage,
        )
    if orderflow < 0.35:
        warnings.append("calibrated_orderflow_weak_tail_penalty")
    if pattern < 0.40:
        warnings.append("calibrated_pattern_weak_tail_penalty")
    return SizingPolicyResult(
        policy="calibrated_v1_controlled",
        score=edge_score,
        tier=score_to_tier(edge_score, min_trade_score),
        warnings=warnings,
        coverage=coverage,
    )


def loss_risk_score(score_inputs: dict[str, float]) -> float:
    technical = float(score_inputs.get("technical_signal_score", 0.0))
    orderflow = float(score_inputs.get("orderflow_confirmation_score", 0.0))
    pattern = float(score_inputs.get("pattern_confirmation_score", 0.0))
    dense = float(score_inputs.get("dense_zone_breakout_score", 0.0))
    trend = float(score_inputs.get("trend_confirmation_score", 0.0))
    range_safety = float(score_inputs.get("range_safety_score", 0.0))
    news_safety = float(score_inputs.get("news_safety_score", 0.0))
    btc = float(score_inputs.get("btc_leader_score", 0.0))
    rotation = float(score_inputs.get("eth_btc_rotation_score", 0.0))

    weak_structure = (
        max(0.0, 0.62 - orderflow) * 0.26
        + max(0.0, 0.58 - pattern) * 0.22
        + max(0.0, 0.55 - dense) * 0.18
        + max(0.0, 0.55 - trend) * 0.10
    )
    hostile_context = (
        (1.0 - range_safety) * 0.20
        + (1.0 - news_safety) * 0.08
        + max(0.0, 0.50 - btc) * 0.06
        + max(0.0, 0.50 - rotation) * 0.04
    )
    late_breakout_divergence = max(0.0, technical - 0.72) * (
        max(0.0, 0.60 - orderflow) * 0.22
        + max(0.0, 0.55 - pattern) * 0.16
        + max(0.0, 0.52 - dense) * 0.14
    )
    return min(max(weak_structure + hostile_context + late_breakout_divergence, 0.0), 1.0)


def calibrated_v2_loss_aware_policy(
    score_inputs: dict[str, float],
    *,
    min_trade_score: float,
    min_factor_coverage: float,
) -> SizingPolicyResult:
    """Research-only loss-aware candidate.

    The v1 calibration lifted too many historical losers. This candidate keeps
    v1's edge score, but requires orderflow and structure confirmation before
    allowing strong/full tiers, and cuts one tier when orderflow is very weak.
    It is intentionally not wired into live config until shadow results justify
    it.
    """

    base = calibrated_v1_policy(
        score_inputs,
        min_trade_score=min_trade_score,
        min_factor_coverage=min_factor_coverage,
    )
    legacy = factor_ranked_policy(score_inputs, min_trade_score=min_trade_score)
    risk = loss_risk_score(score_inputs)
    if "calibrated_factor_coverage_low_fallback_legacy" in base.warnings:
        return SizingPolicyResult(
            policy="calibrated_v2_loss_aware",
            score=base.score,
            tier=base.tier,
            warnings=[*base.warnings],
            coverage=base.coverage,
            loss_risk_score=risk,
        )

    orderflow = float(score_inputs.get("orderflow_confirmation_score", 0.0))
    pattern = float(score_inputs.get("pattern_confirmation_score", 0.0))
    dense = float(score_inputs.get("dense_zone_breakout_score", 0.0))
    range_safety = float(score_inputs.get("range_safety_score", 0.0))
    trend = float(score_inputs.get("trend_confirmation_score", 0.0))
    volume = float(score_inputs.get("volume_score", 0.5))

    tier = base.tier
    warnings = [*base.warnings]
    if orderflow < 0.74 and tier_index(tier) > tier_index(legacy.tier):
        tier = legacy.tier
        warnings.append("loss_aware_orderflow_blocks_promotion")
    if orderflow < 0.30:
        tier = POSITION_TIER_ORDER[max(0, tier_index(tier) - 1)]
        warnings.append("loss_aware_very_weak_orderflow_reduces_tier")
    if tier_index(tier) >= tier_index("full") and not (
        orderflow >= 0.80 and pattern >= 0.88 and dense >= 0.45 and range_safety >= 0.62
    ):
        tier = "strong"
        warnings.append("loss_aware_full_requires_orderflow_pattern_range")
    if tier_index(tier) >= tier_index("strong") and not (
        orderflow >= 0.74 and (pattern >= 0.88 or dense >= 0.45) and trend >= 0.45
    ):
        tier = "normal"
        warnings.append("loss_aware_strong_requires_orderflow_structure")
    if orderflow < 0.50 and volume < 0.60:
        tier = min_tier(tier, "weak")
        warnings.append("loss_aware_low_orderflow_low_volume_caps_weak")
    return SizingPolicyResult(
        policy="calibrated_v2_loss_aware",
        score=base.score,
        tier=tier,
        warnings=warnings,
        coverage=base.coverage,
        loss_risk_score=risk,
    )
