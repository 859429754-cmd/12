from __future__ import annotations

from ai_quant_trader.research.factors import (
    FACTOR_LIBRARY,
    FactorAvailability,
    FactorCategory,
    FactorRole,
    factors_by_availability,
    factors_by_category,
    factors_by_role,
    forbidden_outcome_factors,
    live_archive_required_factors,
    optimization_eligible_factors,
)


def test_factor_library_covers_major_professional_factor_families() -> None:
    categories = set(factors_by_category())

    assert len(FACTOR_LIBRARY) >= 30
    assert FactorCategory.STRATEGY_TRIGGER in categories
    assert FactorCategory.ORDERFLOW in categories
    assert FactorCategory.DERIVATIVES in categories
    assert FactorCategory.NEWS_MACRO in categories
    assert FactorCategory.CROSS_ASSET in categories
    assert FactorCategory.ONCHAIN in categories
    assert FactorCategory.EXECUTION in categories


def test_factor_roles_separate_profit_expansion_and_loss_suppression() -> None:
    roles = factors_by_role()

    assert len(roles[FactorRole.PROFIT_EXPANSION]) >= 5
    assert len(roles[FactorRole.LOSS_SUPPRESSION]) >= 5
    assert len(roles[FactorRole.EXECUTION_QUALITY]) >= 4
    assert len(roles[FactorRole.HARD_RISK_GATE]) >= 3


def test_only_lookahead_safe_backtestable_factors_are_optimization_eligible() -> None:
    eligible = optimization_eligible_factors()
    forbidden = forbidden_outcome_factors()
    eligible_names = {factor.name for factor in eligible}

    assert eligible
    assert all(factor.lookahead_safe for factor in eligible)
    assert all(
        factor.availability
        in {
            FactorAvailability.BACKTESTABLE_NOW,
            FactorAvailability.BACKTESTABLE_WITH_BACKFILL,
        }
        for factor in eligible
    )
    assert {"mae_pct", "mfe_pct", "realized_pnl"}.issubset({factor.name for factor in forbidden})
    assert not eligible_names.intersection({factor.name for factor in forbidden})


def test_live_only_factors_require_archive_before_historical_optimization() -> None:
    live_required = live_archive_required_factors()
    availability = factors_by_availability()
    live_names = {factor.name for factor in live_required}

    assert FactorAvailability.LIVE_ONLY_NEEDS_ARCHIVE in availability
    assert "news_direction_alignment" in live_names
    assert "btc_leader_alignment" in live_names
    assert "realtime_orderbook_imbalance" in live_names
    assert all(not factor.eligible_for_optimization for factor in live_required)
