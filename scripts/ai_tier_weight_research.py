from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_quant_trader.risk.sizing import (
    calibrated_v1_policy,
    calibrated_v2_loss_aware_policy,
    calibrated_v21_profit_loss_policy,
    factor_ranked_policy,
    limit_tier_lift,
)


INPUT_FEATURES = Path("data/research/pure_strategy_tier_research_eth_2022_2026_no_ema.json")
INPUT_ORDERFLOW = Path("data/research/historical_orderflow_proxy_eth_2022_2026.json")
DEFAULT_OUTPUT = Path("data/research/ai_tier_weight_research_eth_2022_2026.json")
DEFAULT_WALK_FORWARD_PERIODS = [
    ("2023-01-01", "2023-07-01"),
    ("2023-07-01", "2024-01-01"),
    ("2024-01-01", "2024-07-01"),
    ("2024-07-01", "2025-01-01"),
    ("2025-01-01", "2025-07-01"),
    ("2025-07-01", "2026-01-01"),
    ("2026-01-01", "2026-06-27"),
]

TIER_SCALE = {
    "block": 0.0,
    "weak": 0.25,
    "normal": 0.50,
    "strong": 0.75,
    "full": 1.0,
}
TIER_ORDER = ["block", "weak", "normal", "strong", "full"]

CURRENT_FACTOR_WEIGHTS = {
    "technical_signal_score": 0.18,
    "orderflow_confirmation_score": 0.20,
    "news_direction_alignment_score": 0.14,
    "pattern_confirmation_score": 0.12,
    "range_safety_score": 0.11,
    "trend_confirmation_score": 0.10,
    "dense_zone_breakout_score": 0.08,
    "news_safety_score": 0.04,
    "btc_leader_score": 0.02,
    "eth_btc_rotation_score": 0.01,
}
HISTORICALLY_TESTABLE_CURRENT_WEIGHTS = {
    key: value
    for key, value in CURRENT_FACTOR_WEIGHTS.items()
    if key
    not in {
        "news_direction_alignment_score",
        "news_safety_score",
        "btc_leader_score",
        "eth_btc_rotation_score",
    }
}


@dataclass(frozen=True)
class ResearchRow:
    signal_idx: int
    signal_time: str
    entry_time: str
    side: str
    pnl: float
    baseline_equity_before: float
    scores: dict[str, float]
    raw: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Research live AI five-tier sizing weights against historical ETH signals.")
    parser.add_argument("--features", default=str(INPUT_FEATURES))
    parser.add_argument("--orderflow", default=str(INPUT_ORDERFLOW))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--split", default="2024-01-01")
    parser.add_argument(
        "--walk-forward-periods",
        default=",".join(f"{start}:{end}" for start, end in DEFAULT_WALK_FORWARD_PERIODS),
        help="Comma-separated validation windows as start:end dates. Train set is all prior rows with embargo.",
    )
    parser.add_argument("--embargo-days", type=int, default=7)
    parser.add_argument("--initial-equity", type=float, default=10_000.0)
    parser.add_argument("--top", type=int, default=12)
    return parser.parse_args()


def clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    if not math.isfinite(float(value)):
        return low
    return max(low, min(high, float(value)))


def tier_index(tier: str) -> int:
    return TIER_ORDER.index(tier)


def min_tier(tier: str, cap: str) -> str:
    return TIER_ORDER[min(tier_index(tier), tier_index(cap))]


def score_to_tier(score: float, thresholds: dict[str, float] | None = None) -> str:
    thresholds = thresholds or {"full": 0.81, "strong": 0.70, "normal": 0.62, "weak": 0.55}
    if score >= thresholds["full"]:
        return "full"
    if score >= thresholds["strong"]:
        return "strong"
    if score >= thresholds["normal"]:
        return "normal"
    if score >= thresholds["weak"]:
        return "weak"
    return "block"


def empirical_percentile(train_values: list[float], value: float) -> float:
    values = sorted(v for v in train_values if math.isfinite(float(v)))
    if not values:
        return 0.5
    count = sum(1 for item in values if item <= value)
    return clip(count / len(values))


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_orderflow_map(orderflow_payload: dict[str, Any], window: str = "60") -> dict[int, dict[str, Any]]:
    rows = orderflow_payload.get("rows", {}).get(window, [])
    return {int(row["signal_idx"]): row for row in rows if "signal_idx" in row}


def usable_orderflow_row(row: dict[str, Any] | None) -> bool:
    return bool(row) and not row.get("missing_days") and float(row.get("trade_count") or 0.0) > 0.0


def is_train(signal_time: str, split: str) -> bool:
    return signal_time[:10] < split


def parse_day(value: str) -> date:
    return date.fromisoformat(value[:10])


def parse_walk_forward_periods(value: str) -> list[tuple[str, str]]:
    periods: list[tuple[str, str]] = []
    for item in [part.strip() for part in value.split(",") if part.strip()]:
        if ":" not in item:
            raise ValueError(f"invalid_walk_forward_period:{item}")
        start, end = [part.strip() for part in item.split(":", 1)]
        if parse_day(start) >= parse_day(end):
            raise ValueError(f"invalid_walk_forward_period_order:{item}")
        periods.append((start, end))
    if not periods:
        raise ValueError("walk_forward_periods_required")
    return periods


def rows_before(rows: list[ResearchRow], cutoff: str, *, embargo_days: int) -> list[ResearchRow]:
    embargo_cutoff = parse_day(cutoff) - timedelta(days=max(embargo_days, 0))
    return [row for row in rows if parse_day(row.signal_time) < embargo_cutoff]


def rows_between(rows: list[ResearchRow], start: str, end: str) -> list[ResearchRow]:
    start_day = parse_day(start)
    end_day = parse_day(end)
    return [row for row in rows if start_day <= parse_day(row.signal_time) < end_day]


def orderflow_quality_scores(
    features: list[dict[str, Any]],
    orderflow_by_idx: dict[int, dict[str, Any]],
    split: str,
) -> dict[int, dict[str, float]]:
    fields = ["trade_count", "total_quote", "large_trade_quote", "max_trade_quote"]
    train_rows = [
        row
        for item in features
        if is_train(str(item["signal_time"]), split)
        for row in [orderflow_by_idx.get(int(item["signal_idx"]))]
        if usable_orderflow_row(row)
    ]
    train_values = {field: [float(row.get(field) or 0.0) for row in train_rows] for field in fields}
    output: dict[int, dict[str, float]] = {}
    for item in features:
        signal_idx = int(item["signal_idx"])
        row = orderflow_by_idx.get(signal_idx)
        if not usable_orderflow_row(row):
            output[signal_idx] = {
                "orderflow_confirmation_score": 0.5,
                "orderflow_direction_score": 0.0,
            }
            continue
        percentiles = [empirical_percentile(train_values[field], float(row.get(field) or 0.0)) for field in fields]
        directional_cvd = clip(float(row.get("directional_cvd_quote_ratio") or 0.0) / 0.35)
        directional_large = clip(float(row.get("directional_large_trade_ratio") or 0.0) / 0.35)
        output[signal_idx] = {
            "orderflow_confirmation_score": sum(percentiles) / len(percentiles) if percentiles else 0.5,
            "orderflow_direction_score": (directional_cvd + directional_large) / 2,
        }
    return output


def build_rows(features_payload: dict[str, Any], orderflow_payload: dict[str, Any], split: str) -> list[ResearchRow]:
    features = sorted(list(features_payload.get("features", [])), key=lambda item: int(item["signal_idx"]))
    orderflow_scores = orderflow_quality_scores(features, build_orderflow_map(orderflow_payload), split)
    rows: list[ResearchRow] = []
    baseline_equity = float(features_payload.get("params", {}).get("initial_equity") or 10_000.0)
    for item in features:
        signal_idx = int(item["signal_idx"])
        pnl = float(item["pnl"])
        baseline_equity_before = baseline_equity
        of_scores = orderflow_scores.get(signal_idx, {})
        scores = {
            "technical_signal_score": clip(float(item.get("signal_strength") or 0.0)),
            "breakout_score": clip(float(item.get("breakout_atr") or 0.0) / 1.2),
            "volume_score": clip((float(item.get("volume_multiple") or 0.0) - 2.5) / 2.0),
            "orderflow_confirmation_score": clip(of_scores.get("orderflow_confirmation_score", 0.5)),
            "orderflow_direction_score": clip(of_scores.get("orderflow_direction_score", 0.0)),
            "news_direction_alignment_score": 0.0,
            "pattern_confirmation_score": clip(float(item.get("pattern_aligned_score") or 0.0)),
            "range_safety_score": 1.0 - clip(float(item.get("regime_range_score") or 0.0)),
            "regime_risk_safety_score": 1.0 - clip(float(item.get("regime_risk_score") or 0.0)),
            "trend_confirmation_score": clip(float(item.get("regime_trend_score") or 0.0)),
            "dense_zone_breakout_score": clip(float(item.get("dense_trend_score") or 0.0)),
            "news_safety_score": 0.5,
            "btc_leader_score": 0.5,
            "eth_btc_rotation_score": 0.5,
            "htf_alignment_score": clip(float(item.get("htf_alignment_score") or 0.0)),
        }
        rows.append(
            ResearchRow(
                signal_idx=signal_idx,
                signal_time=str(item["signal_time"]),
                entry_time=str(item["entry_time"]),
                side=str(item["side"]),
                pnl=pnl,
                baseline_equity_before=baseline_equity_before,
                scores=scores,
                raw=item,
            )
        )
        baseline_equity += pnl
    return rows


def structural_hard_block(row: ResearchRow) -> str | None:
    raw = row.raw
    if str(raw.get("dense_breakout_status") or "") == "failed_breakout":
        return "failed_breakout"
    if float(raw.get("regime_risk_score") or 0.0) >= 0.80:
        return "regime_risk_extreme"
    if (
        str(raw.get("htf_signal_alignment") or "") == "conflict"
        and float(raw.get("htf_trend_strength") or 0.0) >= 0.75
        and float(raw.get("regime_risk_score") or 0.0) >= 0.55
    ):
        return "htf_conflict"
    return None


def apply_research_caps(row: ResearchRow, tier: str) -> str:
    raw = row.raw
    if structural_hard_block(row):
        return "block"
    if float(raw.get("regime_range_score") or 0.0) >= 0.70 and float(raw.get("regime_trend_score") or 0.0) < 0.60:
        tier = min_tier(tier, "weak")
    if row.scores["orderflow_confirmation_score"] < 0.35:
        tier = min_tier(tier, "weak")
    if row.scores["dense_zone_breakout_score"] < 0.25:
        tier = min_tier(tier, "normal")
    if row.scores["pattern_confirmation_score"] < 0.25:
        tier = min_tier(tier, "weak")
    elif row.scores["pattern_confirmation_score"] < 0.40:
        tier = min_tier(tier, "normal")
    return tier


def balanced_entry_score(row: ResearchRow) -> float:
    return clip(
        row.scores["technical_signal_score"] * 0.18
        + row.scores["breakout_score"] * 0.14
        + row.scores["volume_score"] * 0.12
        + row.scores["trend_confirmation_score"] * 0.18
        + row.scores["dense_zone_breakout_score"] * 0.10
        + row.scores["pattern_confirmation_score"] * 0.12
        + row.scores["htf_alignment_score"] * 0.11
        + row.scores["range_safety_score"] * 0.08
        + row.scores["regime_risk_safety_score"] * 0.07
    )


def confirmation_count(row: ResearchRow) -> int:
    raw = row.raw
    checks = [
        row.scores["technical_signal_score"] >= 0.75,
        float(raw.get("breakout_atr") or 0.0) >= 0.55,
        float(raw.get("volume_multiple") or 0.0) >= 3.0,
        row.scores["pattern_confirmation_score"] >= 0.65,
        row.scores["dense_zone_breakout_score"] >= 0.58
        or str(raw.get("dense_breakout_status") or "") in {"breakout_up", "breakout_down", "vacuum_travel"},
        row.scores["trend_confirmation_score"] >= 0.62 and str(raw.get("regime_breakout_quality") or "") in {"strong", "pending"},
        row.scores["htf_alignment_score"] >= 0.62,
    ]
    return sum(1 for item in checks if item)


def balanced_policy(row: ResearchRow) -> str:
    if structural_hard_block(row):
        return "block"
    score = balanced_entry_score(row)
    confirmations = confirmation_count(row)
    if str(row.raw.get("regime_strategy_allowed") or "trend") not in {"trend", "none"} and score < 0.68:
        return "weak"
    if float(row.raw.get("regime_range_score") or 0.0) >= 0.70 and row.scores["trend_confirmation_score"] < 0.60:
        return "weak"
    if score >= 0.82 and confirmations >= 4 and float(row.raw.get("regime_risk_score") or 0.0) < 0.45:
        return "full"
    if score >= 0.72 and confirmations >= 3:
        return "strong"
    if score >= 0.62 and confirmations >= 2:
        return "normal"
    if score >= 0.52:
        return "weak"
    return "block"


def non_volume_confirmation_count(row: ResearchRow) -> int:
    raw = row.raw
    checks = [
        row.scores["technical_signal_score"] >= 0.75,
        float(raw.get("breakout_atr") or 0.0) >= 0.55,
        row.scores["pattern_confirmation_score"] >= 0.65,
        row.scores["dense_zone_breakout_score"] >= 0.58
        or str(raw.get("dense_breakout_status") or "") in {"breakout_up", "breakout_down", "vacuum_travel"},
        row.scores["trend_confirmation_score"] >= 0.62 and str(raw.get("regime_breakout_quality") or "") in {"strong", "pending"},
        row.scores["htf_alignment_score"] >= 0.62,
    ]
    return sum(1 for item in checks if item)


def balanced_volume_dedup_score(row: ResearchRow) -> float:
    return clip(
        row.scores["technical_signal_score"] * 0.14
        + row.scores["breakout_score"] * 0.17
        + row.scores["trend_confirmation_score"] * 0.20
        + row.scores["dense_zone_breakout_score"] * 0.13
        + row.scores["pattern_confirmation_score"] * 0.14
        + row.scores["htf_alignment_score"] * 0.12
        + row.scores["range_safety_score"] * 0.06
        + row.scores["regime_risk_safety_score"] * 0.04
    )


def balanced_volume_dedup_policy(row: ResearchRow) -> str:
    if structural_hard_block(row):
        return "block"
    score = balanced_volume_dedup_score(row)
    confirmations = non_volume_confirmation_count(row)
    if str(row.raw.get("regime_strategy_allowed") or "trend") not in {"trend", "none"} and score < 0.68:
        return "weak"
    if float(row.raw.get("regime_range_score") or 0.0) >= 0.70 and row.scores["trend_confirmation_score"] < 0.60:
        return "weak"
    if score >= 0.82 and confirmations >= 4 and float(row.raw.get("regime_risk_score") or 0.0) < 0.45:
        return "full"
    if score >= 0.72 and confirmations >= 3:
        return "strong"
    if score >= 0.62 and confirmations >= 2:
        return "normal"
    if score >= 0.52:
        return "weak"
    return "block"


def structure_context_score(row: ResearchRow) -> float:
    return clip(
        row.scores["breakout_score"] * 0.17
        + row.scores["trend_confirmation_score"] * 0.22
        + row.scores["dense_zone_breakout_score"] * 0.15
        + row.scores["pattern_confirmation_score"] * 0.16
        + row.scores["htf_alignment_score"] * 0.14
        + row.scores["range_safety_score"] * 0.10
        + row.scores["regime_risk_safety_score"] * 0.06
    )


def structure_context_policy(row: ResearchRow) -> str:
    if structural_hard_block(row):
        return "block"
    score = structure_context_score(row)
    confirmations = non_volume_confirmation_count(row)
    if str(row.raw.get("regime_strategy_allowed") or "trend") not in {"trend", "none"} and score < 0.66:
        return "weak"
    if float(row.raw.get("regime_range_score") or 0.0) >= 0.70 and row.scores["trend_confirmation_score"] < 0.60:
        return "weak"
    if score >= 0.80 and confirmations >= 4 and float(row.raw.get("regime_risk_score") or 0.0) < 0.45:
        return "full"
    if score >= 0.70 and confirmations >= 3:
        return "strong"
    if score >= 0.60 and confirmations >= 2:
        return "normal"
    if score >= 0.50:
        return "weak"
    return "block"


def current_factor_policy(row: ResearchRow) -> str:
    score = sum(row.scores[key] * weight for key, weight in CURRENT_FACTOR_WEIGHTS.items())
    return apply_research_caps(row, score_to_tier(score))


def current_factor_testable_renormalized_policy(row: ResearchRow) -> str:
    total = sum(HISTORICALLY_TESTABLE_CURRENT_WEIGHTS.values())
    score = sum(row.scores[key] * weight for key, weight in HISTORICALLY_TESTABLE_CURRENT_WEIGHTS.items()) / max(total, 1e-9)
    return apply_research_caps(row, score_to_tier(score))


def live_sizing_score_inputs(row: ResearchRow) -> dict[str, float]:
    return {
        "technical_signal_score": row.scores.get("technical_signal_score", 0.0),
        "orderflow_confirmation_score": row.scores.get("orderflow_confirmation_score", 0.0),
        "news_direction_alignment_score": row.scores.get("news_direction_alignment_score", 0.0),
        "pattern_confirmation_score": row.scores.get("pattern_confirmation_score", 0.0),
        "dense_zone_breakout_score": row.scores.get("dense_zone_breakout_score", 0.0),
        "range_safety_score": row.scores.get("range_safety_score", 0.0),
        "trend_confirmation_score": row.scores.get("trend_confirmation_score", 0.0),
        "news_safety_score": row.scores.get("news_safety_score", 0.5),
        "btc_leader_score": row.scores.get("btc_leader_score", 0.5),
        "eth_btc_rotation_score": row.scores.get("eth_btc_rotation_score", 0.5),
    }


def calibrated_v1_controlled_research_policy(row: ResearchRow) -> str:
    score_inputs = live_sizing_score_inputs(row)
    legacy = factor_ranked_policy(score_inputs, min_trade_score=0.55)
    calibrated = calibrated_v1_policy(score_inputs, min_trade_score=0.55, min_factor_coverage=0.70)
    if "calibrated_factor_coverage_low_fallback_legacy" in calibrated.warnings:
        return apply_research_caps(row, legacy.tier)
    return apply_research_caps(row, limit_tier_lift(calibrated.tier, legacy.tier, 1))


def calibrated_v2_loss_aware_research_policy(row: ResearchRow) -> str:
    score_inputs = live_sizing_score_inputs(row)
    legacy = factor_ranked_policy(score_inputs, min_trade_score=0.55)
    calibrated = calibrated_v2_loss_aware_policy(score_inputs, min_trade_score=0.55, min_factor_coverage=0.70)
    if "calibrated_factor_coverage_low_fallback_legacy" in calibrated.warnings:
        return apply_research_caps(row, legacy.tier)
    return apply_research_caps(row, limit_tier_lift(calibrated.tier, legacy.tier, 1))


def calibrated_v21_profit_loss_research_policy(row: ResearchRow) -> str:
    score_inputs = live_sizing_score_inputs(row)
    legacy = factor_ranked_policy(score_inputs, min_trade_score=0.55)
    calibrated = calibrated_v21_profit_loss_policy(score_inputs, min_trade_score=0.55, min_factor_coverage=0.70)
    if "calibrated_factor_coverage_low_fallback_legacy" in calibrated.warnings:
        return apply_research_caps(row, legacy.tier)
    return apply_research_caps(row, limit_tier_lift(calibrated.tier, legacy.tier, 1))


def weighted_policy(weights: dict[str, float], thresholds: dict[str, float]) -> Callable[[ResearchRow], str]:
    active_total = sum(weights.values())

    def policy(row: ResearchRow) -> str:
        score = sum(row.scores[key] * weight for key, weight in weights.items()) / max(active_total, 1e-9)
        return apply_research_caps(row, score_to_tier(score, thresholds))

    return policy


def evaluate_policy(rows: list[ResearchRow], policy: Callable[[ResearchRow], str], initial_equity: float) -> dict[str, Any]:
    baseline_equity = initial_equity
    overlay_equity = initial_equity
    curve = [overlay_equity]
    tier_counts: Counter[str] = Counter()
    taken: list[float] = []
    taken_returns: list[float] = []
    taken_times: list[str] = []
    ruined = False
    for row in sorted(rows, key=lambda item: item.signal_idx):
        tier = policy(row)
        scale = TIER_SCALE[tier]
        tier_counts[tier] += 1
        pnl_ratio = row.pnl / max(row.baseline_equity_before, 1e-9)
        baseline_equity += row.pnl
        overlay_before = overlay_equity
        pnl = overlay_before * pnl_ratio * scale
        overlay_equity += pnl
        if overlay_equity <= 0:
            overlay_equity = 0.0
            ruined = True
            curve.append(overlay_equity)
            if scale > 0:
                taken.append(pnl)
                taken_returns.append(pnl / max(overlay_before, 1e-9))
                taken_times.append(row.entry_time)
            break
        curve.append(overlay_equity)
        if scale > 0:
            taken.append(pnl)
            taken_returns.append(pnl / max(overlay_before, 1e-9))
            taken_times.append(row.entry_time)
    wins = [pnl for pnl in taken if pnl > 0]
    losses = [pnl for pnl in taken if pnl <= 0]
    drawdown = max_drawdown(curve)
    return {
        "final_equity": overlay_equity,
        "total_pnl": overlay_equity - initial_equity,
        "return_pct": (overlay_equity - initial_equity) / max(initial_equity, 1e-9) * 100,
        "max_drawdown_pct": drawdown,
        "trades_taken": len(taken),
        "blocked": tier_counts["block"],
        "tier_counts": dict(tier_counts),
        "win_rate_pct": len(wins) / max(len(taken), 1) * 100,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else (999.0 if wins else 0.0),
        "avg_trade_return_pct": (sum(taken_returns) / len(taken_returns) * 100) if taken_returns else 0.0,
        "trade_sharpe": trade_level_annualized_sharpe(taken_returns, taken_times),
        "ruined": ruined,
        "objective": robust_objective((overlay_equity - initial_equity) / max(initial_equity, 1e-9) * 100, drawdown, len(taken), len(rows)),
    }


def policy_transition_effects(
    rows: list[ResearchRow],
    baseline_policy: Callable[[ResearchRow], str],
    candidate_policy: Callable[[ResearchRow], str],
) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    pnl_delta = 0.0
    winner_pnl_delta = 0.0
    loser_pnl_delta = 0.0
    for row in rows:
        baseline_tier = baseline_policy(row)
        candidate_tier = candidate_policy(row)
        baseline_scale = TIER_SCALE[baseline_tier]
        candidate_scale = TIER_SCALE[candidate_tier]
        delta_scale = candidate_scale - baseline_scale
        pnl_ratio = row.pnl / max(row.baseline_equity_before, 1e-9)
        delta = pnl_ratio * delta_scale
        pnl_delta += delta
        if abs(delta_scale) < 1e-12:
            counts["same"] += 1
        elif row.pnl > 0 and delta_scale > 0:
            counts["winning_trades_scaled_up"] += 1
            winner_pnl_delta += delta
        elif row.pnl > 0 and delta_scale < 0:
            counts["winning_trades_scaled_down"] += 1
            winner_pnl_delta += delta
        elif row.pnl <= 0 and delta_scale > 0:
            counts["losing_trades_scaled_up"] += 1
            loser_pnl_delta += delta
        elif row.pnl <= 0 and delta_scale < 0:
            counts["losing_trades_scaled_down"] += 1
            loser_pnl_delta += delta
    return {
        "same": counts["same"],
        "winning_trades_scaled_up": counts["winning_trades_scaled_up"],
        "winning_trades_scaled_down": counts["winning_trades_scaled_down"],
        "losing_trades_scaled_up": counts["losing_trades_scaled_up"],
        "losing_trades_scaled_down": counts["losing_trades_scaled_down"],
        "pnl_delta_ratio_sum": pnl_delta,
        "winner_pnl_delta_ratio_sum": winner_pnl_delta,
        "loser_pnl_delta_ratio_sum": loser_pnl_delta,
    }


def trade_level_annualized_sharpe(returns: list[float], times: list[str]) -> float:
    if len(returns) < 2:
        return 0.0
    avg = sum(returns) / len(returns)
    variance = sum((item - avg) ** 2 for item in returns) / (len(returns) - 1)
    std = math.sqrt(variance)
    if std <= 1e-12:
        if avg > 0:
            return 999.0
        if avg < 0:
            return -999.0
        return 0.0
    annual_trades = float(len(returns))
    if len(times) >= 2:
        try:
            start = datetime.fromisoformat(times[0].replace("Z", "+00:00"))
            end = datetime.fromisoformat(times[-1].replace("Z", "+00:00"))
            years = max((end - start).total_seconds() / (365.25 * 24 * 3600), 1e-9)
            annual_trades = len(returns) / years
        except ValueError:
            annual_trades = float(len(returns))
    return avg / std * math.sqrt(max(annual_trades, 1e-9))


def max_drawdown(curve: list[float]) -> float:
    peak = curve[0] if curve else 0.0
    drawdown = 0.0
    for value in curve:
        peak = max(peak, value)
        drawdown = min(drawdown, (value - peak) / max(peak, 1e-9))
    return drawdown * 100


def robust_objective(return_pct: float, max_drawdown_pct: float, trades_taken: int, total_rows: int) -> float:
    coverage = trades_taken / max(total_rows, 1)
    if trades_taken < max(30, total_rows * 0.55):
        return -9999.0
    if max_drawdown_pct <= -65:
        return -9999.0 + return_pct / 100
    calmar_like = return_pct / max(abs(max_drawdown_pct), 1.0)
    return calmar_like * math.sqrt(max(coverage, 0.01))


def walk_forward_fold_objective(return_pct: float, max_drawdown_pct: float, trades_taken: int, total_rows: int) -> float:
    coverage = trades_taken / max(total_rows, 1)
    min_trades = max(3, math.ceil(total_rows * 0.35))
    if trades_taken < min_trades:
        return -9999.0
    if max_drawdown_pct <= -70:
        return -9999.0 + return_pct / 100
    calmar_like = return_pct / max(abs(max_drawdown_pct), 1.0)
    return calmar_like * math.sqrt(max(coverage, 0.01))


def candidate_weight_sets() -> list[dict[str, float]]:
    output: list[dict[str, float]] = []
    output.append(dict(HISTORICALLY_TESTABLE_CURRENT_WEIGHTS))
    for technical, orderflow, pattern, trend, dense, range_safety, htf in itertools.product(
        [0.16, 0.18, 0.20],
        [0.16, 0.20, 0.24],
        [0.10, 0.12, 0.14],
        [0.08, 0.10, 0.12],
        [0.06, 0.08, 0.10],
        [0.09, 0.11],
        [0.04, 0.06],
    ):
        weights = {
            "technical_signal_score": technical,
            "orderflow_confirmation_score": orderflow,
            "pattern_confirmation_score": pattern,
            "trend_confirmation_score": trend,
            "dense_zone_breakout_score": dense,
            "range_safety_score": range_safety,
            "htf_alignment_score": htf,
            "news_safety_score": 0.04,
            "btc_leader_score": 0.02,
            "eth_btc_rotation_score": 0.01,
        }
        output.append(weights)
    return output


def candidate_threshold_sets() -> list[dict[str, float]]:
    output: list[dict[str, float]] = []
    for full, strong, normal, weak in itertools.product(
        [0.80, 0.82, 0.84],
        [0.70, 0.72, 0.74],
        [0.60, 0.62, 0.64],
        [0.50, 0.52, 0.55],
    ):
        if weak < normal < strong < full:
            output.append({"full": full, "strong": strong, "normal": normal, "weak": weak})
    return output


def optimize(rows: list[ResearchRow], split: str, initial_equity: float, top: int) -> list[dict[str, Any]]:
    train_rows = [row for row in rows if is_train(row.signal_time, split)]
    validation_rows = [row for row in rows if not is_train(row.signal_time, split)]
    candidates: list[dict[str, Any]] = []
    for weights in candidate_weight_sets():
        for thresholds in candidate_threshold_sets():
            policy = weighted_policy(weights, thresholds)
            train = evaluate_policy(train_rows, policy, initial_equity)
            if train["objective"] <= -9000:
                continue
            validation = evaluate_policy(validation_rows, policy, initial_equity)
            full = evaluate_policy(rows, policy, initial_equity)
            candidates.append(
                {
                    "name": "optimized_weighted_candidate",
                    "weights": weights,
                    "thresholds": thresholds,
                    "train": train,
                    "validation": validation,
                    "full_sample": full,
                    "robust_score": min(train["objective"], validation["objective"]),
                }
            )
    candidates.sort(key=lambda item: (item["robust_score"], item["validation"]["objective"]), reverse=True)
    return candidates[:top]


def evaluate_walk_forward(
    rows: list[ResearchRow],
    policy: Callable[[ResearchRow], str],
    *,
    periods: list[tuple[str, str]],
    initial_equity: float,
    embargo_days: int,
) -> dict[str, Any]:
    folds: list[dict[str, Any]] = []
    validation_returns: list[float] = []
    validation_objectives: list[float] = []
    validation_drawdowns: list[float] = []
    train_validation_gaps: list[float] = []
    total_validation_trades = 0
    ruined_folds = 0
    for start, end in periods:
        train_rows = rows_before(rows, start, embargo_days=embargo_days)
        validation_rows = rows_between(rows, start, end)
        if len(train_rows) < 30 or len(validation_rows) < 5:
            folds.append(
                {
                    "start": start,
                    "end": end,
                    "status": "insufficient_sample",
                    "train_rows": len(train_rows),
                    "validation_rows": len(validation_rows),
                }
            )
            continue
        train = evaluate_policy(train_rows, policy, initial_equity)
        validation = evaluate_policy(validation_rows, policy, initial_equity)
        train_objective = walk_forward_fold_objective(
            float(train["return_pct"]),
            float(train["max_drawdown_pct"]),
            int(train["trades_taken"]),
            len(train_rows),
        )
        validation_objective = walk_forward_fold_objective(
            float(validation["return_pct"]),
            float(validation["max_drawdown_pct"]),
            int(validation["trades_taken"]),
            len(validation_rows),
        )
        validation_returns.append(float(validation["return_pct"]))
        validation_objectives.append(validation_objective)
        validation_drawdowns.append(float(validation["max_drawdown_pct"]))
        train_validation_gaps.append(max(0.0, train_objective - validation_objective))
        total_validation_trades += int(validation["trades_taken"])
        if validation.get("ruined"):
            ruined_folds += 1
        folds.append(
            {
                "start": start,
                "end": end,
                "status": "ok",
                "train_rows": len(train_rows),
                "validation_rows": len(validation_rows),
                "train_walk_forward_objective": train_objective,
                "validation_walk_forward_objective": validation_objective,
                "train": train,
                "validation": validation,
            }
        )
    if not validation_objectives:
        return {
            "status": "insufficient_sample",
            "folds": folds,
            "ok_folds": 0,
            "total_validation_trades": total_validation_trades,
            "walk_forward_score": -9999.0,
        }
    negative_folds = sum(1 for item in validation_returns if item <= 0)
    median_objective = median(validation_objectives)
    min_objective = min(validation_objectives)
    gap_penalty = median(train_validation_gaps) if train_validation_gaps else 0.0
    drawdown_penalty = abs(min(validation_drawdowns)) / 100.0 if validation_drawdowns else 0.0
    sample_penalty = 1.0 if total_validation_trades < 100 else 0.0
    walk_forward_score = (
        median_objective
        + min_objective * 0.35
        - gap_penalty * 0.25
        - drawdown_penalty * 0.2
        - negative_folds * 0.5
        - ruined_folds * 10_000
        - sample_penalty
    )
    return {
        "status": "ok",
        "folds": folds,
        "ok_folds": len(validation_objectives),
        "negative_folds": negative_folds,
        "ruined_folds": ruined_folds,
        "total_validation_trades": total_validation_trades,
        "median_validation_return_pct": median(validation_returns),
        "min_validation_return_pct": min(validation_returns),
        "median_validation_objective": median_objective,
        "min_validation_objective": min_objective,
        "median_train_validation_gap": gap_penalty,
        "worst_validation_drawdown_pct": min(validation_drawdowns),
        "walk_forward_score": walk_forward_score,
    }


def optimize_walk_forward(
    rows: list[ResearchRow],
    *,
    periods: list[tuple[str, str]],
    initial_equity: float,
    embargo_days: int,
    top: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for weights in candidate_weight_sets():
        for thresholds in candidate_threshold_sets():
            policy = weighted_policy(weights, thresholds)
            walk_forward = evaluate_walk_forward(
                rows,
                policy,
                periods=periods,
                initial_equity=initial_equity,
                embargo_days=embargo_days,
            )
            if walk_forward["status"] != "ok":
                continue
            full = evaluate_policy(rows, policy, initial_equity)
            candidates.append(
                {
                    "name": "walk_forward_weighted_candidate",
                    "weights": weights,
                    "thresholds": thresholds,
                    "walk_forward": walk_forward,
                    "full_sample": full,
                    "robust_score": walk_forward["walk_forward_score"],
                }
            )
    candidates.sort(
        key=lambda item: (
            item["walk_forward"].get("ruined_folds", 0),
            item["walk_forward"].get("negative_folds", 0),
            -item["robust_score"],
            -item["walk_forward"]["min_validation_objective"],
            -item["walk_forward"]["total_validation_trades"],
        )
    )
    return candidates[:top]


def rank_walk_forward_candidates(
    rows: list[ResearchRow],
    candidates: list[dict[str, Any]],
    *,
    periods: list[tuple[str, str]],
    initial_equity: float,
    embargo_days: int,
    top: int,
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        policy = weighted_policy(candidate["weights"], candidate["thresholds"])
        walk_forward = evaluate_walk_forward(
            rows,
            policy,
            periods=periods,
            initial_equity=initial_equity,
            embargo_days=embargo_days,
        )
        if walk_forward["status"] != "ok":
            continue
        ranked.append(
            {
                "name": "walk_forward_reranked_candidate",
                "weights": candidate["weights"],
                "thresholds": candidate["thresholds"],
                "walk_forward": walk_forward,
                "single_split": {
                    "robust_score": candidate["robust_score"],
                    "train": candidate["train"],
                    "validation": candidate["validation"],
                },
                "full_sample": candidate["full_sample"],
                "robust_score": walk_forward["walk_forward_score"],
            }
        )
    ranked.sort(
        key=lambda item: (
            item["walk_forward"].get("ruined_folds", 0),
            item["walk_forward"].get("negative_folds", 0),
            -item["robust_score"],
            -item["walk_forward"]["min_validation_objective"],
            -item["walk_forward"]["total_validation_trades"],
        )
    )
    return ranked[:top]


def evaluate_named_walk_forward_policies(
    rows: list[ResearchRow],
    policies: dict[str, Callable[[ResearchRow], str]],
    *,
    periods: list[tuple[str, str]],
    initial_equity: float,
    embargo_days: int,
) -> dict[str, Any]:
    return {
        name: {
            "walk_forward": evaluate_walk_forward(
                rows,
                policy,
                periods=periods,
                initial_equity=initial_equity,
                embargo_days=embargo_days,
            ),
            "full_sample": evaluate_policy(rows, policy, initial_equity),
        }
        for name, policy in policies.items()
    }


def feature_effects(rows: list[ResearchRow]) -> dict[str, Any]:
    winners = [row for row in rows if row.pnl > 0]
    losers = [row for row in rows if row.pnl <= 0]
    output: dict[str, Any] = {}
    for key in sorted(rows[0].scores) if rows else []:
        win_values = [row.scores[key] for row in winners]
        loss_values = [row.scores[key] for row in losers]
        all_values = win_values + loss_values
        variance = 0.0
        if len(all_values) > 1:
            avg = sum(all_values) / len(all_values)
            variance = sum((item - avg) ** 2 for item in all_values) / len(all_values)
        output[key] = {
            "winner_median": median(win_values) if win_values else None,
            "loser_median": median(loss_values) if loss_values else None,
            "effect_size": ((sum(win_values) / len(win_values)) - (sum(loss_values) / len(loss_values))) / math.sqrt(variance)
            if win_values and loss_values and variance > 1e-12
            else 0.0,
        }
    return output


FACTOR_CHANNEL_WEIGHTS: dict[str, dict[str, float]] = {
    "profit_expansion": {
        "orderflow_confirmation_score": 0.24,
        "pattern_confirmation_score": 0.18,
        "dense_zone_breakout_score": 0.15,
        "trend_confirmation_score": 0.12,
        "htf_alignment_score": 0.10,
        "technical_signal_score": 0.09,
        "volume_score": 0.07,
        "breakout_score": 0.05,
    },
    "execution_quality": {
        "orderflow_confirmation_score": 0.32,
        "volume_score": 0.18,
        "dense_zone_breakout_score": 0.16,
        "range_safety_score": 0.14,
        "pattern_confirmation_score": 0.12,
        "breakout_quality_score": 0.08,
    },
    "loss_suppression_risk": {
        "range_risk_score": 0.22,
        "orderflow_weakness_score": 0.20,
        "pattern_weakness_score": 0.14,
        "dense_weakness_score": 0.12,
        "trend_weakness_score": 0.10,
        "regime_risk_score": 0.10,
        "overextension_risk_score": 0.08,
        "htf_conflict_risk_score": 0.04,
    },
    "context_quality": {
        "htf_alignment_score": 0.22,
        "trend_confirmation_score": 0.18,
        "range_safety_score": 0.16,
        "btc_leader_score": 0.14,
        "eth_btc_rotation_score": 0.12,
        "news_direction_alignment_score": 0.10,
        "news_safety_score": 0.08,
    },
}


def _weighted_channel(values: dict[str, float], weights: dict[str, float]) -> float:
    total = sum(weights.values())
    return clip(sum(clip(values.get(key, 0.0)) * weight for key, weight in weights.items()) / max(total, 1e-9))


def factor_channel_scores(row: ResearchRow) -> dict[str, float]:
    """Pre-entry factor channels for sizing research.

    The channel scores intentionally use only signal-time inputs already present
    in `ResearchRow.scores` and `ResearchRow.raw`. Outcome fields such as PnL,
    MAE, and MFE are reserved for evaluation and must not enter this function.
    """

    scores = row.scores
    raw = row.raw
    breakout = scores.get("breakout_score", 0.0)
    orderflow = scores.get("orderflow_confirmation_score", 0.0)
    pattern = scores.get("pattern_confirmation_score", 0.0)
    dense = scores.get("dense_zone_breakout_score", 0.0)
    trend = scores.get("trend_confirmation_score", 0.0)
    htf_alignment = scores.get("htf_alignment_score", 0.0)
    regime_range = clip(float(raw.get("regime_range_score") or (1.0 - scores.get("range_safety_score", 0.0))))
    regime_risk = clip(float(raw.get("regime_risk_score") or (1.0 - scores.get("regime_risk_safety_score", 0.0))))
    overextension_risk = clip(max(0.0, breakout - 0.72) * (0.45 + max(0.0, 0.58 - orderflow) + max(0.0, 0.55 - pattern)))
    htf_conflict_risk = 1.0 if str(raw.get("htf_signal_alignment") or "") == "conflict" else clip(1.0 - htf_alignment)
    breakout_quality_score = clip(1.0 - overextension_risk)
    channel_inputs = {
        **scores,
        "breakout_quality_score": breakout_quality_score,
        "range_risk_score": regime_range,
        "orderflow_weakness_score": clip(1.0 - orderflow),
        "pattern_weakness_score": clip(1.0 - pattern),
        "dense_weakness_score": clip(1.0 - dense),
        "trend_weakness_score": clip(1.0 - trend),
        "regime_risk_score": regime_risk,
        "overextension_risk_score": overextension_risk,
        "htf_conflict_risk_score": htf_conflict_risk,
    }
    return {
        channel: _weighted_channel(channel_inputs, weights)
        for channel, weights in FACTOR_CHANNEL_WEIGHTS.items()
    }


def factor_channel_effects(rows: list[ResearchRow]) -> dict[str, Any]:
    winners = [row for row in rows if row.pnl > 0]
    losers = [row for row in rows if row.pnl <= 0]
    output: dict[str, Any] = {}
    channels = sorted(FACTOR_CHANNEL_WEIGHTS)
    for channel in channels:
        win_values = [factor_channel_scores(row)[channel] for row in winners]
        loss_values = [factor_channel_scores(row)[channel] for row in losers]
        all_values = win_values + loss_values
        variance = 0.0
        if len(all_values) > 1:
            avg = sum(all_values) / len(all_values)
            variance = sum((item - avg) ** 2 for item in all_values) / len(all_values)
        raw_effect = (
            ((sum(win_values) / len(win_values)) - (sum(loss_values) / len(loss_values))) / math.sqrt(variance)
            if win_values and loss_values and variance > 1e-12
            else 0.0
        )
        desired_direction = "loser_higher_is_good" if channel == "loss_suppression_risk" else "winner_higher_is_good"
        desired_effect = -raw_effect if channel == "loss_suppression_risk" else raw_effect
        output[channel] = {
            "winner_median": median(win_values) if win_values else None,
            "loser_median": median(loss_values) if loss_values else None,
            "winner_mean": sum(win_values) / len(win_values) if win_values else None,
            "loser_mean": sum(loss_values) / len(loss_values) if loss_values else None,
            "raw_effect_size": raw_effect,
            "desired_effect_size": desired_effect,
            "desired_direction": desired_direction,
            "weights": FACTOR_CHANNEL_WEIGHTS[channel],
        }
    return output


def build_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# AI 五档仓位权重回测研究",
        "",
        "本研究在当前 ETH 1h TV 对齐纯策略 291 笔交易上，叠加 Binance 历史 aggTrades 订单流 proxy，重新评估 AI 五档仓位逻辑。",
        "",
        "## 关键限制",
        "- 没有完整历史新闻归档，因此 `news_direction_alignment_score` 不参与历史最优搜索；缺失新闻不被当成负面证据。",
        "- 订单流使用 Binance futures aggTrades proxy，不是完整盘口深度。",
        "- 这是开仓前一次性分档研究，不包含持仓后加仓/减仓。",
        "- 最优候选必须先灰度，不得直接大资金切换。",
        "",
        "## 固定策略样本",
        f"- 总交易数: `{summary['sample']['rows']}`",
        f"- 训练集: `{summary['sample']['train_rows']}`，验证集: `{summary['sample']['validation_rows']}`，split `{summary['split']}`",
        "",
        "## 基准策略",
    ]
    for name, result in summary["baseline_policies"].items():
        lines.append(
            f"- `{name}`: return `{result['full_sample']['return_pct']:.2f}%`, DD `{result['full_sample']['max_drawdown_pct']:.2f}%`, "
            f"trades `{result['full_sample']['trades_taken']}`, tiers `{result['full_sample']['tier_counts']}`"
        )
    best = summary["optimized_candidates"][0] if summary["optimized_candidates"] else None
    if best:
        lines.extend(
            [
                "",
                "## 当前最稳健候选",
                f"- robust_score: `{best['robust_score']:.4f}`",
                f"- train: return `{best['train']['return_pct']:.2f}%`, DD `{best['train']['max_drawdown_pct']:.2f}%`, trades `{best['train']['trades_taken']}`",
                f"- validation: return `{best['validation']['return_pct']:.2f}%`, DD `{best['validation']['max_drawdown_pct']:.2f}%`, trades `{best['validation']['trades_taken']}`",
                f"- full_sample: return `{best['full_sample']['return_pct']:.2f}%`, DD `{best['full_sample']['max_drawdown_pct']:.2f}%`, trades `{best['full_sample']['trades_taken']}`",
                f"- thresholds: `{best['thresholds']}`",
                f"- weights: `{best['weights']}`",
            ]
        )
    lines.extend(["", "## 因子效果排序"])
    for key, stats in sorted(
        summary["feature_effects"].items(),
        key=lambda item: abs(item[1]["effect_size"]),
        reverse=True,
    )[:12]:
        lines.append(
            f"- `{key}`: win_median `{stats['winner_median']}`, loss_median `{stats['loser_median']}`, effect `{stats['effect_size']:.3f}`"
        )
    lines.extend(["", "## 结论"])
    lines.extend(f"- {item}" for item in summary["conclusions"])
    return "\n".join(lines) + "\n"


def build_markdown_v2(summary: dict[str, Any]) -> str:
    lines = [
        "# AI five-tier sizing walk-forward research",
        "",
        "This report evaluates ETH 1h pre-entry five-tier sizing candidates using local, deterministic historical signals.",
        "DeepSeek is not called here and is not treated as a historical backtest engine.",
        "",
        "## Non-negotiable limits",
        "- Historical news direction is incomplete, so `news_direction_alignment_score` is not optimized from hindsight.",
        "- Orderflow uses Binance futures aggTrades proxy; it measures participation/liquidity/impulse quality, not full order-book depth.",
        "- The result is a candidate for shadow ledger, not an automatic live replacement.",
        "- The local ETH 1h strategy definition and TradingView-aligned execution contract are unchanged.",
        "",
        "## Sample",
        f"- rows: `{summary['sample']['rows']}`",
        f"- single_split: `{summary['split']}`",
        f"- single_split_train_rows: `{summary['sample']['train_rows']}`",
        f"- single_split_validation_rows: `{summary['sample']['validation_rows']}`",
        f"- walk_forward_periods: `{summary.get('walk_forward_periods', [])}`",
        f"- embargo_days: `{summary.get('embargo_days', 0)}`",
        "",
        "## Baselines",
    ]
    for name, result in summary["baseline_policies"].items():
        full = result["full_sample"]
        lines.append(
            f"- `{name}`: return `{full['return_pct']:.2f}%`, DD `{full['max_drawdown_pct']:.2f}%`, "
            f"trades `{full['trades_taken']}`, tiers `{full['tier_counts']}`"
        )
    if summary.get("baseline_walk_forward"):
        lines.extend(["", "## Baseline rolling walk-forward checks"])
        for name, result in summary["baseline_walk_forward"].items():
            wf = result["walk_forward"]
            full = result["full_sample"]
            lines.append(
                f"- `{name}`: wf_score `{wf['walk_forward_score']:.4f}`, ok `{wf['ok_folds']}`, "
                f"negative `{wf['negative_folds']}`, ruined `{wf.get('ruined_folds', 0)}`, "
                f"min_val_return `{wf.get('min_validation_return_pct', 0):.2f}%`, "
                f"full_return `{full['return_pct']:.2f}%`, full_DD `{full['max_drawdown_pct']:.2f}%`"
            )

    if summary.get("transition_effects"):
        lines.extend(["", "## Transition effects versus legacy factor-ranked policy"])
        for name, stats in summary["transition_effects"].items():
            lines.append(
                f"- `{name}`: same `{stats['same']}`, win_up `{stats['winning_trades_scaled_up']}`, "
                f"win_down `{stats['winning_trades_scaled_down']}`, loss_up `{stats['losing_trades_scaled_up']}`, "
                f"loss_down `{stats['losing_trades_scaled_down']}`, "
                f"winner_delta `{stats['winner_pnl_delta_ratio_sum']:.4f}`, loser_delta `{stats['loser_pnl_delta_ratio_sum']:.4f}`"
            )

    best = summary["optimized_candidates"][0] if summary["optimized_candidates"] else None
    if best:
        lines.extend(
            [
                "",
                "## Best single-split candidate",
                f"- robust_score: `{best['robust_score']:.4f}`",
                f"- train: return `{best['train']['return_pct']:.2f}%`, DD `{best['train']['max_drawdown_pct']:.2f}%`, trades `{best['train']['trades_taken']}`",
                f"- validation: return `{best['validation']['return_pct']:.2f}%`, DD `{best['validation']['max_drawdown_pct']:.2f}%`, trades `{best['validation']['trades_taken']}`",
                f"- full_sample: return `{best['full_sample']['return_pct']:.2f}%`, DD `{best['full_sample']['max_drawdown_pct']:.2f}%`, trades `{best['full_sample']['trades_taken']}`",
                f"- thresholds: `{best['thresholds']}`",
                f"- weights: `{best['weights']}`",
            ]
        )

    wf_best = summary.get("walk_forward_candidates", [None])[0] if summary.get("walk_forward_candidates") else None
    if wf_best:
        wf = wf_best["walk_forward"]
        lines.extend(
            [
                "",
                "## Best rolling walk-forward candidate",
                f"- walk_forward_score: `{wf['walk_forward_score']:.4f}`",
                f"- ok_folds: `{wf['ok_folds']}`",
                f"- negative_folds: `{wf['negative_folds']}`",
                f"- ruined_folds: `{wf.get('ruined_folds', 0)}`",
                f"- total_validation_trades: `{wf['total_validation_trades']}`",
                f"- median_validation_return: `{wf['median_validation_return_pct']:.2f}%`",
                f"- min_validation_return: `{wf['min_validation_return_pct']:.2f}%`",
                f"- worst_validation_drawdown: `{wf['worst_validation_drawdown_pct']:.2f}%`",
                f"- median_train_validation_gap: `{wf['median_train_validation_gap']:.4f}`",
                f"- full_sample: return `{wf_best['full_sample']['return_pct']:.2f}%`, DD `{wf_best['full_sample']['max_drawdown_pct']:.2f}%`, trades `{wf_best['full_sample']['trades_taken']}`",
                f"- thresholds: `{wf_best['thresholds']}`",
                f"- weights: `{wf_best['weights']}`",
            ]
        )

    lines.extend(["", "## Feature effect ranking"])
    for key, stats in sorted(
        summary["feature_effects"].items(),
        key=lambda item: abs(item[1]["effect_size"]),
        reverse=True,
    )[:12]:
        lines.append(
            f"- `{key}`: win_median `{stats['winner_median']}`, loss_median `{stats['loser_median']}`, effect `{stats['effect_size']:.3f}`"
        )

    lines.extend(["", "## Factor channel effects"])
    for key, stats in sorted(
        summary.get("factor_channel_effects", {}).items(),
        key=lambda item: abs(item[1]["desired_effect_size"]),
        reverse=True,
    ):
        lines.append(
            f"- `{key}`: desired `{stats['desired_direction']}`, "
            f"win_median `{stats['winner_median']:.3f}`, loss_median `{stats['loser_median']:.3f}`, "
            f"raw_effect `{stats['raw_effect_size']:.3f}`, desired_effect `{stats['desired_effect_size']:.3f}`"
        )

    lines.extend(["", "## Conclusions"])
    lines.extend(f"- {item}" for item in summary["conclusions"])
    return "\n".join(lines) + "\n"


def main() -> None:
    args = parse_args()
    features_payload = load_json(Path(args.features))
    orderflow_payload = load_json(Path(args.orderflow))
    walk_forward_periods = parse_walk_forward_periods(args.walk_forward_periods)
    rows = build_rows(features_payload, orderflow_payload, args.split)
    train_rows = [row for row in rows if is_train(row.signal_time, args.split)]
    validation_rows = [row for row in rows if not is_train(row.signal_time, args.split)]
    baselines = {
        "balanced_candidate_v1": balanced_policy,
        "balanced_volume_dedup_v1": balanced_volume_dedup_policy,
        "structure_context_v1": structure_context_policy,
        "factor_ranked_current_weights_zero_news": current_factor_policy,
        "factor_ranked_current_weights_testable_renormalized": current_factor_testable_renormalized_policy,
        "calibrated_v1_controlled_live": calibrated_v1_controlled_research_policy,
        "calibrated_v2_loss_aware": calibrated_v2_loss_aware_research_policy,
        "calibrated_v21_profit_loss": calibrated_v21_profit_loss_research_policy,
    }
    baseline_results = {
        name: {
            "train": evaluate_policy(train_rows, policy, args.initial_equity),
            "validation": evaluate_policy(validation_rows, policy, args.initial_equity),
            "full_sample": evaluate_policy(rows, policy, args.initial_equity),
        }
        for name, policy in baselines.items()
    }
    baseline_walk_forward = evaluate_named_walk_forward_policies(
        rows,
        baselines,
        periods=walk_forward_periods,
        initial_equity=args.initial_equity,
        embargo_days=args.embargo_days,
    )
    candidate_pool = optimize(rows, args.split, args.initial_equity, max(args.top, 120))
    candidates = candidate_pool[: args.top]
    walk_forward_candidates = rank_walk_forward_candidates(
        rows,
        candidate_pool,
        periods=walk_forward_periods,
        initial_equity=args.initial_equity,
        embargo_days=args.embargo_days,
        top=args.top,
    )
    best = candidates[0] if candidates else None
    wf_best = walk_forward_candidates[0] if walk_forward_candidates else None
    conclusions = [
        "当前没有历史新闻归档，不能把新闻方向权重做成历史最优；新闻应继续作为实时方向确认和风险 cap。",
        "订单流历史 proxy 更支持“参与度/流动性/冲击质量”，不支持简单方向同向满仓。",
        "如果最优候选只在全样本好、验证集不稳，不能上线，只能继续研究。",
    ]
    if best:
        current = baseline_results["factor_ranked_current_weights_zero_news"]["full_sample"]
        balanced = baseline_results["balanced_candidate_v1"]["full_sample"]
        if best["validation"]["objective"] > baseline_results["balanced_candidate_v1"]["validation"]["objective"]:
            conclusions.append("最稳健候选在验证集优于 balanced_candidate_v1，可进入小仓灰度候选。")
        else:
            conclusions.append("最稳健候选没有稳定超过 balanced_candidate_v1，不建议替换平衡版。")
        if current["return_pct"] < balanced["return_pct"]:
            conclusions.append("当前权重在缺失新闻方向时偏保守，这解释了实盘五档经常低于平衡版。")
    conclusions = [
        f"This version accepts the {len(rows)} closed-trade research sample as the current AI tier-sizing research baseline.",
        "Historical news direction is incomplete, so news direction must remain a live contextual factor instead of a hindsight-optimized historical weight.",
        "Orderflow is useful as participation/liquidity/impulse quality, but the current archive does not prove simple directional orderflow timing.",
        "Rolling walk-forward uses baseline_equity_before for each historical trade; validation windows no longer misuse full-path absolute PnL after resetting equity.",
    ]
    if best:
        current = baseline_results["factor_ranked_current_weights_zero_news"]["full_sample"]
        balanced = baseline_results["balanced_candidate_v1"]["full_sample"]
        if best["validation"]["objective"] > baseline_results["balanced_candidate_v1"]["validation"]["objective"]:
            conclusions.append(
                "The single-split optimized candidate beats balanced_candidate_v1 in validation, but this alone is insufficient for live replacement."
            )
        else:
            conclusions.append(
                "The single-split optimized candidate does not reliably beat balanced_candidate_v1; keep it in research only."
            )
        if current["return_pct"] < balanced["return_pct"]:
            conclusions.append(
                "The current live-style weights are more conservative than balanced_candidate_v1 when historical news direction is absent."
            )
    conclusions.append(
        "Rolling walk-forward uses only prior training signals for each validation window and applies embargo to reduce label leakage."
    )
    if wf_best:
        if (
            wf_best["walk_forward"].get("ruined_folds", 0) == 0
            and wf_best["walk_forward"]["negative_folds"] == 0
            and wf_best["walk_forward"]["ok_folds"] >= 4
        ):
            conclusions.append(
                "Rolling walk-forward candidate has no negative validation fold, but it is still a shadow-ledger candidate, not a direct live replacement."
            )
        else:
            conclusions.append(
                "Rolling walk-forward candidate has weak validation coverage, negative folds, or ruined folds; do not switch live defaults directly."
            )
    summary = {
        "created_at": datetime.now(UTC).isoformat(),
        "split": args.split,
        "walk_forward_periods": walk_forward_periods,
        "embargo_days": args.embargo_days,
        "inputs": {"features": args.features, "orderflow": args.orderflow},
        "sample": {"rows": len(rows), "train_rows": len(train_rows), "validation_rows": len(validation_rows)},
        "baseline_policies": baseline_results,
        "baseline_walk_forward": baseline_walk_forward,
        "transition_effects": {
            "calibrated_v1_controlled_live": policy_transition_effects(
                rows,
                current_factor_policy,
                calibrated_v1_controlled_research_policy,
            ),
            "calibrated_v2_loss_aware": policy_transition_effects(
                rows,
                current_factor_policy,
                calibrated_v2_loss_aware_research_policy,
            ),
            "calibrated_v21_profit_loss": policy_transition_effects(
                rows,
                current_factor_policy,
                calibrated_v21_profit_loss_research_policy,
            ),
            "balanced_volume_dedup_v1": policy_transition_effects(
                rows,
                current_factor_policy,
                balanced_volume_dedup_policy,
            ),
            "structure_context_v1": policy_transition_effects(
                rows,
                current_factor_policy,
                structure_context_policy,
            ),
        },
        "optimized_candidates": candidates,
        "walk_forward_candidates": walk_forward_candidates,
        "feature_effects": feature_effects(rows),
        "factor_channel_effects": factor_channel_effects(rows),
        "conclusions": conclusions,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    output.with_suffix(".md").write_text(build_markdown_v2(summary), encoding="utf-8")
    print(f"[ai_tier_weight_research] wrote {output} and {output.with_suffix('.md')}")


if __name__ == "__main__":
    main()
