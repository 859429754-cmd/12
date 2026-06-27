from __future__ import annotations

from ai_quant_trader.core.models import TrendStrategyConfig
from scripts.pure_strategy_tier_research import TradeFeature
from scripts.strategy_parameter_tier_grid_research import build_orderflow_scores, build_report, overfit_flags, strategy_grid


def _trade_feature(signal_idx: int, signal_time: str) -> TradeFeature:
    return TradeFeature(
        signal_idx=signal_idx,
        signal_time=signal_time,
        entry_time=signal_time.replace("00:00:00", "01:00:00"),
        exit_time=signal_time.replace("00:00:00", "02:00:00"),
        side="long",
        exit_reason="test",
        pnl=10.0,
        return_pct=1.0,
        mae_pct=-0.2,
        mfe_pct=1.5,
        signal_strength=0.8,
        breakout_atr=0.7,
        volume_multiple=3.4,
        atr_pct=0.01,
        pattern_type="rectangle",
        pattern_family="range",
        pattern_confidence=0.7,
        pattern_aligned_score=0.7,
        dense_position="above_value",
        dense_breakout_status="breakout_up",
        dense_trend_score=0.7,
        dense_range_score=0.2,
        dense_strength=0.6,
        regime_candidate="trend",
        regime_strategy_allowed="trend",
        regime_breakout_quality="strong",
        regime_trend_score=0.7,
        regime_range_score=0.2,
        regime_risk_score=0.2,
        htf_signal_alignment="aligned",
        htf_alignment_score=0.6,
        htf_trend_strength=0.7,
        entry_quality_score=0.75,
        confirmations=5,
    )


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


def test_grid_research_treats_missing_orderflow_as_neutral_not_low_quality() -> None:
    features = [
        _trade_feature(1, "2023-01-01T00:00:00+00:00"),
        _trade_feature(2, "2025-01-01T00:00:00+00:00"),
    ]
    payload = {
        "rows": {
            "60": [
                {
                    "signal_idx": 1,
                    "trade_count": 0,
                    "total_quote": 0,
                    "large_trade_quote": 0,
                    "max_trade_quote": 0,
                    "missing_days": ["2023-01-01"],
                },
                {
                    "signal_idx": 2,
                    "trade_count": 100,
                    "total_quote": 100,
                    "large_trade_quote": 10,
                    "max_trade_quote": 5,
                    "missing_days": [],
                },
            ]
        }
    }

    scores, coverage = build_orderflow_scores(features, payload, "2024-01-01")

    assert scores[1]["orderflow_confirmation_score"] == 0.5
    assert scores[1]["orderflow_missing"] == 1.0
    assert scores[2]["orderflow_confirmation_score"] == 0.5
    assert coverage["covered"] == 1


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
