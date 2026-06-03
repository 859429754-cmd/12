from __future__ import annotations

from ai_quant_trader.core.models import SignalAction, StrategySignal
from scripts.live_small_signal_drill import _apply_regime_drill_override


def _signal() -> StrategySignal:
    return StrategySignal(
        symbol="ETH/USDT:USDT",
        timeframe="1h",
        action=SignalAction.SHORT,
        current_price=2000.0,
        suggested_qty=0.01,
        signal_strength=0.75,
        technical_evidence={"strategy_allowed": "none"},
    )


def test_regime_override_is_explicit_and_audited() -> None:
    unchanged = _apply_regime_drill_override(_signal(), False, "none")
    assert unchanged.technical_evidence["strategy_allowed"] == "none"

    overridden = _apply_regime_drill_override(_signal(), True, "none")
    assert overridden.technical_evidence["strategy_allowed"] == "trend"
    assert overridden.technical_evidence["live_drill_regime_override"] is True
    assert overridden.technical_evidence["live_drill_original_strategy_allowed"] == "none"
    assert overridden.technical_evidence["live_drill_override_scope"] == "execution_path_only"
