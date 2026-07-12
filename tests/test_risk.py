from __future__ import annotations

from ai_quant_trader.core.models import (
    AiDecision,
    Alignment,
    MarketRegime,
    PositionSnapshot,
    RiskConfig,
    Side,
    SignalAction,
    StrategySignal,
    VetoAction,
)
from ai_quant_trader.core.state import RuntimeState
from ai_quant_trader.risk.manager import RiskManager
from ai_quant_trader.risk.sizing import calibrated_v21_profit_loss_policy


def _signal() -> StrategySignal:
    return StrategySignal(
        symbol="ETH/USDT:USDT",
        timeframe="1h",
        action=SignalAction.LONG,
        current_price=100,
        suggested_qty=100,
        signal_strength=0.9,
    )


def _ai(**kwargs) -> AiDecision:
    data = {
        "symbol": "ETH/USDT:USDT",
        "regime": MarketRegime.TREND,
        "direction": Side.LONG,
        "confidence": 0.8,
        "multiplier": 1.0,
        "news_alignment": Alignment.ALIGNED,
        "orderflow_alignment": Alignment.ALIGNED,
        "dense_zone_position": "above_value",
        "trend_confirmation_score": 0.86,
        "range_risk_score": 0.15,
        "news_risk_score": 0.20,
        "btc_leader_alignment": Alignment.ALIGNED,
        "crypto_market_impact_score": 0.35,
        "btc_leader_impact_score": 0.65,
        "symbol_news_impact_score": 0.35,
        "pattern_confirmation_score": 0.82,
        "orderflow_confirmation_score": 0.85,
        "dense_zone_breakout_score": 0.75,
        "veto_action": VetoAction.ALLOW,
        "brief_reason": "三项同向",
    }
    data.update(kwargs)
    return AiDecision(**data)


def test_cold_start_blocks_entry() -> None:
    manager = RiskManager(RiskConfig(), RuntimeState(opening_paused=True))
    decision = manager.evaluate(_signal(), _ai(), 1000, [])
    assert not decision.allowed
    assert decision.reason == "cold_start_or_symbol_not_authorized"


def test_ai_veto_blocks_entry() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(), state)
    decision = manager.evaluate(_signal(), _ai(veto_action=VetoAction.BLOCK), 1000, [])
    assert not decision.allowed
    assert decision.reason == "ai_veto_block"


def test_pure_strategy_mode_uses_strategy_qty_and_ignores_ai_overlay() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=4), state)
    signal = _signal().model_copy(
        update={
            "suggested_qty": 20.0,
            "technical_evidence": {"strategy_allowed": "range"},
        }
    )

    decision = manager.evaluate(
        signal,
        _ai(
            direction=Side.SHORT,
            regime=MarketRegime.RANGE,
            confidence=0.0,
            veto_action=VetoAction.BLOCK,
            orderflow_alignment=Alignment.CONFLICT,
            news_alignment=Alignment.CONFLICT,
        ),
        1000,
        [],
        decision_mode="pure_strategy",
    )

    assert decision.allowed is True
    assert decision.decision_mode == "pure_strategy"
    assert decision.sizing_basis == "pure_strategy_signal"
    assert decision.sizing_policy == "pure_strategy"
    assert decision.strategy_baseline_notional == 2000.0
    assert decision.target_notional == 2000.0
    assert decision.clipped_qty == 20.0
    assert decision.position_scale == 0.5
    assert decision.position_tier == "normal"
    assert decision.reason == "pure_strategy_signal_allowed"


def test_pure_strategy_mode_keeps_authorization_and_leverage_hard_gates() -> None:
    blocked = RiskManager(RiskConfig(max_total_leverage=4), RuntimeState(opening_paused=True)).evaluate(
        _signal(),
        _ai(veto_action=VetoAction.ALLOW),
        1000,
        [],
        decision_mode="pure_strategy",
    )
    assert blocked.allowed is False
    assert blocked.reason == "cold_start_or_symbol_not_authorized"

    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=4), state)
    existing = [PositionSnapshot(symbol="BTC/USDT:USDT", side=Side.LONG, qty=30, mark_price=100)]
    clipped = manager.evaluate(_signal(), _ai(), 1000, existing, decision_mode="pure_strategy")
    assert clipped.allowed is True
    assert clipped.target_notional == 1000.0
    assert clipped.clipped_qty == 10.0


def test_four_x_hard_cap_clips_position() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=4), state)
    existing = [PositionSnapshot(symbol="BTC/USDT:USDT", side=Side.LONG, qty=30, mark_price=100)]
    decision = manager.evaluate(_signal(), _ai(), 1000, existing)
    assert decision.allowed
    assert decision.target_notional <= 1000
    assert decision.clipped_qty <= 10


def test_configurable_hard_cap_allows_higher_account_leverage() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=10), state)
    existing = [PositionSnapshot(symbol="BTC/USDT:USDT", side=Side.LONG, qty=30, mark_price=100)]
    decision = manager.evaluate(_signal(), _ai(), 1000, existing)
    assert decision.allowed
    assert decision.max_total_notional == 10_000
    assert decision.target_notional > 1000


def test_same_direction_position_blocks_addon() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=4), state)
    existing = [PositionSnapshot(symbol="ETH/USDT:USDT", side=Side.LONG, qty=1, mark_price=100)]
    decision = manager.evaluate(_signal(), _ai(), 1000, existing)
    assert not decision.allowed
    assert decision.reason == "same_direction_position_exists"


def test_local_range_regime_blocks_trend_entry_before_ai() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=4), state)
    signal = _signal().model_copy(update={"technical_evidence": {"strategy_allowed": "range"}})

    decision = manager.evaluate(signal, _ai(), 1000, [])

    assert not decision.allowed
    assert decision.reason == "regime_blocks_trend_strategy:range"


def test_ai_reduce_scales_position_down() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=4), state)
    decision = manager.evaluate(_signal(), _ai(veto_action=VetoAction.REDUCE), 1000, [])
    assert decision.allowed
    assert decision.position_tier == "normal"
    assert decision.position_scale == 0.5
    assert decision.decision_score > 0
    assert decision.clipped_qty < _signal().suggested_qty


def test_low_combined_ai_score_blocks_even_with_technical_signal() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(min_confidence_to_trade=0.55), state)
    weak_signal = _signal().model_copy(update={"signal_strength": 0.2})
    decision = manager.evaluate(
        weak_signal,
        _ai(
            confidence=0.56,
            news_alignment=Alignment.NEUTRAL,
            orderflow_alignment=Alignment.UNKNOWN,
            trend_confirmation_score=0.30,
            range_risk_score=0.75,
            news_risk_score=0.75,
            orderflow_confirmation_score=0.20,
            dense_zone_breakout_score=0.20,
        ),
        1000,
        [],
    )
    assert not decision.allowed
    assert decision.reason == "combined_decision_score_too_low"


def test_full_size_requires_high_confidence_multi_factor_consensus() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=4), state)
    strong_signal = _signal().model_copy(
        update={
            "signal_strength": 0.96,
            "technical_evidence": {"volume_multiple": 2.6, "breakout_atr": 1.2},
        }
    )
    strong_ai = _ai(confidence=0.92, dense_zone_position="above_value")
    decision = manager.evaluate(strong_signal, strong_ai, 1000, [])
    assert decision.allowed
    assert decision.position_tier == "full"
    assert decision.position_scale == 1.0
    assert decision.reason == "full_size_by_five_score_consensus"


def test_ai_can_amplify_from_strategy_baseline_to_account_risk_tier() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=4, ai_dynamic_position_sizing=True), state)
    half_size_signal = _signal().model_copy(update={"suggested_qty": 20.0, "signal_strength": 0.96})

    decision = manager.evaluate(half_size_signal, _ai(confidence=0.92), 1000, [])

    assert decision.allowed
    assert decision.position_tier == "full"
    assert decision.position_scale == 1.0
    assert decision.sizing_basis == "account_risk_cap"
    assert decision.strategy_baseline_notional == 2000.0
    assert decision.ai_desired_notional == 4000.0
    assert decision.target_notional == 4000.0
    assert decision.clipped_qty == 40.0


def test_ai_dynamic_sizing_does_not_create_qty_when_strategy_qty_is_zero() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=4, ai_dynamic_position_sizing=True), state)
    zero_qty_signal = _signal().model_copy(update={"suggested_qty": 0.0, "signal_strength": 0.96})

    decision = manager.evaluate(zero_qty_signal, _ai(confidence=0.92), 1000, [])

    assert not decision.allowed
    assert decision.sizing_basis == "account_risk_cap"
    assert decision.strategy_baseline_notional == 0.0
    assert decision.ai_desired_notional == 0.0
    assert decision.reason == "qty_clipped_to_zero"


def test_legacy_strategy_signal_sizing_can_be_kept_by_config() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=4, ai_dynamic_position_sizing=False), state)
    half_size_signal = _signal().model_copy(update={"suggested_qty": 20.0, "signal_strength": 0.96})

    decision = manager.evaluate(half_size_signal, _ai(confidence=0.92), 1000, [])

    assert decision.allowed
    assert decision.sizing_basis == "strategy_signal"
    assert decision.strategy_baseline_notional == 2000.0
    assert decision.ai_desired_notional == 2000.0
    assert decision.target_notional == 2000.0
    assert decision.clipped_qty == 20.0


def test_five_score_model_maps_to_clear_position_tiers() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=4), state)

    weak = manager.evaluate(
        _signal().model_copy(update={"signal_strength": 0.78}),
        _ai(
            trend_confirmation_score=0.55,
            range_risk_score=0.45,
            news_risk_score=0.45,
            orderflow_confirmation_score=0.45,
            dense_zone_breakout_score=0.45,
        ),
        1000,
        [],
    )
    normal = manager.evaluate(
        _signal().model_copy(update={"signal_strength": 0.82}),
        _ai(
            trend_confirmation_score=0.70,
            range_risk_score=0.28,
            news_risk_score=0.28,
            orderflow_confirmation_score=0.60,
            dense_zone_breakout_score=0.60,
        ),
        1000,
        [],
    )
    strong = manager.evaluate(_signal(), _ai(), 1000, [])

    assert (weak.position_tier, weak.position_scale) == ("weak", 0.25)
    assert (normal.position_tier, normal.position_scale) == ("normal", 0.5)
    assert (strong.position_tier, strong.position_scale) == ("strong", 0.75)
    assert "trend_confirmation_score" in strong.score_breakdown


def test_factor_ranked_policy_promotes_high_orderflow_participation_quality() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=4), state)
    signal = _signal().model_copy(
        update={
            "signal_strength": 0.82,
            "technical_evidence": {"volume_multiple": 3.4, "breakout_atr": 0.45},
        }
    )
    base_ai = {
        "confidence": 0.82,
        "trend_confirmation_score": 0.65,
        "range_risk_score": 0.28,
        "news_risk_score": 0.25,
        "pattern_confirmation_score": 0.60,
        "dense_zone_breakout_score": 0.60,
        "btc_leader_impact_score": 0.35,
    }

    low_flow = manager.evaluate(
        signal,
        _ai(**base_ai, orderflow_confirmation_score=0.35, orderflow_alignment=Alignment.NEUTRAL),
        1000,
        [],
    )
    high_flow = manager.evaluate(
        signal,
        _ai(**base_ai, orderflow_confirmation_score=0.95, orderflow_alignment=Alignment.NEUTRAL),
        1000,
        [],
    )

    assert low_flow.allowed
    assert high_flow.allowed
    assert low_flow.position_tier in {"weak", "normal"}
    assert high_flow.position_tier == "strong"
    assert high_flow.decision_score - low_flow.decision_score >= 0.10
    assert high_flow.score_breakdown["weight_orderflow_confirmation_score"] > high_flow.score_breakdown["weight_trend_confirmation_score"]


def test_orderflow_direction_alignment_alone_does_not_create_full_size() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=4), state)
    signal = _signal().model_copy(
        update={
            "signal_strength": 0.92,
            "technical_evidence": {"volume_multiple": 4.0, "breakout_atr": 0.7},
        }
    )

    decision = manager.evaluate(
        signal,
        _ai(
            confidence=0.92,
            trend_confirmation_score=0.82,
            range_risk_score=0.52,
            news_risk_score=0.20,
            pattern_confirmation_score=0.35,
            orderflow_alignment=Alignment.ALIGNED,
            orderflow_confirmation_score=0.98,
            dense_zone_breakout_score=0.82,
        ),
        1000,
        [],
    )

    assert decision.allowed
    assert decision.position_tier == "normal"
    assert "pattern_confirmation_weak_caps_normal" in decision.warnings


def test_news_direction_confirmation_adds_score_but_neutral_low_risk_news_does_not() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=4), state)
    signal = _signal().model_copy(update={"signal_strength": 0.75})
    base_ai = {
        "confidence": 0.82,
        "trend_confirmation_score": 0.70,
        "range_risk_score": 0.25,
        "news_risk_score": 0.15,
        "crypto_market_impact_score": 0.25,
        "symbol_news_impact_score": 0.25,
        "pattern_confirmation_score": 0.65,
        "orderflow_confirmation_score": 0.70,
        "dense_zone_breakout_score": 0.65,
        "btc_leader_impact_score": 0.35,
    }

    neutral = manager.evaluate(
        signal,
        _ai(
            **base_ai,
            news_alignment=Alignment.NEUTRAL,
            news_direction_alignment_score=0.0,
        ),
        1000,
        [],
    )
    aligned = manager.evaluate(
        signal,
        _ai(
            **base_ai,
            news_alignment=Alignment.ALIGNED,
            news_direction_alignment_score=0.85,
        ),
        1000,
        [],
    )

    assert neutral.allowed
    assert aligned.allowed
    assert neutral.score_breakdown["news_direction_alignment_score"] == 0.0
    assert aligned.score_breakdown["news_direction_alignment_score"] == 0.85
    assert aligned.decision_score - neutral.decision_score >= 0.10
    assert neutral.position_tier == "weak"
    assert aligned.position_tier == "strong"
    assert aligned.score_breakdown["weight_news_direction_alignment_score"] > aligned.score_breakdown["weight_pattern_confirmation_score"]


def test_calibrated_sizing_policy_can_promote_but_only_one_tier_above_legacy() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(
        RiskConfig(
            max_total_leverage=4,
            ai_sizing_policy="calibrated_v1_controlled",
            calibrated_max_tier_lift=1,
        ),
        state,
    )
    signal = _signal().model_copy(update={"signal_strength": 0.20})

    decision = manager.evaluate(
        signal,
        _ai(
            confidence=0.88,
            trend_confirmation_score=0.20,
            range_risk_score=0.10,
            news_risk_score=0.10,
            orderflow_confirmation_score=0.95,
            dense_zone_breakout_score=0.90,
            pattern_confirmation_score=0.90,
        ),
        1000,
        [],
    )

    assert decision.allowed
    assert decision.sizing_policy == "calibrated_v1_controlled"
    assert decision.legacy_position_tier == "normal"
    assert decision.calibrated_position_tier == "full"
    assert decision.position_tier == "strong"
    assert decision.position_scale == 0.75
    assert decision.calibrated_edge_score is not None
    assert "calibrated_tier_lift_limited:full->strong:legacy=normal" in decision.warnings


def test_calibrated_sizing_policy_falls_back_to_legacy_when_factor_coverage_is_low() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(
        RiskConfig(
            max_total_leverage=4,
            ai_sizing_policy="calibrated_v1_controlled",
            calibrated_min_factor_coverage=0.90,
        ),
        state,
    )

    decision = manager.evaluate(
        _signal().model_copy(update={"signal_strength": 0.95}),
        _ai(
            confidence=0.92,
            trend_confirmation_score=0.0,
            range_risk_score=0.20,
            news_risk_score=0.20,
            orderflow_confirmation_score=0.0,
            dense_zone_breakout_score=0.0,
            pattern_confirmation_score=0.0,
        ),
        1000,
        [],
    )

    assert decision.sizing_policy == "calibrated_v1_controlled"
    assert decision.position_tier == decision.legacy_position_tier
    assert "calibrated_v1_fallback_to_legacy_factor_coverage_low" in decision.warnings
    assert "calibrated_factor_coverage_low_fallback_legacy" in decision.warnings


def test_calibrated_v2_loss_aware_can_be_selected_as_live_sizing_policy() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(
        RiskConfig(max_total_leverage=4, ai_sizing_policy="calibrated_v2_loss_aware"),
        state,
    )

    decision = manager.evaluate(
        _signal().model_copy(update={"signal_strength": 0.96}),
        _ai(
            confidence=0.92,
            trend_confirmation_score=0.76,
            range_risk_score=0.18,
            news_risk_score=0.18,
            orderflow_confirmation_score=0.82,
            dense_zone_breakout_score=0.65,
            pattern_confirmation_score=0.90,
        ),
        1000,
        [],
    )

    assert decision.allowed
    assert decision.sizing_policy == "calibrated_v2_loss_aware"
    assert decision.calibrated_position_tier in {"strong", "full"}
    assert decision.score_breakdown["loss_risk_score"] < 0.30


def test_hybrid_subjective_guarded_v2_promotes_only_one_tier_above_v2_base() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(
        RiskConfig(max_total_leverage=4, ai_sizing_policy="hybrid_subjective_guarded_v2"),
        state,
    )

    decision = manager.evaluate(
        _signal().model_copy(update={"signal_strength": 0.30}),
        _ai(
            confidence=0.92,
            subjective_position_tier="full",
            subjective_position_confidence=0.86,
            trend_confirmation_score=0.55,
            range_risk_score=0.22,
            news_risk_score=0.20,
            orderflow_confirmation_score=0.74,
            dense_zone_breakout_score=0.50,
            pattern_confirmation_score=0.72,
        ),
        1000,
        [],
    )

    assert decision.allowed
    assert decision.sizing_policy == "hybrid_subjective_guarded_v2"
    assert decision.subjective_position_tier == "full"
    assert decision.position_tier == "strong"
    assert decision.score_breakdown["hybrid_base_position_tier_index"] == 2
    assert "hybrid_subjective_promoted_one_tier:normal->strong:raw=full" in decision.warnings


def test_hybrid_subjective_guarded_v2_can_reduce_below_v2_base() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(
        RiskConfig(max_total_leverage=4, ai_sizing_policy="hybrid_subjective_guarded_v2"),
        state,
    )

    decision = manager.evaluate(
        _signal().model_copy(update={"signal_strength": 0.96}),
        _ai(
            confidence=0.88,
            subjective_position_tier="weak",
            subjective_position_confidence=0.82,
            trend_confirmation_score=0.78,
            range_risk_score=0.28,
            news_risk_score=0.24,
            orderflow_confirmation_score=0.86,
            dense_zone_breakout_score=0.70,
            pattern_confirmation_score=0.90,
        ),
        1000,
        [],
    )

    assert decision.allowed
    assert decision.subjective_position_tier == "weak"
    assert decision.position_tier == "weak"
    assert "hybrid_subjective_reduced_tier" in decision.warnings


def test_calibrated_v21_profit_loss_policy_separates_expansion_and_loss_risk() -> None:
    clean = calibrated_v21_profit_loss_policy(
        {
            "technical_signal_score": 0.96,
            "orderflow_confirmation_score": 0.92,
            "pattern_confirmation_score": 0.92,
            "dense_zone_breakout_score": 0.76,
            "range_safety_score": 0.86,
            "trend_confirmation_score": 0.82,
            "news_direction_alignment_score": 0.70,
            "news_safety_score": 0.80,
            "btc_leader_score": 0.70,
            "eth_btc_rotation_score": 0.65,
        },
        min_trade_score=0.55,
        min_factor_coverage=0.70,
    )
    noisy = calibrated_v21_profit_loss_policy(
        {
            "technical_signal_score": 0.94,
            "orderflow_confirmation_score": 0.46,
            "pattern_confirmation_score": 0.34,
            "dense_zone_breakout_score": 0.32,
            "range_safety_score": 0.30,
            "trend_confirmation_score": 0.62,
            "news_direction_alignment_score": 0.0,
            "news_safety_score": 0.45,
            "btc_leader_score": 0.50,
            "eth_btc_rotation_score": 0.50,
        },
        min_trade_score=0.55,
        min_factor_coverage=0.70,
    )

    assert clean.profit_expansion_score is not None
    assert noisy.loss_risk_score is not None
    assert clean.profit_expansion_score > noisy.profit_expansion_score
    assert noisy.loss_risk_score > clean.loss_risk_score
    assert clean.tier in {"strong", "full"}
    assert noisy.tier in {"block", "weak", "normal"}


def test_high_impact_news_conflict_remains_hard_block() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=4), state)

    decision = manager.evaluate(
        _signal(),
        _ai(
            news_alignment=Alignment.CONFLICT,
            news_direction_alignment_score=1.0,
            crypto_market_impact_score=0.85,
            symbol_news_impact_score=0.80,
            confidence=0.95,
            trend_confirmation_score=0.95,
            range_risk_score=0.05,
            news_risk_score=0.05,
            orderflow_confirmation_score=0.95,
            dense_zone_breakout_score=0.95,
            pattern_confirmation_score=0.95,
        ),
        1000,
        [],
    )

    assert not decision.allowed
    assert decision.reason == "news_major_conflict"


def test_major_news_without_strategy_signal_blocks_explicitly() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=4), state)
    signal = _signal().model_copy(
        update={
            "action": SignalAction.HOLD,
            "technical_evidence": {"major_news_context": True, "major_news_event_count": 1},
        }
    )

    decision = manager.evaluate(signal, _ai(), 1000, [])

    assert not decision.allowed
    assert decision.reason == "major_news_without_strategy_signal"


def test_major_news_direction_conflict_with_direct_crypto_impact_is_hard_block() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=4), state)
    signal = _signal().model_copy(update={"technical_evidence": {"major_news_context": True}})

    decision = manager.evaluate(
        signal,
        _ai(
            news_alignment=Alignment.CONFLICT,
            crypto_market_impact_score=0.70,
            symbol_news_impact_score=0.65,
        ),
        1000,
        [],
    )

    assert not decision.allowed
    assert decision.reason == "major_news_direction_conflict"


def test_major_news_conflict_with_low_crypto_impact_caps_weak_instead_of_blocking() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=4), state)
    signal = _signal().model_copy(
        update={
            "signal_strength": 1.0,
            "technical_evidence": {"major_news_context": True, "volume_multiple": 4.4, "breakout_atr": 1.3},
        }
    )

    decision = manager.evaluate(
        signal,
        _ai(
            news_alignment=Alignment.CONFLICT,
            veto_action=VetoAction.REDUCE,
            confidence=0.65,
            trend_confirmation_score=0.80,
            range_risk_score=0.07,
            news_risk_score=0.90,
            crypto_market_impact_score=0.0,
            btc_leader_impact_score=0.0,
            symbol_news_impact_score=0.0,
            orderflow_alignment=Alignment.ALIGNED,
            orderflow_confirmation_score=0.80,
            dense_zone_breakout_score=0.75,
            pattern_confirmation_score=0.50,
        ),
        386.94,
        [],
    )

    assert decision.allowed
    assert decision.position_tier == "weak"
    assert decision.reason == "weak_size_by_partial_consensus"
    assert "news_conflict_low_direct_impact_caps_weak" in decision.warnings


def test_major_news_unknown_direction_caps_position_at_normal() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=4), state)
    signal = _signal().model_copy(
        update={"signal_strength": 0.98, "technical_evidence": {"major_news_context": True}}
    )

    decision = manager.evaluate(
        signal,
        _ai(
            confidence=0.95,
            news_alignment=Alignment.UNKNOWN,
            trend_confirmation_score=0.95,
            range_risk_score=0.05,
            news_risk_score=0.05,
            orderflow_confirmation_score=0.90,
            dense_zone_breakout_score=0.90,
        ),
        1000,
        [],
    )

    assert decision.allowed
    assert decision.position_tier == "normal"
    assert decision.position_scale == 0.5
    assert "major_news_unknown_caps_normal" in decision.warnings


def test_major_news_aligned_full_size_requires_orderflow_and_dense_zone_quality() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=4), state)
    signal = _signal().model_copy(
        update={"signal_strength": 0.98, "technical_evidence": {"major_news_context": True}}
    )

    decision = manager.evaluate(
        signal,
        _ai(
            confidence=0.95,
            trend_confirmation_score=0.95,
            range_risk_score=0.05,
            news_risk_score=0.05,
            orderflow_confirmation_score=0.70,
            dense_zone_breakout_score=0.60,
        ),
        1000,
        [],
    )

    assert decision.allowed
    assert decision.position_tier == "strong"
    assert decision.position_scale == 0.75
    assert "major_news_full_requires_orderflow_and_dense_zone_confirmation" in decision.warnings


def test_major_news_aligned_extreme_event_risk_caps_weak_not_hard_block() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=4), state)
    signal = _signal().model_copy(
        update={"signal_strength": 0.98, "technical_evidence": {"major_news_context": True}}
    )

    decision = manager.evaluate(
        signal,
        _ai(
            confidence=0.95,
            news_alignment=Alignment.ALIGNED,
            trend_confirmation_score=0.95,
            range_risk_score=0.05,
            news_risk_score=0.90,
            orderflow_confirmation_score=0.90,
            dense_zone_breakout_score=0.90,
            veto_action=VetoAction.ALLOW,
        ),
        1000,
        [],
    )

    assert decision.allowed
    assert decision.position_tier == "weak"
    assert decision.position_scale == 0.25
    assert "aligned_major_news_extreme_risk_caps_weak" in decision.warnings


def test_btc_leader_conflict_caps_but_does_not_invent_direction_or_auto_block() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=4), state)
    signal = _signal().model_copy(update={"signal_strength": 0.96})

    decision = manager.evaluate(
        signal,
        _ai(
            confidence=0.95,
            trend_confirmation_score=0.95,
            range_risk_score=0.05,
            news_risk_score=0.05,
            orderflow_confirmation_score=0.90,
            dense_zone_breakout_score=0.90,
            pattern_confirmation_score=0.90,
            btc_leader_alignment=Alignment.CONFLICT,
            btc_leader_impact_score=0.90,
        ),
        1000,
        [],
    )

    assert decision.allowed
    assert decision.position_tier == "weak"
    assert decision.position_scale == 0.25
    assert "btc_leader_conflict_high_impact_caps_weak" in decision.warnings


def test_eth_btc_rotation_context_prevents_false_btc_conflict_cap() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=4), state)
    signal = _signal().model_copy(update={"signal_strength": 0.96})

    decision = manager.evaluate(
        signal,
        _ai(
            confidence=0.95,
            trend_confirmation_score=0.95,
            range_risk_score=0.05,
            news_risk_score=0.05,
            orderflow_confirmation_score=0.90,
            dense_zone_breakout_score=0.90,
            pattern_confirmation_score=0.90,
            btc_leader_alignment=Alignment.CONFLICT,
            btc_leader_regime="rotation_lag",
            btc_leader_impact_score=0.70,
            eth_btc_rotation_score=0.78,
        ),
        1000,
        [],
    )

    assert decision.allowed
    assert decision.position_tier in {"strong", "full"}
    assert "btc_leader_conflict_caps_normal" not in decision.warnings
    assert "btc_eth_rotation_context_prevents_false_conflict_cap" in decision.warnings


def test_weak_pattern_confirmation_caps_position_even_with_strong_news_and_orderflow() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=4), state)
    signal = _signal().model_copy(update={"signal_strength": 0.96})

    decision = manager.evaluate(
        signal,
        _ai(
            confidence=0.95,
            trend_confirmation_score=0.95,
            range_risk_score=0.05,
            news_risk_score=0.05,
            orderflow_confirmation_score=0.90,
            dense_zone_breakout_score=0.90,
            pattern_confirmation_score=0.20,
        ),
        1000,
        [],
    )

    assert decision.allowed
    assert decision.position_tier == "weak"
    assert "pattern_confirmation_very_weak_caps_weak" in decision.warnings
