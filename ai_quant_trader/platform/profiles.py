from __future__ import annotations

from typing import Any

from ai_quant_trader.core.models import TrendStrategyConfig
from ai_quant_trader.core.state import RuntimeState


def strategy_execution_contract(config: TrendStrategyConfig) -> dict[str, Any]:
    """Describe the live/backtest contract the console must not silently change."""
    return {
        "signal_candle": "closed_1h_bar",
        "entry_fill": "next_tradeable_open",
        "entry_rule": "keltner_breakout_with_enabled_filters",
        "exit_rule": "reverse_cross_of_keltner_midline",
        "stop_rule": "fixed_atr_stop_from_entry",
        "stop_atr_source": "previous_closed_bar_atr",
        "position_sizing": "equity * position_fraction * leverage",
        "same_direction_add": "blocked",
        "reversal": "close_opposite_then_open_new_signal",
        "max_leverage": 4.0,
        "active_filters": {
            "volume": config.use_volume_filter,
            "momentum": config.momentum_filter,
        },
    }


def backtest_defaults(config: TrendStrategyConfig, max_leverage: float = 4.0) -> dict[str, Any]:
    return {
        "timeframe": "1h",
        "initial_equity": 200.0,
        "fee_rate": 0.0004,
        "slippage_bps": 0.0,
        "funding_rate_per_8h": 0.0,
        "min_order_qty": 0.0,
        "max_volume_participation": 1.0,
        "leverage": max_leverage,
        "position_fraction": config.position_fraction,
        "notional_multiple": config.position_fraction * max_leverage,
        "data_source": "binance",
    }


def optimization_defaults(config: TrendStrategyConfig) -> dict[str, Any]:
    return {
        "validation_ratio": 0.3,
        "min_trades": 20,
        "max_candidates": 512,
        "top_n": 10,
        "kc_lengths": [config.kc_length],
        "kc_scalars": sorted({2.4, 2.6, config.kc_scalar, 3.0, 3.2}),
        "atr_lengths": [config.atr_length],
        "vma_lengths": [config.vma_length],
        "volume_multiples": sorted({2.0, 2.2, config.volume_multiple, 2.8, 3.0}),
        "atr_stop_multiples": sorted({1.2, config.atr_stop_multiple, 1.8, 2.0}),
        "position_fractions": [config.position_fraction],
        "use_volume_filters": [config.use_volume_filter],
        "momentum_filters": [config.momentum_filter],
        "kdj_lengths": sorted({7, config.kdj_length, 14}),
    }


def build_strategy_profile(
    *,
    symbol: str,
    config: TrendStrategyConfig,
    state: RuntimeState,
    max_leverage: float = 4.0,
) -> dict[str, Any]:
    contract = strategy_execution_contract(config)
    contract["max_leverage"] = max_leverage
    defaults = backtest_defaults(config, max_leverage=max_leverage)
    return {
        "symbol": symbol,
        "profile_name": config.profile_name,
        "strategy_type": "trend",
        "enabled": config.enabled,
        "opening_authorized": symbol in state.enabled_symbols,
        "report_enabled": symbol in state.report_symbols,
        "live_ready": config.enabled and symbol in state.enabled_symbols and not state.opening_paused,
        "params": config.model_dump(mode="json"),
        "backtest_defaults": defaults,
        "optimization_defaults": optimization_defaults(config),
        "execution_contract": contract,
        "notes": "research_only" if not config.enabled else "eligible_for_risk_checked_execution",
    }
