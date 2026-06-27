from __future__ import annotations

from scripts.factor_combination_research import (
    eligible_factor_specs,
    generate_combinations,
    normalized_factor_score,
    overfit_flags,
)


def _feature(**updates):
    base = {
        "signal_idx": 1,
        "signal_time": "2024-01-01T00:00:00+00:00",
        "pnl": 10.0,
        "signal_strength": 0.85,
        "breakout_atr": 0.6,
        "volume_multiple": 3.2,
        "atr_pct": 0.01,
        "pattern_aligned_score": 0.8,
        "dense_trend_score": 0.7,
        "dense_range_score": 0.2,
        "regime_trend_score": 0.72,
        "regime_range_score": 0.18,
        "regime_risk_score": 0.12,
        "htf_alignment_score": 0.65,
        "htf_trend_strength": 0.6,
        "mae_pct": -3.0,
        "mfe_pct": 5.0,
    }
    base.update(updates)
    return base


def test_eligible_factor_specs_exclude_outcome_and_live_only_factors() -> None:
    features = [_feature(signal_idx=index) for index in range(1, 40)]

    factors = eligible_factor_specs(features)
    names = {factor.name for factor in factors}

    assert "volume_multiple" in names
    assert "regime_range_score" in names
    assert "mae_pct" not in names
    assert "mfe_pct" not in names
    assert "news_direction_alignment" not in names
    assert all(factor.availability.value == "backtestable_now" for factor in factors)


def test_loss_suppression_factor_score_is_higher_when_risk_is_lower() -> None:
    features = [_feature(signal_idx=index) for index in range(1, 40)]
    factors = {factor.name: factor for factor in eligible_factor_specs(features)}

    calm = _feature(regime_range_score=0.1)
    choppy = _feature(regime_range_score=0.9)

    assert normalized_factor_score(factors["regime_range_score"], calm) > normalized_factor_score(
        factors["regime_range_score"], choppy
    )


def test_generate_combinations_requires_profit_or_execution_and_risk_side() -> None:
    features = [_feature(signal_idx=index) for index in range(1, 40)]
    factors = eligible_factor_specs(features)

    combos = generate_combinations(factors, min_size=2, max_size=3, max_candidates=200)

    assert combos
    assert all(len(combo.factor_names) <= 3 for combo in combos)
    assert all("mae_pct" not in combo.factor_names for combo in combos)


def test_overfit_flags_reject_deep_drawdown_and_many_negative_years() -> None:
    flags = overfit_flags(
        {"return_pct": 200.0, "max_drawdown_pct": -65.0, "profit_factor": 1.0},
        {
            "walk_forward_score": -1500.0,
            "negative_years": 3,
            "worst_year_drawdown_pct": -55.0,
            "median_year_return_pct": 5.0,
        },
        generate_combinations(
            eligible_factor_specs([_feature(signal_idx=index) for index in range(1, 40)]),
            min_size=2,
            max_size=2,
            max_candidates=1,
        )[0],
    )

    assert "yearly_objective_broken" in flags
    assert "too_many_negative_years" in flags
    assert "year_drawdown_too_deep" in flags
    assert "full_drawdown_too_deep" in flags
