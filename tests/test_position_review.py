from __future__ import annotations

from ai_quant_trader.core.models import (
    AggregatedOrderflow,
    AiDecision,
    Alignment,
    DenseZone,
    ExchangeConnectionStatus,
    ExchangeSafetyState,
    MarketRegime,
    PatternCandidate,
    PositionReviewConfig,
    PositionSnapshot,
    Side,
    SignalAction,
    StrategySignal,
    VetoAction,
)
from ai_quant_trader.risk.position_review import PositionReviewEngine
from ai_quant_trader.strategy.trend_state import TrendPositionState


def _signal(price: float = 110.0, kc_mid: float = 102.0) -> StrategySignal:
    return StrategySignal(
        symbol="ETH/USDT:USDT",
        timeframe="1h",
        action=SignalAction.HOLD,
        current_price=price,
        technical_evidence={
            "kc_mid": kc_mid,
            "kc_upper": 108.0,
            "kc_lower": 96.0,
            "exit_long": False,
            "exit_short": False,
        },
    )


def _position(price: float = 110.0) -> PositionSnapshot:
    return PositionSnapshot(
        symbol="ETH/USDT:USDT",
        side=Side.LONG,
        qty=1.0,
        entry_price=100.0,
        mark_price=price,
        unrealized_pnl=10.0,
    )


def _trend_state(native_stop_order_id: str | None = "stop_1") -> TrendPositionState:
    return TrendPositionState(
        symbol="ETH/USDT:USDT",
        side=Side.LONG.value,
        entry_price=100.0,
        atr_value=10.0,
        atr_stop_multiple=1.5,
        stop_loss_price=85.0,
        opened_at="2026-06-27T00:00:00+00:00",
        native_stop_order_id=native_stop_order_id,
    )


def _ai(**kwargs) -> AiDecision:
    data = {
        "symbol": "ETH/USDT:USDT",
        "regime": MarketRegime.TREND,
        "direction": Side.LONG,
        "confidence": 0.82,
        "multiplier": 1.0,
        "news_alignment": Alignment.ALIGNED,
        "orderflow_alignment": Alignment.ALIGNED,
        "btc_leader_alignment": Alignment.ALIGNED,
        "dense_zone_position": "above_value",
        "trend_confirmation_score": 0.85,
        "range_risk_score": 0.2,
        "news_risk_score": 0.2,
        "orderflow_confirmation_score": 0.8,
        "pattern_confirmation_score": 0.75,
        "dense_zone_breakout_score": 0.7,
        "veto_action": VetoAction.ALLOW,
        "brief_reason": "trend continuation",
    }
    data.update(kwargs)
    return AiDecision(**data)


def _safety() -> ExchangeSafetyState:
    return ExchangeSafetyState(
        status=ExchangeConnectionStatus.OK,
        can_open_new_entries=True,
        reason="ok",
    )


def test_position_review_shadow_add_candidate_when_trend_profit_is_validated() -> None:
    engine = PositionReviewEngine(PositionReviewConfig(enabled=True, mode="shadow", max_add_fraction=0.25))

    decision = engine.evaluate(
        _signal(),
        _position(),
        _trend_state(),
        _ai(),
        AggregatedOrderflow(symbol="ETH/USDT:USDT"),
        DenseZone(symbol="ETH/USDT:USDT", poc=100, vah=106, val=94),
        PatternCandidate(symbol="ETH/USDT:USDT", pattern_type="ascending_channel"),
        _safety(),
    )

    assert decision.action == "add_candidate"
    assert decision.shadow_only is True
    assert decision.can_add is False
    assert decision.add_qty == 0.25
    assert decision.r_multiple > 0.5
    assert "shadow_only_no_order_submitted" in decision.warnings


def test_position_review_blocks_when_price_loses_kc_mid() -> None:
    engine = PositionReviewEngine(PositionReviewConfig(enabled=True, mode="shadow"))

    decision = engine.evaluate(
        _signal(price=99.0, kc_mid=102.0),
        _position(price=99.0),
        _trend_state(),
        _ai(),
        AggregatedOrderflow(symbol="ETH/USDT:USDT"),
        DenseZone(symbol="ETH/USDT:USDT", poc=100, vah=106, val=94),
        PatternCandidate(symbol="ETH/USDT:USDT", pattern_type="ascending_channel"),
        _safety(),
    )

    assert decision.action == "blocked"
    assert decision.can_add is False
    assert "trend_structure_not_intact" in decision.reason_codes


def test_position_review_blocks_when_native_stop_is_not_verified() -> None:
    engine = PositionReviewEngine(PositionReviewConfig(enabled=True, mode="shadow", require_native_stop_verified=True))

    decision = engine.evaluate(
        _signal(),
        _position(),
        _trend_state(native_stop_order_id=None),
        _ai(),
        AggregatedOrderflow(symbol="ETH/USDT:USDT"),
        DenseZone(symbol="ETH/USDT:USDT", poc=100, vah=106, val=94),
        PatternCandidate(symbol="ETH/USDT:USDT", pattern_type="ascending_channel"),
        _safety(),
    )

    assert decision.action == "blocked"
    assert decision.can_add is False
    assert "native_stop_not_verified" in decision.reason_codes
