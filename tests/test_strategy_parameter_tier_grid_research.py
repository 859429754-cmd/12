from __future__ import annotations

from ai_quant_trader.core.models import TrendStrategyConfig
from scripts.strategy_parameter_tier_grid_research import build_report, overfit_flags, strategy_grid


def test_strategy_grid_can_search_reasonable_wide_ranges() -> None:
    base = TrendStrategyConfig(volume_multiple=2.5, atr_stop_multiple=1.5, position_fraction=1.0)

    rows = strategy_grid(
        base,
        kc_lengths=[16, 20],
        kc_scalars=[2.2, 2.8],
        volume_multiples=[2.0, 2.5],
        atr_stop_multiples=[1.0, 1.5],
    )

    assert len(rows) == 16
    assert any(item["kc_length"] == 20 and item["volume_multiple"] == 2.5 for item in rows)
    assert all(item["position_fraction"] == 1.0 for item in rows)
    assert all(item["momentum_filter"] == "kdj" for item in rows)


def test_overfit_flags_detect_validation_gap_and_unstable_walk_forward() -> None:
    flags = overfit_flags(
        full={"return_pct": 5000, "max_drawdown_pct": -30, "trades_taken": 250},
        train={"return_pct": 900},
        validation={"return_pct": 50},
        walk_forward={"negative_folds": 3, "worst_validation_drawdown_pct": -55},
        pure_full={"return_pct": 1000},
        coverage={"coverage_pct": 90},
        min_trades=180,
    )

    assert "train_validation_gap_large" in flags
    assert "too_many_negative_walk_forward_folds" in flags
    assert "walk_forward_drawdown_too_deep" in flags


def test_build_report_uses_actual_grid_params() -> None:
    report = build_report(
        {
            "params": {
                "start": "2022-01-01",
                "end": "2026-06-26",
                "source": "binance",
                "initial_equity": 10000,
                "leverage": 4,
                "fee_rate": 0.0006,
                "slippage_bps": 2,
            },
            "data_source": "binance",
            "searched_combos": 8,
            "grid_params": {
                "kc_lengths": [20, 22, 24, 26],
                "kc_scalars": [2.6, 2.8],
                "volume_multiples": [2.3, 2.5],
                "atr_stop_multiples": [1.2, 1.5],
            },
            "top_candidates": [],
            "top_pf_sharpe_candidates": [
                {
                    "policy": "candidate",
                    "params": {
                        "kc_length": 20,
                        "kc_scalar": 2.8,
                        "volume_multiple": 2.5,
                        "atr_stop_multiple": 1.5,
                    },
                    "full": {
                        "return_pct": 100,
                        "max_drawdown_pct": -20,
                        "profit_factor": 1.8,
                        "trade_sharpe": 1.2,
                    },
                    "walk_forward": {"walk_forward_score": 0.5, "negative_folds": 0},
                    "beats_pure": True,
                    "overfit_flags": [],
                }
            ],
            "conclusions": ["ok"],
        }
    )

    assert "kc_length: 20 / 22 / 24 / 26" in report
    assert "atr_stop_multiple: 1.2 / 1.5" in report
    assert "盈利因子 + 夏普筛选榜" in report
    assert "PF `1.80`, Sharpe `1.20`" in report
