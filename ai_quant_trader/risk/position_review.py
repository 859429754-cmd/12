from __future__ import annotations

from ai_quant_trader.core.models import (
    AggregatedOrderflow,
    AiDecision,
    Alignment,
    DenseZone,
    ExchangeSafetyState,
    HealthStatus,
    PatternCandidate,
    PositionReviewConfig,
    PositionReviewDecision,
    PositionSnapshot,
    Side,
    SignalAction,
    StrategySignal,
)
from ai_quant_trader.strategy.trend_state import TrendPositionState


class PositionReviewEngine:
    """Closed-candle position review for shadow add-on decisions.

    This module does not submit orders. It only decides whether the current
    position is strong enough to deserve an audited add-on candidate.
    """

    def __init__(self, config: PositionReviewConfig):
        self.config = config

    def evaluate(
        self,
        signal: StrategySignal,
        position: PositionSnapshot,
        trend_state: TrendPositionState | None,
        ai: AiDecision,
        orderflow: AggregatedOrderflow,
        dense_zone: DenseZone,
        pattern: PatternCandidate,
        exchange_safety: ExchangeSafetyState,
        *,
        data_health_status: HealthStatus = HealthStatus.OK,
    ) -> PositionReviewDecision:
        cfg = self.config
        base = {
            "symbol": signal.symbol,
            "side": position.side,
            "mode": cfg.mode,
            "current_price": float(position.mark_price or signal.current_price or 0.0),
            "entry_price": float(position.entry_price or 0.0),
            "unrealized_pnl": float(position.unrealized_pnl or 0.0),
        }
        if not cfg.enabled or cfg.mode == "disabled":
            return PositionReviewDecision(**base, action="disabled", reason="position_review_disabled")
        if position.side == Side.FLAT or abs(position.qty) <= 0:
            return PositionReviewDecision(**base, action="no_position", reason="no_open_position")
        if trend_state is None:
            return PositionReviewDecision(**base, action="blocked", reason="missing_trend_state", reason_codes=["missing_trend_state"])
        if trend_state.side != position.side.value:
            return PositionReviewDecision(
                **base,
                action="blocked",
                reason="trend_state_position_side_mismatch",
                reason_codes=["trend_state_position_side_mismatch"],
            )

        evidence = signal.technical_evidence or {}
        kcm = _float_or_none(evidence.get("kc_mid"))
        kcu = _float_or_none(evidence.get("kc_upper"))
        kcl = _float_or_none(evidence.get("kc_lower"))
        current_price = float(position.mark_price or signal.current_price or 0.0)
        entry = float(trend_state.entry_price)
        stop = float(trend_state.stop_loss_price)
        atr = max(float(trend_state.atr_value), 1e-9)
        risk_per_unit = abs(entry - stop)
        if risk_per_unit <= 0:
            return PositionReviewDecision(
                **base,
                action="blocked",
                stop_loss_price=stop,
                kc_mid=kcm,
                kc_upper=kcu,
                kc_lower=kcl,
                reason="invalid_initial_risk",
                reason_codes=["invalid_initial_risk"],
            )

        signed_profit = current_price - entry if position.side == Side.LONG else entry - current_price
        r_multiple = signed_profit / risk_per_unit
        atr_profit_multiple = signed_profit / atr
        trend_intact = self._trend_intact(position.side, current_price, kcm, evidence)
        profit_validated = r_multiple >= cfg.min_profit_r or atr_profit_multiple >= cfg.min_profit_atr
        native_stop_verified = bool(trend_state.native_stop_order_id) or not cfg.require_native_stop_verified
        confirmation_codes = self._confirmation_codes(ai)
        blockers: list[str] = []
        if signal.action in {SignalAction.EXIT_LONG, SignalAction.EXIT_SHORT}:
            blockers.append("exit_signal_present")
        if not exchange_safety.can_open_new_entries:
            blockers.append("exchange_safety_blocks_review")
        if data_health_status == HealthStatus.BLOCK:
            blockers.append("data_health_blocks_review")
        if not native_stop_verified:
            blockers.append("native_stop_not_verified")
        if not trend_intact:
            blockers.append("trend_structure_not_intact")
        if not profit_validated:
            blockers.append("profit_not_validated")
        if len(confirmation_codes) < cfg.min_confirmation_count:
            blockers.append("insufficient_confirmation_count")
        if ai.news_alignment == Alignment.CONFLICT:
            blockers.append("news_conflict")
        if ai.news_risk_score > cfg.max_news_risk_score:
            blockers.append("news_risk_too_high")
        if ai.range_risk_score > cfg.max_range_risk_score:
            blockers.append("range_risk_too_high")
        if ai.btc_leader_alignment == Alignment.CONFLICT and ai.btc_leader_impact_score >= 0.65:
            blockers.append("btc_leader_hard_conflict")

        payload = {
            **base,
            "stop_loss_price": stop,
            "kc_mid": kcm,
            "kc_upper": kcu,
            "kc_lower": kcl,
            "r_multiple": round(r_multiple, 4),
            "atr_profit_multiple": round(atr_profit_multiple, 4),
            "trend_intact": trend_intact,
            "profit_validated": profit_validated,
            "native_stop_verified": native_stop_verified,
            "confirmation_count": len(confirmation_codes),
            "reason_codes": confirmation_codes + blockers,
        }
        if blockers:
            return PositionReviewDecision(
                **payload,
                action="blocked",
                can_add=False,
                add_fraction=0.0,
                add_qty=0.0,
                reason=f"position_review_blocked:{blockers[0]}",
            )

        add_fraction = min(cfg.max_add_fraction, 0.5)
        return PositionReviewDecision(
            **payload,
            action="add_candidate",
            can_add=cfg.mode == "live_addon",
            shadow_only=cfg.mode != "live_addon",
            add_fraction=add_fraction,
            add_qty=abs(float(position.qty)) * add_fraction,
            reason="trend_position_addon_candidate_shadow" if cfg.mode == "shadow" else "trend_position_addon_candidate",
            warnings=["shadow_only_no_order_submitted"] if cfg.mode == "shadow" else [],
        )

    def _trend_intact(self, side: Side, current_price: float, kc_mid: float | None, evidence: dict) -> bool:
        if kc_mid is None:
            return False
        if side == Side.LONG:
            return current_price > kc_mid and not bool(evidence.get("exit_long"))
        if side == Side.SHORT:
            return current_price < kc_mid and not bool(evidence.get("exit_short"))
        return False

    def _confirmation_codes(self, ai: AiDecision) -> list[str]:
        cfg = self.config
        codes: list[str] = []
        if ai.orderflow_confirmation_score >= cfg.min_orderflow_confirmation:
            codes.append("orderflow_confirmed")
        if ai.pattern_confirmation_score >= cfg.min_pattern_confirmation:
            codes.append("pattern_confirmed")
        if ai.dense_zone_breakout_score >= cfg.min_dense_zone_breakout:
            codes.append("dense_zone_confirmed")
        return codes


def _float_or_none(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
