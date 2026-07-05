from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import mean, pstdev
from typing import Any


Policy = Callable[["WalkForwardTrade"], float]


@dataclass(frozen=True)
class WalkForwardTrade:
    signal_time: str
    pnl: float
    baseline_equity_before: float
    fee_paid: float = 0.0
    slippage_paid: float = 0.0
    funding_paid: float = 0.0
    max_adverse_excursion_pct: float = 0.0
    params: Mapping[str, float] = field(default_factory=dict)
    scores: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class WalkForwardWindow:
    train_start: str
    train_end: str
    validation_start: str
    validation_end: str
    out_of_sample_start: str
    out_of_sample_end: str


def baseline_policy(_trade: WalkForwardTrade) -> float:
    return 1.0


def ai_veto_overlay(trade: WalkForwardTrade) -> float:
    risk = _score(trade, "risk_score", "loss_risk_score", "false_breakout_risk")
    if risk >= 0.80:
        return 0.0
    return 1.0


def ai_reduce_overlay(trade: WalkForwardTrade) -> float:
    risk = _score(trade, "risk_score", "loss_risk_score", "false_breakout_risk")
    if risk >= 0.80:
        return 0.25
    if risk >= 0.60:
        return 0.5
    return 1.0


def ai_full_size_strict_consensus_overlay(trade: WalkForwardTrade) -> float:
    consensus = _score(
        trade,
        "consensus_score",
        "entry_quality_score",
        "orderflow_confirmation_score",
        "pattern_confirmation_score",
    )
    risk = _score(trade, "risk_score", "loss_risk_score", "false_breakout_risk")
    if risk >= 0.75:
        return 0.0
    if consensus >= 0.80 and risk <= 0.45:
        return 1.0
    return 0.5


DEFAULT_POLICIES: dict[str, Policy] = {
    "baseline_trend": baseline_policy,
    "ai_veto_overlay": ai_veto_overlay,
    "ai_reduce_overlay": ai_reduce_overlay,
    "ai_full_size_strict_consensus_overlay": ai_full_size_strict_consensus_overlay,
}


def evaluate_walk_forward_harness(
    trades: Iterable[WalkForwardTrade],
    windows: Iterable[WalkForwardWindow],
    *,
    policies: Mapping[str, Policy] | None = None,
    candidate_name: str = "candidate",
    candidate_policy: Policy | None = None,
    min_validation_trades: int = 20,
    min_oos_trades: int = 10,
    min_profit_factor: float = 1.0,
    max_drawdown_worsening_ratio: float = 1.20,
) -> dict[str, Any]:
    rows = sorted(list(trades), key=lambda item: _parse_time(item.signal_time))
    active_policies = dict(DEFAULT_POLICIES if policies is None else policies)
    if candidate_policy is not None:
        active_policies[candidate_name] = candidate_policy

    folds = []
    rejected_reasons: list[str] = []
    for window in windows:
        fold = {
            "window": {
                "train": [window.train_start, window.train_end],
                "validation": [window.validation_start, window.validation_end],
                "out_of_sample": [window.out_of_sample_start, window.out_of_sample_end],
            },
            "policies": {},
        }
        splits = {
            "train": _between(rows, window.train_start, window.train_end),
            "validation": _between(rows, window.validation_start, window.validation_end),
            "out_of_sample": _between(rows, window.out_of_sample_start, window.out_of_sample_end),
        }
        for name, policy in active_policies.items():
            fold["policies"][name] = {
                split_name: score_policy(split_rows, policy)
                for split_name, split_rows in splits.items()
            }
        if candidate_policy is not None:
            rejected_reasons.extend(
                _candidate_rejection_reasons(
                    baseline=fold["policies"]["baseline_trend"],
                    candidate=fold["policies"][candidate_name],
                    min_validation_trades=min_validation_trades,
                    min_oos_trades=min_oos_trades,
                    min_profit_factor=min_profit_factor,
                    max_drawdown_worsening_ratio=max_drawdown_worsening_ratio,
                )
            )
        folds.append(fold)

    aggregate = {
        name: {
            "train": _aggregate_metrics([fold["policies"][name]["train"] for fold in folds]),
            "validation": _aggregate_metrics([fold["policies"][name]["validation"] for fold in folds]),
            "out_of_sample": _aggregate_metrics([fold["policies"][name]["out_of_sample"] for fold in folds]),
        }
        for name in active_policies
    }
    status = "accepted" if candidate_policy is not None and not rejected_reasons else "research_only"
    if rejected_reasons:
        status = "rejected"
    return {
        "status": status,
        "rejected_reasons": sorted(set(rejected_reasons)),
        "policies": list(active_policies),
        "folds": folds,
        "aggregate": aggregate,
        "note": "Walk-forward harness is offline research only; it must not call DeepSeek per candle or auto-apply live changes.",
    }


def score_policy(trades: Iterable[WalkForwardTrade], policy: Policy) -> dict[str, Any]:
    rows = list(trades)
    if not rows:
        return _empty_metrics()
    equity = float(rows[0].baseline_equity_before or 0.0)
    curve = [equity]
    scaled_pnls: list[float] = []
    gross_abs_pnl = 0.0
    total_cost = 0.0
    mae_values: list[float] = []
    param_values: dict[str, list[float]] = {}

    for row in rows:
        scale = min(max(float(policy(row)), 0.0), 1.0)
        scaled_pnl = row.pnl * scale
        if scale > 0:
            scaled_pnls.append(scaled_pnl)
        gross_abs_pnl += abs(row.pnl * scale)
        total_cost += (abs(row.fee_paid) + abs(row.slippage_paid) + abs(row.funding_paid)) * scale
        mae_values.append(float(row.max_adverse_excursion_pct or 0.0) * scale)
        for key, value in row.params.items():
            param_values.setdefault(key, []).append(float(value))
        equity = max(equity + scaled_pnl, 0.0)
        curve.append(equity)

    wins = [item for item in scaled_pnls if item > 0]
    losses = [item for item in scaled_pnls if item < 0]
    total_return_pct = (curve[-1] - curve[0]) / max(curve[0], 1e-9) * 100
    return {
        "return_pct": total_return_pct,
        "max_drawdown_pct": _max_drawdown(curve),
        "win_rate_pct": len(wins) / max(len(scaled_pnls), 1) * 100,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses and abs(sum(losses)) > 1e-12 else (999.0 if wins else 0.0),
        "signal_count": len(rows),
        "trade_count": len(scaled_pnls),
        "blocked_count": sum(1 for row in rows if policy(row) <= 0),
        "cost_ratio": total_cost / max(gross_abs_pnl, 1e-9),
        "max_adverse_excursion_pct": min(mae_values) if mae_values else 0.0,
        "parameter_stability": _parameter_stability(param_values),
    }


def _candidate_rejection_reasons(
    *,
    baseline: Mapping[str, Mapping[str, Any]],
    candidate: Mapping[str, Mapping[str, Any]],
    min_validation_trades: int,
    min_oos_trades: int,
    min_profit_factor: float,
    max_drawdown_worsening_ratio: float,
) -> list[str]:
    reasons: list[str] = []
    for split_name, min_trades in [("validation", min_validation_trades), ("out_of_sample", min_oos_trades)]:
        base = baseline[split_name]
        cand = candidate[split_name]
        if cand["trade_count"] < min_trades:
            reasons.append(f"{split_name}_insufficient_trades")
        if cand["profit_factor"] < min_profit_factor:
            reasons.append(f"{split_name}_profit_factor_below_min")
        if cand["return_pct"] <= base["return_pct"]:
            reasons.append(f"{split_name}_return_not_improved")
        if abs(cand["max_drawdown_pct"]) > abs(base["max_drawdown_pct"]) * max_drawdown_worsening_ratio:
            reasons.append(f"{split_name}_drawdown_materially_worse")
    if candidate["validation"]["parameter_stability"] < 0.70:
        reasons.append("parameter_stability_low")
    return reasons


def _aggregate_metrics(items: list[Mapping[str, Any]]) -> dict[str, Any]:
    if not items:
        return _empty_metrics()
    return {
        "return_pct": sum(float(item["return_pct"]) for item in items),
        "max_drawdown_pct": min(float(item["max_drawdown_pct"]) for item in items),
        "win_rate_pct": mean(float(item["win_rate_pct"]) for item in items),
        "profit_factor": mean(float(item["profit_factor"]) for item in items),
        "signal_count": sum(int(item.get("signal_count", item["trade_count"])) for item in items),
        "trade_count": sum(int(item["trade_count"]) for item in items),
        "blocked_count": sum(int(item["blocked_count"]) for item in items),
        "cost_ratio": mean(float(item["cost_ratio"]) for item in items),
        "max_adverse_excursion_pct": min(float(item["max_adverse_excursion_pct"]) for item in items),
        "parameter_stability": mean(float(item["parameter_stability"]) for item in items),
    }


def _empty_metrics() -> dict[str, Any]:
    return {
        "return_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "win_rate_pct": 0.0,
        "profit_factor": 0.0,
        "trade_count": 0,
        "signal_count": 0,
        "blocked_count": 0,
        "cost_ratio": 0.0,
        "max_adverse_excursion_pct": 0.0,
        "parameter_stability": 1.0,
    }


def _score(trade: WalkForwardTrade, *keys: str) -> float:
    values = [float(trade.scores.get(key, 0.0) or 0.0) for key in keys if key in trade.scores]
    if not values:
        return 0.0
    return sum(values) / len(values)


def _between(rows: list[WalkForwardTrade], start: str, end: str) -> list[WalkForwardTrade]:
    start_dt = _parse_time(start)
    end_dt = _parse_time(end)
    return [row for row in rows if start_dt <= _parse_time(row.signal_time) < end_dt]


def _parse_time(value: str) -> datetime:
    raw = str(value)
    if len(raw) == 10:
        raw = f"{raw}T00:00:00+00:00"
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)


def _max_drawdown(curve: list[float]) -> float:
    peak = curve[0] if curve else 0.0
    worst = 0.0
    for value in curve:
        peak = max(peak, value)
        worst = min(worst, (value - peak) / max(peak, 1e-9))
    return worst * 100


def _parameter_stability(values: Mapping[str, list[float]]) -> float:
    if not values:
        return 1.0
    scores = []
    for series in values.values():
        if len(series) < 2:
            scores.append(1.0)
            continue
        avg = abs(mean(series))
        if avg <= 1e-9:
            scores.append(1.0)
            continue
        coefficient = pstdev(series) / avg
        scores.append(max(0.0, 1.0 - coefficient))
    return mean(scores) if scores else 1.0
