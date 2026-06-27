from __future__ import annotations

import pytest

from scripts.ai_tier_weight_research import (
    ResearchRow,
    balanced_policy,
    build_rows,
    calibrated_v1_controlled_research_policy,
    calibrated_v2_loss_aware_research_policy,
    current_factor_policy,
    evaluate_walk_forward,
    evaluate_policy,
    factor_channel_effects,
    factor_channel_scores,
    policy_transition_effects,
    parse_walk_forward_periods,
    rows_before,
    rows_between,
    weighted_policy,
)


def _row(**overrides) -> ResearchRow:
    raw = {
        "signal_idx": 1,
        "signal_time": "2024-02-01T00:00:00+00:00",
        "entry_time": "2024-02-01T01:00:00+00:00",
        "side": "long",
        "pnl": 10.0,
        "signal_strength": 0.86,
        "breakout_atr": 0.75,
        "volume_multiple": 3.5,
        "pattern_aligned_score": 0.82,
        "regime_range_score": 0.20,
        "regime_risk_score": 0.18,
        "regime_trend_score": 0.74,
        "dense_trend_score": 0.72,
        "dense_breakout_status": "breakout_up",
        "regime_breakout_quality": "strong",
        "htf_signal_alignment": "aligned",
        "htf_alignment_score": 0.70,
        "htf_trend_strength": 0.65,
    }
    scores = {
        "technical_signal_score": 0.86,
        "breakout_score": 0.62,
        "volume_score": 0.50,
        "orderflow_confirmation_score": 0.80,
        "orderflow_direction_score": 0.40,
        "news_direction_alignment_score": 0.0,
        "pattern_confirmation_score": 0.82,
        "range_safety_score": 0.80,
        "regime_risk_safety_score": 0.82,
        "trend_confirmation_score": 0.74,
        "dense_zone_breakout_score": 0.72,
        "news_safety_score": 0.5,
        "btc_leader_score": 0.5,
        "eth_btc_rotation_score": 0.5,
        "htf_alignment_score": 0.70,
    }
    raw.update(overrides.pop("raw", {}))
    scores.update(overrides.pop("scores", {}))
    return ResearchRow(
        signal_idx=overrides.pop("signal_idx", raw["signal_idx"]),
        signal_time=overrides.pop("signal_time", raw["signal_time"]),
        entry_time=overrides.pop("entry_time", raw["entry_time"]),
        side=overrides.pop("side", raw["side"]),
        pnl=overrides.pop("pnl", raw["pnl"]),
        baseline_equity_before=overrides.pop("baseline_equity_before", 1000.0),
        scores=scores,
        raw=raw,
    )


def test_balanced_policy_blocks_failed_breakout_before_score() -> None:
    row = _row(raw={"dense_breakout_status": "failed_breakout"}, scores={"orderflow_confirmation_score": 1.0})

    assert balanced_policy(row) == "block"


def test_current_factor_policy_does_not_use_missing_news_as_positive_confirmation() -> None:
    row = _row(scores={"news_direction_alignment_score": 0.0})

    tier = current_factor_policy(row)

    assert tier in {"normal", "strong", "full"}
    assert row.scores["news_direction_alignment_score"] == 0.0


def test_loss_aware_candidate_reduces_high_risk_promotion() -> None:
    row = _row(
        scores={
            "technical_signal_score": 0.95,
            "orderflow_confirmation_score": 0.42,
            "pattern_confirmation_score": 0.38,
            "dense_zone_breakout_score": 0.36,
            "range_safety_score": 0.35,
            "trend_confirmation_score": 0.78,
            "news_safety_score": 0.50,
        },
        raw={
            "regime_range_score": 0.65,
            "regime_risk_score": 0.45,
            "regime_trend_score": 0.78,
            "dense_trend_score": 0.36,
        },
    )

    legacy_scale = {"block": 0.0, "weak": 0.25, "normal": 0.50, "strong": 0.75, "full": 1.0}
    v1 = calibrated_v1_controlled_research_policy(row)
    v2 = calibrated_v2_loss_aware_research_policy(row)

    assert legacy_scale[v2] <= legacy_scale[v1]
    assert v2 in {"block", "weak", "normal"}


def test_weighted_policy_can_promote_high_orderflow_quality_without_directional_cvd() -> None:
    row = _row(scores={"orderflow_confirmation_score": 0.98, "orderflow_direction_score": 0.0})
    policy = weighted_policy(
        {
            "technical_signal_score": 0.20,
            "orderflow_confirmation_score": 0.28,
            "pattern_confirmation_score": 0.14,
            "trend_confirmation_score": 0.14,
            "dense_zone_breakout_score": 0.10,
            "range_safety_score": 0.10,
            "htf_alignment_score": 0.04,
        },
        {"full": 0.78, "strong": 0.68, "normal": 0.58, "weak": 0.50},
    )

    assert policy(row) in {"strong", "full"}


def test_evaluate_policy_uses_trade_sequence_without_future_outcome_for_tier() -> None:
    rows = [_row(signal_idx=1, pnl=100), _row(signal_idx=2, pnl=-50, scores={"orderflow_confirmation_score": 0.1})]

    result = evaluate_policy(rows, balanced_policy, 1000)

    assert result["trades_taken"] >= 1
    assert result["final_equity"] != pytest.approx(1000)


def test_evaluate_policy_reports_trade_level_sharpe() -> None:
    rows = [
        _row(signal_idx=1, entry_time="2024-01-01T01:00:00+00:00", pnl=20, baseline_equity_before=1000),
        _row(signal_idx=2, entry_time="2024-02-01T01:00:00+00:00", pnl=10, baseline_equity_before=1020),
        _row(signal_idx=3, entry_time="2024-03-01T01:00:00+00:00", pnl=-5, baseline_equity_before=1030),
        _row(signal_idx=4, entry_time="2024-04-01T01:00:00+00:00", pnl=15, baseline_equity_before=1025),
    ]

    result = evaluate_policy(rows, lambda row: "full", 1000)

    assert result["avg_trade_return_pct"] > 0
    assert result["trade_sharpe"] > 0


def test_policy_transition_effects_reports_loser_scaled_down() -> None:
    rows = [
        _row(signal_idx=1, pnl=100),
        _row(signal_idx=2, pnl=-50),
    ]

    result = policy_transition_effects(
        rows,
        lambda _row: "strong",
        lambda row: "normal" if row.pnl <= 0 else "full",
    )

    assert result["winning_trades_scaled_up"] == 1
    assert result["losing_trades_scaled_down"] == 1
    assert result["loser_pnl_delta_ratio_sum"] > 0


def test_evaluate_policy_stops_when_overlay_equity_is_ruined() -> None:
    rows = [_row(signal_idx=1, pnl=-2000, baseline_equity_before=1000)]

    result = evaluate_policy(rows, lambda row: "full", 1000)

    assert result["ruined"] is True
    assert result["final_equity"] == pytest.approx(0)
    assert result["max_drawdown_pct"] == pytest.approx(-100)


def test_evaluate_policy_uses_original_equity_before_trade_for_walk_forward_subsets() -> None:
    rows = [_row(signal_idx=10, pnl=-2000, baseline_equity_before=100_000)]

    result = evaluate_policy(rows, lambda row: "full", 1000)

    assert result["ruined"] is False
    assert result["final_equity"] == pytest.approx(980)


def test_build_rows_uses_train_orderflow_distribution_for_percentiles() -> None:
    features_payload = {
        "features": [
            {
                "signal_idx": 1,
                "signal_time": "2023-01-01T00:00:00+00:00",
                "entry_time": "2023-01-01T01:00:00+00:00",
                "side": "long",
                "pnl": 10,
                "signal_strength": 0.8,
                "breakout_atr": 0.7,
                "volume_multiple": 3.4,
                "pattern_aligned_score": 0.7,
                "regime_range_score": 0.2,
                "regime_risk_score": 0.2,
                "regime_trend_score": 0.7,
                "dense_trend_score": 0.7,
                "htf_alignment_score": 0.6,
            },
            {
                "signal_idx": 2,
                "signal_time": "2025-01-01T00:00:00+00:00",
                "entry_time": "2025-01-01T01:00:00+00:00",
                "side": "long",
                "pnl": -5,
                "signal_strength": 0.8,
                "breakout_atr": 0.7,
                "volume_multiple": 3.4,
                "pattern_aligned_score": 0.7,
                "regime_range_score": 0.2,
                "regime_risk_score": 0.2,
                "regime_trend_score": 0.7,
                "dense_trend_score": 0.7,
                "htf_alignment_score": 0.6,
            },
        ]
    }
    orderflow_payload = {
        "rows": {
            "60": [
                {"signal_idx": 1, "trade_count": 100, "total_quote": 100, "large_trade_quote": 10, "max_trade_quote": 5},
                {"signal_idx": 2, "trade_count": 200, "total_quote": 200, "large_trade_quote": 20, "max_trade_quote": 10},
            ]
        }
    }

    rows = build_rows(features_payload, orderflow_payload, "2024-01-01")

    assert rows[0].scores["orderflow_confirmation_score"] == pytest.approx(1.0)
    assert rows[1].scores["orderflow_confirmation_score"] == pytest.approx(1.0)


def test_parse_walk_forward_periods_rejects_invalid_order() -> None:
    with pytest.raises(ValueError):
        parse_walk_forward_periods("2024-02-01:2024-01-01")


def test_walk_forward_rows_apply_embargo() -> None:
    rows = [
        _row(signal_idx=1, signal_time="2023-12-20T00:00:00+00:00"),
        _row(signal_idx=2, signal_time="2023-12-30T00:00:00+00:00"),
        _row(signal_idx=3, signal_time="2024-01-02T00:00:00+00:00"),
    ]

    assert [row.signal_idx for row in rows_before(rows, "2024-01-01", embargo_days=7)] == [1]
    assert [row.signal_idx for row in rows_between(rows, "2024-01-01", "2024-02-01")] == [3]


def test_evaluate_walk_forward_scores_validation_window_without_future_training_rows() -> None:
    rows = [
        _row(
            signal_idx=index,
            signal_time=f"2023-01-{(index % 28) + 1:02d}T00:00:00+00:00",
            pnl=10 if index % 3 else -4,
        )
        for index in range(1, 41)
    ]
    rows.extend(
        _row(
            signal_idx=100 + index,
            signal_time=f"2023-07-{index + 1:02d}T00:00:00+00:00",
            pnl=10 if index % 3 else -4,
        )
        for index in range(6)
    )

    result = evaluate_walk_forward(
        rows,
        balanced_policy,
        periods=[("2023-07-01", "2023-08-01")],
        initial_equity=1000,
        embargo_days=0,
    )

    assert result["status"] == "ok"
    assert result["ok_folds"] == 1
    assert result["ruined_folds"] == 0
    assert result["total_validation_trades"] > 0
    assert result["min_validation_objective"] > -9000
    assert "walk_forward_score" in result


def test_factor_channel_scores_split_profit_and_loss_roles_without_outcome_leakage() -> None:
    clean_trend = _row(
        pnl=-9999,
        scores={
            "orderflow_confirmation_score": 0.92,
            "pattern_confirmation_score": 0.86,
            "dense_zone_breakout_score": 0.80,
            "trend_confirmation_score": 0.78,
            "range_safety_score": 0.84,
        },
        raw={"regime_range_score": 0.16, "regime_risk_score": 0.12},
    )
    noisy_breakout = _row(
        pnl=9999,
        scores={
            "orderflow_confirmation_score": 0.25,
            "pattern_confirmation_score": 0.24,
            "dense_zone_breakout_score": 0.30,
            "trend_confirmation_score": 0.42,
            "range_safety_score": 0.25,
            "breakout_score": 0.95,
        },
        raw={"regime_range_score": 0.75, "regime_risk_score": 0.62},
    )

    clean_scores = factor_channel_scores(clean_trend)
    noisy_scores = factor_channel_scores(noisy_breakout)

    assert clean_scores["profit_expansion"] > noisy_scores["profit_expansion"]
    assert clean_scores["execution_quality"] > noisy_scores["execution_quality"]
    assert noisy_scores["loss_suppression_risk"] > clean_scores["loss_suppression_risk"]


def test_factor_channel_effects_reports_desired_direction() -> None:
    rows = [
        _row(signal_idx=1, pnl=120, scores={"orderflow_confirmation_score": 0.9, "pattern_confirmation_score": 0.86, "range_safety_score": 0.82}),
        _row(signal_idx=2, pnl=80, scores={"orderflow_confirmation_score": 0.82, "pattern_confirmation_score": 0.78, "range_safety_score": 0.76}),
        _row(
            signal_idx=3,
            pnl=-50,
            scores={"orderflow_confirmation_score": 0.24, "pattern_confirmation_score": 0.22, "range_safety_score": 0.20},
            raw={"regime_range_score": 0.80, "regime_risk_score": 0.70},
        ),
        _row(
            signal_idx=4,
            pnl=-20,
            scores={"orderflow_confirmation_score": 0.35, "pattern_confirmation_score": 0.28, "range_safety_score": 0.32},
            raw={"regime_range_score": 0.68, "regime_risk_score": 0.58},
        ),
    ]

    effects = factor_channel_effects(rows)

    assert effects["profit_expansion"]["desired_direction"] == "winner_higher_is_good"
    assert effects["loss_suppression_risk"]["desired_direction"] == "loser_higher_is_good"
    assert effects["profit_expansion"]["desired_effect_size"] > 0
    assert effects["loss_suppression_risk"]["desired_effect_size"] > 0
