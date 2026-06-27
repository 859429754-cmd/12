from __future__ import annotations

from typing import Any

from ai_quant_trader.core.models import AiDecision, LiveFactorSnapshot, RiskDecision, StrategySignal


FORBIDDEN_OUTCOME_KEYS = {
    "pnl",
    "realized_pnl",
    "unrealized_pnl",
    "mae",
    "mfe",
    "mae_pct",
    "mfe_pct",
    "return_pct",
    "exit_price",
    "exit_reason",
    "exit_time",
    "closed_at",
    "winner",
}


def build_live_factor_snapshot(
    *,
    signal: StrategySignal,
    ai: AiDecision,
    risk: RiskDecision,
    orderflow: Any | None = None,
    source: str = "trading_cycle",
) -> LiveFactorSnapshot:
    """Archive live-visible factors for later post-trade attribution.

    The snapshot is intentionally pre-outcome only. It records what the strategy,
    AI layer, and RiskManager knew before execution so later research can join it
    to closed-trade outcomes without look-ahead leakage.
    """

    live_factors = _strip_outcome_keys(
        {
            "signal_action": _enum_value(signal.action),
            "signal_strength": signal.signal_strength,
            "technical_evidence": signal.technical_evidence,
            "ai_direction": _enum_value(ai.direction),
            "ai_regime": _enum_value(ai.regime),
            "ai_confidence": ai.confidence,
            "ai_multiplier": ai.multiplier,
            "ai_veto_action": _enum_value(ai.veto_action),
            "news_alignment": _enum_value(ai.news_alignment),
            "news_risk_score": ai.news_risk_score,
            "news_direction_alignment_score": ai.news_direction_alignment_score,
            "crypto_market_impact_score": ai.crypto_market_impact_score,
            "symbol_news_impact_score": ai.symbol_news_impact_score,
            "btc_leader_alignment": _enum_value(ai.btc_leader_alignment),
            "btc_leader_regime": ai.btc_leader_regime,
            "btc_leader_impact_score": ai.btc_leader_impact_score,
            "eth_btc_rotation_score": ai.eth_btc_rotation_score,
            "orderflow_alignment": _enum_value(ai.orderflow_alignment),
            "orderflow_confirmation_score": ai.orderflow_confirmation_score,
            "orderflow_data_quality": _number_attr(orderflow, "data_quality"),
            "orderflow_source_count": _number_attr(orderflow, "source_count"),
            "orderflow_spread_bps": _number_attr(orderflow, "spread_bps"),
            "orderflow_depth_usd": _number_attr(orderflow, "depth_usd"),
            "orderflow_large_trade_events": _number_attr(orderflow, "large_trade_events"),
            "dense_zone_position": ai.dense_zone_position,
            "dense_zone_breakout_score": ai.dense_zone_breakout_score,
            "pattern_type": ai.pattern_type,
            "pattern_confirmation_score": ai.pattern_confirmation_score,
            "trend_confirmation_score": ai.trend_confirmation_score,
            "range_risk_score": ai.range_risk_score,
        }
    )
    warnings = list(ai.data_quality_warnings or []) + list(risk.warnings or [])
    return LiveFactorSnapshot(
        symbol=signal.symbol,
        signal_action=signal.action,
        signal_strength=signal.signal_strength,
        position_tier=risk.position_tier,
        position_scale=risk.position_scale,
        sizing_policy=risk.sizing_policy,
        score_breakdown=dict(risk.score_breakdown or {}),
        live_factors=live_factors,
        archive_status="shadow_only",
        source=source,  # type: ignore[arg-type]
        warnings=warnings,
    )


def _strip_outcome_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_outcome_keys(v) for k, v in value.items() if str(k).lower() not in FORBIDDEN_OUTCOME_KEYS}
    if isinstance(value, list):
        return [_strip_outcome_keys(item) for item in value]
    return value


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _number_attr(obj: Any | None, name: str) -> float:
    if obj is None:
        return 0.0
    try:
        return float(getattr(obj, name, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
