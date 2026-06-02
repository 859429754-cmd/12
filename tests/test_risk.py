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


def test_major_news_direction_conflict_is_hard_block() -> None:
    state = RuntimeState(opening_paused=False, enabled_symbols={"ETH/USDT:USDT"})
    manager = RiskManager(RiskConfig(max_total_leverage=4), state)
    signal = _signal().model_copy(update={"technical_evidence": {"major_news_context": True}})

    decision = manager.evaluate(signal, _ai(news_alignment=Alignment.CONFLICT), 1000, [])

    assert not decision.allowed
    assert decision.reason == "major_news_direction_conflict"


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
