from __future__ import annotations

from ai_quant_trader.core.models import (
    AiDecision,
    Alignment,
    MarketRegime,
    PositionSnapshot,
    RiskConfig,
    RiskDecision,
    Side,
    SignalAction,
    StrategySignal,
    VetoAction,
)
from ai_quant_trader.core.state import RuntimeState
from ai_quant_trader.risk.sizing import (
    FACTOR_RANKED_SCORE_WEIGHTS,
    POSITION_TIER_ORDER,
    POSITION_TIER_SCALE,
    PositionTier,
    calibrated_v1_policy,
    factor_ranked_policy,
    limit_tier_lift,
    max_tier,
    min_tier,
)


class RiskManager:
    """Final hard gate before any order reaches the exchange gateway.

    AI cannot bypass local technical signals, symbol authorization, cold-start
    locks, duplicate-position checks, or the total leverage limit.
    """
    def __init__(self, config: RiskConfig, state: RuntimeState):
        self.config = config
        self.state = state

    def evaluate(
        self,
        signal: StrategySignal,
        ai: AiDecision,
        equity: float,
        positions: list[PositionSnapshot],
    ) -> RiskDecision:
        max_total_notional = equity * self.config.max_total_leverage
        used_notional = sum(pos.notional for pos in positions)
        remaining = max(max_total_notional - used_notional, 0.0)

        if signal.action in {SignalAction.EXIT_LONG, SignalAction.EXIT_SHORT}:
            return RiskDecision(
                allowed=True,
                action=signal.action,
                symbol=signal.symbol,
                target_qty=signal.suggested_qty,
                clipped_qty=signal.suggested_qty,
                target_notional=0.0,
                max_total_notional=max_total_notional,
                remaining_notional=remaining,
                reason="exit_signal_allowed_even_when_opening_paused",
            )

        major_news_context = self._has_major_news_context(signal)
        if signal.action == SignalAction.HOLD:
            return RiskDecision(
                allowed=False,
                action=signal.action,
                symbol=signal.symbol,
                max_total_notional=max_total_notional,
                remaining_notional=remaining,
                reason="major_news_without_strategy_signal" if major_news_context else "no_entry_signal",
            )

        if not self.state.can_open(signal.symbol):
            return RiskDecision(
                allowed=False,
                action=signal.action,
                symbol=signal.symbol,
                max_total_notional=max_total_notional,
                remaining_notional=remaining,
                reason="cold_start_or_symbol_not_authorized",
            )

        if self._same_direction_position(signal, positions) is not None:
            return self._blocked(signal, max_total_notional, remaining, "same_direction_position_exists")

        strategy_allowed = str(signal.technical_evidence.get("strategy_allowed") or "trend")
        if strategy_allowed != "trend":
            return self._blocked(signal, max_total_notional, remaining, f"regime_blocks_trend_strategy:{strategy_allowed}")

        expected_direction = "long" if signal.action == SignalAction.LONG else "short"
        if ai.direction != expected_direction:
            return self._blocked(signal, max_total_notional, remaining, "ai_direction_conflict")
        if ai.regime != MarketRegime.TREND:
            return self._blocked(signal, max_total_notional, remaining, "ai_not_trend_regime")
        if ai.veto_action == VetoAction.BLOCK:
            return self._blocked(signal, max_total_notional, remaining, "ai_veto_block")
        if ai.confidence < self.config.min_confidence_to_trade:
            return self._blocked(signal, max_total_notional, remaining, "ai_confidence_too_low")
        if ai.news_alignment == Alignment.CONFLICT:
            reason = "major_news_direction_conflict" if major_news_context else "news_major_conflict"
            return self._blocked(signal, max_total_notional, remaining, reason)
        if ai.orderflow_alignment == Alignment.CONFLICT:
            return self._blocked(signal, max_total_notional, remaining, "orderflow_conflict")
        if remaining <= 0:
            return self._blocked(signal, max_total_notional, remaining, "max_total_leverage_reached")

        strategy_baseline_notional = max(signal.current_price * signal.suggested_qty, 0.0)
        score, tier, breakdown, score_warnings = self._decision_score(signal, ai)
        if score < self.config.min_confidence_to_trade:
            blocked = self._blocked(signal, max_total_notional, remaining, "combined_decision_score_too_low")
            blocked.decision_score = score
            blocked.position_scale = 0.0
            blocked.position_tier = "block"
            self._attach_sizing_audit(blocked, breakdown)
            blocked.score_breakdown = breakdown
            blocked.warnings = [*ai.data_quality_warnings, *score_warnings]
            return blocked
        if tier == "block":
            blocked = self._blocked(signal, max_total_notional, remaining, "five_score_tier_block")
            blocked.decision_score = score
            blocked.position_scale = 0.0
            blocked.position_tier = "block"
            self._attach_sizing_audit(blocked, breakdown)
            blocked.score_breakdown = breakdown
            blocked.warnings = [*ai.data_quality_warnings, *score_warnings]
            return blocked

        consensus_score = self._consensus_score(ai)
        if tier == "full" and (consensus_score < 3 or ai.confidence < self.config.ai_full_size_confidence):
            tier = "strong"
        if consensus_score >= 3 and ai.confidence >= self.config.ai_full_size_confidence and score >= 0.85:
            tier = self._max_tier(tier, "full")
        tier = self._apply_post_consensus_caps(tier, ai, score_warnings)
        if major_news_context and ai.news_alignment in {Alignment.NEUTRAL, Alignment.UNKNOWN}:
            tier = self._min_tier(tier, "normal")
            score_warnings.append(f"major_news_{ai.news_alignment}_caps_normal")
        if (
            major_news_context
            and ai.news_alignment == Alignment.ALIGNED
            and (ai.orderflow_confirmation_score < 0.75 or ai.dense_zone_breakout_score < 0.65)
        ):
            if tier == "full":
                tier = "strong"
            if tier in {"strong", "full"}:
                score_warnings.append("major_news_full_requires_orderflow_and_dense_zone_confirmation")
        if ai.veto_action == VetoAction.REDUCE:
            tier = self._reduce_tier_cap(tier)
        if tier == "block":
            blocked = self._blocked(signal, max_total_notional, remaining, "post_consensus_risk_caps_block")
            blocked.decision_score = score
            blocked.position_scale = 0.0
            blocked.position_tier = "block"
            self._attach_sizing_audit(blocked, breakdown)
            blocked.score_breakdown = breakdown
            blocked.warnings = [*ai.data_quality_warnings, *score_warnings]
            return blocked

        scale = POSITION_TIER_SCALE[tier]

        sizing_basis = "account_risk_cap" if self.config.ai_dynamic_position_sizing else "strategy_signal"
        sizing_cap_notional = max_total_notional if self.config.ai_dynamic_position_sizing else strategy_baseline_notional
        if strategy_baseline_notional <= 0:
            sizing_cap_notional = 0.0
        scaled_notional = sizing_cap_notional * scale

        clipped_notional = min(max(scaled_notional, 0.0), remaining)
        if self.config.small_position_mode:
            clipped_notional = min(clipped_notional, self.config.small_position_notional_usdt)
        clipped_qty = clipped_notional / signal.current_price if signal.current_price > 0 else 0.0
        decision = RiskDecision(
            allowed=clipped_qty > 0,
            action=signal.action,
            symbol=signal.symbol,
            target_qty=signal.suggested_qty,
            clipped_qty=clipped_qty,
            target_notional=clipped_notional,
            strategy_baseline_notional=strategy_baseline_notional,
            ai_desired_notional=scaled_notional,
            sizing_basis=sizing_basis,
            max_total_notional=max_total_notional,
            remaining_notional=remaining,
            decision_score=score,
            position_scale=scale,
            position_tier=tier,
            score_breakdown=breakdown,
            reason=self._scale_reason(tier, consensus_score) if clipped_qty > 0 else "qty_clipped_to_zero",
            warnings=[*ai.data_quality_warnings, *score_warnings],
        )
        self._attach_sizing_audit(decision, breakdown)
        return decision

    def _blocked(
        self,
        signal: StrategySignal,
        max_total_notional: float,
        remaining: float,
        reason: str,
    ) -> RiskDecision:
        return RiskDecision(
            allowed=False,
            action=signal.action,
            symbol=signal.symbol,
            max_total_notional=max_total_notional,
            remaining_notional=remaining,
            reason=reason,
        )

    def _consensus_score(self, ai: AiDecision) -> int:
        score = 0
        if ai.regime == MarketRegime.TREND and ai.direction in {"long", "short"}:
            score += 1
        if ai.news_alignment == Alignment.ALIGNED:
            score += 1
        if ai.orderflow_alignment == Alignment.ALIGNED:
            score += 1
        if ai.btc_leader_alignment == Alignment.ALIGNED and ai.btc_leader_impact_score >= 0.25:
            score += 1
        if ai.dense_zone_position in {"above_value", "below_value"}:
            score += 1
        return score

    def _same_direction_position(
        self,
        signal: StrategySignal,
        positions: list[PositionSnapshot],
    ) -> PositionSnapshot | None:
        expected_side = Side.LONG if signal.action == SignalAction.LONG else Side.SHORT
        for position in positions:
            if position.symbol == signal.symbol and position.side == expected_side and abs(position.qty) > 0:
                return position
        return None

    def _has_major_news_context(self, signal: StrategySignal) -> bool:
        evidence = signal.technical_evidence or {}
        return bool(
            evidence.get("major_news_context")
            or evidence.get("news_risk_review")
            or evidence.get("major_news_severity")
            or float(evidence.get("major_news_event_count") or 0) > 0
        )

    def _decision_score(self, signal: StrategySignal, ai: AiDecision) -> tuple[float, PositionTier, dict[str, float], list[str]]:
        technical = min(max(signal.signal_strength, 0.0), 1.0)
        trend = min(max(ai.trend_confirmation_score, 0.0), 1.0)
        range_safety = 1.0 - min(max(ai.range_risk_score, 0.0), 1.0)
        news_safety = 1.0 - min(max(ai.news_risk_score, 0.0), 1.0)
        news_direction_raw = min(max(ai.news_direction_alignment_score, 0.0), 1.0)
        news_direction = news_direction_raw if ai.news_alignment == Alignment.ALIGNED else 0.0
        pattern = min(max(ai.pattern_confirmation_score, 0.0), 1.0)
        orderflow = min(max(ai.orderflow_confirmation_score, 0.0), 1.0)
        dense_zone = min(max(ai.dense_zone_breakout_score, 0.0), 1.0)
        btc_leader = self._btc_leader_score(ai)
        eth_btc_rotation = min(max(ai.eth_btc_rotation_score, 0.0), 1.0)
        score_inputs = {
            "technical_signal_score": technical,
            "orderflow_confirmation_score": orderflow,
            "news_direction_alignment_score": news_direction,
            "pattern_confirmation_score": pattern,
            "dense_zone_breakout_score": dense_zone,
            "range_safety_score": range_safety,
            "trend_confirmation_score": trend,
            "news_safety_score": news_safety,
            "btc_leader_score": btc_leader,
            "eth_btc_rotation_score": eth_btc_rotation,
        }
        legacy = factor_ranked_policy(score_inputs, min_trade_score=self.config.min_confidence_to_trade)
        calibrated = calibrated_v1_policy(
            score_inputs,
            min_trade_score=self.config.min_confidence_to_trade,
            min_factor_coverage=self.config.calibrated_min_factor_coverage,
        )

        selected = legacy
        selection_warnings: list[str] = []
        if self.config.ai_sizing_policy == "calibrated_v1_controlled":
            if "calibrated_factor_coverage_low_fallback_legacy" in calibrated.warnings:
                selected = legacy
                selection_warnings.append("calibrated_v1_fallback_to_legacy_factor_coverage_low")
            else:
                limited_tier = limit_tier_lift(
                    calibrated.tier,
                    legacy.tier,
                    self.config.calibrated_max_tier_lift,
                )
                if limited_tier != calibrated.tier:
                    selection_warnings.append(
                        f"calibrated_tier_lift_limited:{calibrated.tier}->{limited_tier}:legacy={legacy.tier}"
                    )
                selected = type(calibrated)(
                    policy=calibrated.policy,
                    score=calibrated.score,
                    tier=limited_tier,
                    warnings=calibrated.warnings,
                    coverage=calibrated.coverage,
                )

        raw_score = selected.score
        tier = selected.tier
        warnings: list[str] = [*selection_warnings]
        if self.config.ai_sizing_policy == "calibrated_v1_controlled":
            warnings.extend(calibrated.warnings)
        if ai.range_risk_score >= 0.85:
            tier = "block"
            warnings.append("range_risk_extreme_blocks_entry")
        elif ai.range_risk_score >= 0.70:
            tier = self._min_tier(tier, "weak")
            warnings.append("range_risk_high_caps_weak")
        elif ai.range_risk_score >= 0.55:
            tier = self._min_tier(tier, "normal")
            warnings.append("range_risk_elevated_caps_normal")
        if ai.news_risk_score >= 0.85 and ai.news_alignment == Alignment.ALIGNED:
            tier = self._min_tier(tier, "weak")
            warnings.append("aligned_major_news_extreme_risk_caps_weak")
        elif ai.news_risk_score >= 0.85:
            tier = "block"
            warnings.append("news_risk_extreme_blocks_entry")
        elif ai.news_risk_score >= 0.70:
            tier = self._min_tier(tier, "weak")
            warnings.append("news_risk_high_caps_weak")
        elif ai.news_risk_score >= 0.55:
            tier = self._min_tier(tier, "normal")
            warnings.append("news_risk_elevated_caps_normal")
        if ai.orderflow_confirmation_score < 0.35:
            tier = self._min_tier(tier, "weak")
            warnings.append("orderflow_confirmation_weak_caps_weak")
        if ai.dense_zone_breakout_score < 0.25:
            tier = self._min_tier(tier, "normal")
            warnings.append("dense_zone_breakout_quality_weak_caps_normal")
        if ai.pattern_confirmation_score < 0.25:
            tier = self._min_tier(tier, "weak")
            warnings.append("pattern_confirmation_very_weak_caps_weak")
        elif ai.pattern_confirmation_score < 0.40:
            tier = self._min_tier(tier, "normal")
            warnings.append("pattern_confirmation_weak_caps_normal")
        rotation_context = ai.btc_leader_regime in {"rotation_lag", "leader_pullback"} and ai.eth_btc_rotation_score >= 0.55
        if ai.btc_leader_alignment == Alignment.CONFLICT and ai.btc_leader_impact_score >= 0.80 and not rotation_context:
            tier = self._min_tier(tier, "weak")
            warnings.append("btc_leader_conflict_high_impact_caps_weak")
        elif ai.btc_leader_alignment == Alignment.CONFLICT and ai.btc_leader_impact_score >= 0.45 and not rotation_context:
            tier = self._min_tier(tier, "normal")
            warnings.append("btc_leader_conflict_caps_normal")
        if rotation_context:
            warnings.append("btc_eth_rotation_context_prevents_false_conflict_cap")
        if ai.btc_leader_alignment in {Alignment.NEUTRAL, Alignment.UNKNOWN} and ai.btc_leader_impact_score >= 0.60:
            tier = self._min_tier(tier, "strong")
            warnings.append("btc_leader_unclear_high_impact_size_not_full")
        if ai.news_alignment in {Alignment.NEUTRAL, Alignment.UNKNOWN} and ai.crypto_market_impact_score >= 0.65:
            tier = self._min_tier(tier, "normal")
            warnings.append("broad_crypto_news_unclear_caps_normal")
        if ai.news_alignment in {Alignment.NEUTRAL, Alignment.UNKNOWN} and ai.symbol_news_impact_score >= 0.65:
            tier = self._min_tier(tier, "normal")
            warnings.append("symbol_news_unclear_caps_normal")
        if ai.news_alignment == Alignment.NEUTRAL:
            warnings.append("news_neutral_size_not_full")
        if ai.orderflow_alignment == Alignment.NEUTRAL:
            warnings.append("orderflow_neutral_size_not_full")
        return raw_score, tier, {
            **score_inputs,
            "crypto_market_impact_score": min(max(ai.crypto_market_impact_score, 0.0), 1.0),
            "btc_leader_impact_score": min(max(ai.btc_leader_impact_score, 0.0), 1.0),
            "symbol_news_impact_score": min(max(ai.symbol_news_impact_score, 0.0), 1.0),
            **{f"weight_{key}": weight for key, weight in FACTOR_RANKED_SCORE_WEIGHTS.items()},
            "legacy_decision_score": legacy.score,
            "legacy_position_scale": POSITION_TIER_SCALE[legacy.tier],
            "legacy_position_tier_index": float(self._tier_index(legacy.tier)),
            "calibrated_edge_score": calibrated.score,
            "calibrated_position_scale": POSITION_TIER_SCALE[calibrated.tier],
            "calibrated_position_tier_index": float(self._tier_index(calibrated.tier)),
            "calibrated_factor_coverage": calibrated.coverage,
            "selected_position_tier_index": float(self._tier_index(tier)),
            "combined_decision_score": raw_score,
        }, warnings

    def _btc_leader_score(self, ai: AiDecision) -> float:
        impact = min(max(ai.btc_leader_impact_score, 0.0), 1.0)
        rotation = min(max(ai.eth_btc_rotation_score, 0.0), 1.0)
        if ai.btc_leader_regime in {"rotation_lag", "leader_pullback"}:
            return min(1.0, 0.55 + rotation * 0.35)
        if ai.btc_leader_alignment == Alignment.ALIGNED:
            return min(1.0, 0.5 + impact * 0.5)
        if ai.btc_leader_alignment == Alignment.CONFLICT:
            return max(0.0, 0.5 - impact * 0.5)
        return 0.5

    def _apply_post_consensus_caps(self, tier: str, ai: AiDecision, warnings: list[str]) -> str:
        def warn_once(value: str) -> None:
            if value not in warnings:
                warnings.append(value)

        if ai.range_risk_score >= 0.85:
            warn_once("range_risk_extreme_blocks_entry")
            return "block"
        if ai.range_risk_score >= 0.70:
            tier = self._min_tier(tier, "weak")
            warn_once("range_risk_high_caps_weak")
        elif ai.range_risk_score >= 0.55:
            tier = self._min_tier(tier, "normal")
            warn_once("range_risk_elevated_caps_normal")

        if ai.news_risk_score >= 0.85 and ai.news_alignment == Alignment.ALIGNED:
            tier = self._min_tier(tier, "weak")
            warn_once("aligned_major_news_extreme_risk_caps_weak")
        elif ai.news_risk_score >= 0.85:
            warn_once("news_risk_extreme_blocks_entry")
            return "block"
        elif ai.news_risk_score >= 0.70:
            tier = self._min_tier(tier, "weak")
            warn_once("news_risk_high_caps_weak")
        elif ai.news_risk_score >= 0.55:
            tier = self._min_tier(tier, "normal")
            warn_once("news_risk_elevated_caps_normal")

        if ai.orderflow_confirmation_score < 0.35:
            tier = self._min_tier(tier, "weak")
            warn_once("orderflow_confirmation_weak_caps_weak")
        if ai.dense_zone_breakout_score < 0.25:
            tier = self._min_tier(tier, "normal")
            warn_once("dense_zone_breakout_quality_weak_caps_normal")
        if ai.pattern_confirmation_score < 0.25:
            tier = self._min_tier(tier, "weak")
            warn_once("pattern_confirmation_very_weak_caps_weak")
        elif ai.pattern_confirmation_score < 0.40:
            tier = self._min_tier(tier, "normal")
            warn_once("pattern_confirmation_weak_caps_normal")
        rotation_context = ai.btc_leader_regime in {"rotation_lag", "leader_pullback"} and ai.eth_btc_rotation_score >= 0.55
        if ai.btc_leader_alignment == Alignment.CONFLICT and ai.btc_leader_impact_score >= 0.80 and not rotation_context:
            tier = self._min_tier(tier, "weak")
            warn_once("btc_leader_conflict_high_impact_caps_weak")
        elif ai.btc_leader_alignment == Alignment.CONFLICT and ai.btc_leader_impact_score >= 0.45 and not rotation_context:
            tier = self._min_tier(tier, "normal")
            warn_once("btc_leader_conflict_caps_normal")
        if rotation_context:
            warn_once("btc_eth_rotation_context_prevents_false_conflict_cap")
        if ai.btc_leader_alignment in {Alignment.NEUTRAL, Alignment.UNKNOWN} and ai.btc_leader_impact_score >= 0.60:
            tier = self._min_tier(tier, "strong")
            warn_once("btc_leader_unclear_high_impact_size_not_full")
        if ai.news_alignment in {Alignment.NEUTRAL, Alignment.UNKNOWN} and ai.crypto_market_impact_score >= 0.65:
            tier = self._min_tier(tier, "normal")
            warn_once("broad_crypto_news_unclear_caps_normal")
        if ai.news_alignment in {Alignment.NEUTRAL, Alignment.UNKNOWN} and ai.symbol_news_impact_score >= 0.65:
            tier = self._min_tier(tier, "normal")
            warn_once("symbol_news_unclear_caps_normal")
        return tier

    def _tier_index(self, tier: str) -> int:
        return POSITION_TIER_ORDER.index(tier)

    def _min_tier(self, tier: PositionTier, cap: PositionTier) -> PositionTier:
        return min_tier(tier, cap)

    def _max_tier(self, tier: PositionTier, floor: PositionTier) -> PositionTier:
        return max_tier(tier, floor)

    def _reduce_tier_cap(self, tier: str) -> str:
        if tier in {"full", "strong"}:
            return "normal"
        if tier == "normal":
            return "weak"
        return tier

    def _scale_reason(self, tier: str, consensus_score: int) -> str:
        if tier == "full":
            return "full_size_by_five_score_consensus"
        if tier == "strong":
            return "strong_size_by_five_score_consensus"
        if tier == "normal":
            return "normal_size_by_five_score_consensus" if consensus_score >= 2 else "normal_size_by_score_only"
        if tier == "weak":
            return "weak_size_by_partial_consensus"
        return "blocked_by_five_score_model"

    def _attach_sizing_audit(self, decision: RiskDecision, breakdown: dict[str, float]) -> None:
        decision.sizing_policy = self.config.ai_sizing_policy
        legacy_index = int(breakdown.get("legacy_position_tier_index", 0.0))
        calibrated_index = int(breakdown.get("calibrated_position_tier_index", 0.0))
        legacy_tier = POSITION_TIER_ORDER[max(0, min(legacy_index, len(POSITION_TIER_ORDER) - 1))]
        calibrated_tier = POSITION_TIER_ORDER[max(0, min(calibrated_index, len(POSITION_TIER_ORDER) - 1))]
        decision.legacy_position_tier = legacy_tier
        decision.legacy_position_scale = POSITION_TIER_SCALE[legacy_tier]
        decision.calibrated_position_tier = calibrated_tier
        decision.calibrated_position_scale = POSITION_TIER_SCALE[calibrated_tier]
        decision.calibrated_edge_score = breakdown.get("calibrated_edge_score")
