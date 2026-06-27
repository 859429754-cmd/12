from __future__ import annotations

import pytest

from scripts.pure_strategy_tier_research import (
    TradeFeature,
    apply_trend_overrides,
    balanced_policy,
    evaluate_policy,
    summarize_factor_groups,
    summarize_numeric_features,
)

from ai_quant_trader.core.models import TrendStrategyConfig


def _feature(**overrides):
    base = dict(
        signal_idx=1,
        signal_time="2024-01-01T00:00:00Z",
        entry_time="2024-01-01T01:00:00Z",
        exit_time="2024-01-01T08:00:00Z",
        side="long",
        exit_reason="kc_mid_exit",
        pnl=10.0,
        return_pct=0.01,
        mae_pct=-0.2,
        mfe_pct=2.0,
        signal_strength=0.82,
        breakout_atr=0.8,
        volume_multiple=3.4,
        atr_pct=0.02,
        pattern_type="rectangle_breakout",
        pattern_family="breakout",
        pattern_confidence=0.75,
        pattern_aligned_score=0.82,
        dense_position="above_value",
        dense_breakout_status="breakout_up",
        dense_trend_score=0.75,
        dense_range_score=0.25,
        dense_strength=0.7,
        regime_candidate="trend",
        regime_strategy_allowed="trend",
        regime_breakout_quality="strong",
        regime_trend_score=0.78,
        regime_range_score=0.24,
        regime_risk_score=0.18,
        htf_signal_alignment="aligned",
        htf_alignment_score=0.72,
        htf_trend_strength=0.70,
        entry_quality_score=0.80,
        confirmations=5,
    )
    base.update(overrides)
    return TradeFeature(**base)


def test_balanced_policy_promotes_high_quality_entries_without_post_entry_addon() -> None:
    assert balanced_policy(_feature(entry_quality_score=0.85, confirmations=5)) == "full"
    assert balanced_policy(_feature(entry_quality_score=0.74, confirmations=3)) == "strong"
    assert balanced_policy(_feature(entry_quality_score=0.63, confirmations=2)) == "normal"


def test_balanced_policy_blocks_failed_breakout_even_when_score_is_high() -> None:
    item = _feature(entry_quality_score=0.90, confirmations=6, dense_breakout_status="failed_breakout")

    assert balanced_policy(item) == "block"


def test_numeric_overlap_compares_winning_and_losing_entry_features() -> None:
    stats = summarize_numeric_features(
        [
            _feature(pnl=12, signal_strength=0.9, regime_trend_score=0.8),
            _feature(pnl=-4, signal_strength=0.5, regime_trend_score=0.3),
        ]
    )

    assert stats["signal_strength"]["winner_median"] == pytest.approx(0.9)
    assert stats["signal_strength"]["loser_median"] == pytest.approx(0.5)
    assert stats["regime_trend_score"]["effect_size"] > 0


def test_policy_evaluation_uses_shadow_sizing_not_future_outcome() -> None:
    items = [
        _feature(signal_idx=1, pnl=20, entry_quality_score=0.85, confirmations=5),
        _feature(signal_idx=2, pnl=-10, entry_quality_score=0.40, confirmations=1),
    ]

    result = evaluate_policy(items, balanced_policy, 1000)

    assert result["tier_counts"]["full"] == 1
    assert result["tier_counts"]["block"] == 1
    assert result["total_pnl"] == pytest.approx(20.0)


def test_factor_groups_mark_live_news_orderflow_as_not_backtestable_without_archive() -> None:
    numeric = summarize_numeric_features(
        [
            _feature(pnl=12, signal_strength=0.9, regime_trend_score=0.8),
            _feature(pnl=-4, signal_strength=0.5, regime_trend_score=0.3),
        ]
    )

    groups = summarize_factor_groups(numeric, {})

    assert groups["core_strategy_trigger"]["top_numeric"]
    assert groups["live_news_orderflow"]["verdict"] == "live_only_needs_archival_backtest"
    assert "news_risk_score" in groups["live_news_orderflow"]["live_only"]


def test_apply_trend_overrides_keeps_default_config_when_args_are_empty() -> None:
    config = TrendStrategyConfig(volume_multiple=2.5, kc_scalar=2.8, atr_stop_multiple=1.5)
    args = type(
        "Args",
        (),
        {
            "kc_scalar": None,
            "volume_multiple": None,
            "atr_stop_multiple": None,
            "position_fraction": None,
        },
    )()

    assert apply_trend_overrides(config, args) is config


def test_apply_trend_overrides_can_replay_legacy_volume_threshold() -> None:
    config = TrendStrategyConfig(volume_multiple=2.5, kc_scalar=2.8, atr_stop_multiple=1.5)
    args = type(
        "Args",
        (),
        {
            "kc_scalar": 2.8,
            "volume_multiple": 2.0,
            "atr_stop_multiple": 1.5,
            "position_fraction": 1.0,
        },
    )()

    updated = apply_trend_overrides(config, args)

    assert updated is not config
    assert updated.volume_multiple == pytest.approx(2.0)
    assert updated.position_fraction == pytest.approx(1.0)
    assert config.volume_multiple == pytest.approx(2.5)
