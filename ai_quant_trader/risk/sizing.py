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
