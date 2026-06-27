from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_quant_trader.research.factors import (
    FactorAvailability,
    FactorRole,
    FactorSpec,
    optimization_eligible_factors,
)


DEFAULT_INPUT = Path("data/research/pure_strategy_tier_research_eth_2020_2026_no_ema.json")
DEFAULT_OUTPUT = Path("data/research/factor_combination_research_eth_2020_2026.json")

TIER_SCALE = {"block": 0.0, "weak": 0.25, "normal": 0.5, "strong": 0.75, "full": 1.0}
TIER_ORDER = ["block", "weak", "normal", "strong", "full"]
DEFAULT_THRESHOLDS = (
    {"full": 0.84, "strong": 0.74, "normal": 0.64, "weak": 0.54},
    {"full": 0.80, "strong": 0.70, "normal": 0.60, "weak": 0.50},
    {"full": 0.76, "strong": 0.66, "normal": 0.56, "weak": 0.48},
)


@dataclass(frozen=True)
class FactorCombination:
    name: str
    factor_names: tuple[str, ...]
    thresholds: dict[str, float]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Controlled factor-combination research for ETH trend sizing. "
            "Only uses registered, look-ahead-safe, currently backtestable factors."
        )
    )
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--initial-equity", type=float, default=10_000.0)
    parser.add_argument("--min-size", type=int, default=2)
    parser.add_argument("--max-size", type=int, default=4)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--max-candidates", type=int, default=10_000)
    return parser.parse_args()


def clip(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low
    if not math.isfinite(number):
        return low
    return max(low, min(high, number))


def tier_index(tier: str) -> int:
    return TIER_ORDER.index(tier)


def cap_tier(tier: str, cap: str) -> str:
    return TIER_ORDER[min(tier_index(tier), tier_index(cap))]


def score_to_tier(score: float, thresholds: dict[str, float]) -> str:
    if score >= thresholds["full"]:
        return "full"
    if score >= thresholds["strong"]:
        return "strong"
    if score >= thresholds["normal"]:
        return "normal"
    if score >= thresholds["weak"]:
        return "weak"
    return "block"


def load_features(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def feature_year(feature: dict[str, Any]) -> str:
    return str(feature["signal_time"])[:4]


def is_number(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)


def eligible_factor_specs(features: Iterable[dict[str, Any]]) -> list[FactorSpec]:
    rows = list(features)
    if not rows:
        return []
    sample_fields = set().union(*(row.keys() for row in rows))
    output: list[FactorSpec] = []
    for factor in optimization_eligible_factors():
        if factor.availability != FactorAvailability.BACKTESTABLE_NOW:
            continue
        if not factor.current_field or factor.current_field not in sample_fields:
            continue
        numeric_count = sum(1 for row in rows if is_number(row.get(factor.current_field)))
        if numeric_count < max(20, math.ceil(len(rows) * 0.5)):
            continue
        output.append(factor)
    return sorted(output, key=lambda item: (item.priority, item.category.value, item.name))


def normalized_factor_score(factor: FactorSpec, feature: dict[str, Any]) -> float:
    field = factor.current_field
    value = clip(feature.get(field), 0.0, 1.0) if field else 0.5

    if field == "volume_multiple":
        value = clip((float(feature.get(field) or 0.0) - 2.5) / 2.0)
    elif field == "breakout_atr":
        value = clip(float(feature.get(field) or 0.0) / 1.2)
    elif field == "atr_pct":
        value = 1.0 - clip(float(feature.get(field) or 0.0) / 0.03)
    elif field == "regime_range_score":
        value = 1.0 - clip(feature.get(field))
    elif field == "regime_risk_score":
        value = 1.0 - clip(feature.get(field))
    elif field == "dense_range_score":
        value = 1.0 - clip(feature.get(field))
    elif factor.role in {FactorRole.LOSS_SUPPRESSION, FactorRole.HARD_RISK_GATE} and "safety" not in factor.name:
        # Loss-suppression factors are encoded as risk when their raw direction says higher is bad.
        if "higher reduces" in factor.direction or "higher caps" in factor.direction or "too high" in factor.direction:
            value = 1.0 - clip(feature.get(field))

    return clip(value)


def structural_hard_block(feature: dict[str, Any]) -> str | None:
    if str(feature.get("dense_breakout_status") or "") == "failed_breakout":
        return "failed_breakout"
    if float(feature.get("regime_risk_score") or 0.0) >= 0.80:
        return "regime_risk_extreme"
    if (
        str(feature.get("htf_signal_alignment") or "") == "conflict"
        and float(feature.get("htf_trend_strength") or 0.0) >= 0.75
        and float(feature.get("regime_risk_score") or 0.0) >= 0.55
    ):
        return "htf_conflict"
    return None


def policy_from_combination(
    factor_map: dict[str, FactorSpec],
    combo: FactorCombination,
) -> Callable[[dict[str, Any]], str]:
    factors = [factor_map[name] for name in combo.factor_names]

    def policy(feature: dict[str, Any]) -> str:
        if structural_hard_block(feature):
            return "block"
        scores = [normalized_factor_score(factor, feature) for factor in factors]
        score = sum(scores) / max(len(scores), 1)
        tier = score_to_tier(score, combo.thresholds)
        if float(feature.get("regime_risk_score") or 0.0) >= 0.68:
            tier = cap_tier(tier, "weak")
        elif float(feature.get("regime_range_score") or 0.0) >= 0.70:
            tier = cap_tier(tier, "normal")
        if str(feature.get("regime_strategy_allowed") or "") not in {"trend", "unknown"}:
            tier = cap_tier(tier, "normal")
        return tier

    return policy


def max_drawdown(curve: list[float]) -> float:
    peak = curve[0] if curve else 0.0
    drawdown = 0.0
    for value in curve:
        peak = max(peak, value)
        drawdown = min(drawdown, (value - peak) / max(peak, 1e-9))
    return drawdown * 100


def evaluate_policy(
    features: list[dict[str, Any]],
    policy: Callable[[dict[str, Any]], str],
    initial_equity: float,
) -> dict[str, Any]:
    baseline_equity = initial_equity
    overlay_equity = initial_equity
    curve = [overlay_equity]
    tier_counts: Counter[str] = Counter()
    taken: list[float] = []
    for feature in sorted(features, key=lambda row: int(row["signal_idx"])):
        tier = policy(feature)
        scale = TIER_SCALE[tier]
        tier_counts[tier] += 1
        pnl_ratio = float(feature["pnl"]) / max(baseline_equity, 1e-9)
        baseline_equity += float(feature["pnl"])
        pnl = overlay_equity * pnl_ratio * scale
        overlay_equity += pnl
        curve.append(overlay_equity)
        if scale > 0:
            taken.append(pnl)
        if overlay_equity <= 0:
            return {
                "final_equity": overlay_equity,
                "return_pct": -100.0,
                "max_drawdown_pct": -100.0,
                "trades_taken": len(taken),
                "blocked": tier_counts["block"],
                "tier_counts": dict(tier_counts),
                "win_rate_pct": 0.0,
                "profit_factor": 0.0,
                "objective": -9999.0,
                "ruined": True,
            }
    wins = [pnl for pnl in taken if pnl > 0]
    losses = [pnl for pnl in taken if pnl <= 0]
    return_pct = (overlay_equity - initial_equity) / max(initial_equity, 1e-9) * 100
    drawdown = max_drawdown(curve)
    profit_factor = sum(wins) / abs(sum(losses)) if losses else (999.0 if wins else 0.0)
    coverage = len(taken) / max(len(features), 1)
    if len(taken) < max(5, math.ceil(len(features) * 0.25)):
        objective = -9999.0
    elif drawdown <= -70:
        objective = -9999.0 + return_pct / 100.0
    else:
        objective = (return_pct / max(abs(drawdown), 1.0)) * math.sqrt(max(coverage, 0.01))
        objective += min(profit_factor, 3.0) * 0.12
    return {
        "final_equity": overlay_equity,
        "return_pct": return_pct,
        "max_drawdown_pct": drawdown,
        "trades_taken": len(taken),
        "blocked": tier_counts["block"],
        "tier_counts": dict(tier_counts),
        "win_rate_pct": len(wins) / max(len(taken), 1) * 100,
        "profit_factor": profit_factor,
        "objective": objective,
        "ruined": False,
    }


def yearly_validation(
    features: list[dict[str, Any]],
    policy: Callable[[dict[str, Any]], str],
    initial_equity: float,
) -> dict[str, Any]:
    years = sorted({feature_year(row) for row in features})
    folds: list[dict[str, Any]] = []
    objectives: list[float] = []
    returns: list[float] = []
    drawdowns: list[float] = []
    for year in years:
        rows = [row for row in features if feature_year(row) == year]
        if len(rows) < 10:
            folds.append({"year": year, "status": "insufficient_sample", "rows": len(rows)})
            continue
        result = evaluate_policy(rows, policy, initial_equity)
        folds.append({"year": year, "status": "ok", "rows": len(rows), "result": result})
        objectives.append(float(result["objective"]))
        returns.append(float(result["return_pct"]))
        drawdowns.append(float(result["max_drawdown_pct"]))
    if not objectives:
        return {"status": "insufficient_sample", "folds": folds, "walk_forward_score": -9999.0}
    negative_years = sum(1 for value in returns if value <= 0)
    ruined_years = sum(1 for fold in folds if (fold.get("result") or {}).get("ruined"))
    worst_drawdown = min(drawdowns)
    score = (
        median(objectives)
        + min(objectives) * 0.35
        - negative_years * 0.75
        - ruined_years * 10_000
        - abs(worst_drawdown) / 100.0 * 0.25
    )
    return {
        "status": "ok",
        "folds": folds,
        "ok_folds": len(objectives),
        "negative_years": negative_years,
        "ruined_years": ruined_years,
        "median_year_return_pct": median(returns),
        "min_year_return_pct": min(returns),
        "worst_year_drawdown_pct": worst_drawdown,
        "walk_forward_score": score,
    }


def generate_combinations(
    factors: list[FactorSpec],
    *,
    min_size: int,
    max_size: int,
    max_candidates: int,
) -> list[FactorCombination]:
    ordered = sorted(factors, key=lambda item: (item.priority, item.category.value, item.name))
    combos: list[FactorCombination] = []
    for size in range(max(1, min_size), max_size + 1):
        for factor_set in itertools.combinations(ordered, size):
            roles = {factor.role for factor in factor_set}
            if FactorRole.PROFIT_EXPANSION not in roles and FactorRole.EXECUTION_QUALITY not in roles:
                continue
            if FactorRole.LOSS_SUPPRESSION not in roles and FactorRole.HARD_RISK_GATE not in roles:
                continue
            for thresholds in DEFAULT_THRESHOLDS:
                names = tuple(factor.name for factor in factor_set)
                combos.append(
                    FactorCombination(
                        name=f"combo_{len(names)}_" + "_".join(names),
                        factor_names=names,
                        thresholds=dict(thresholds),
                    )
                )
            if len(combos) >= max_candidates:
                return combos[:max_candidates]
    return combos


def overfit_flags(full: dict[str, Any], yearly: dict[str, Any], combo: FactorCombination) -> list[str]:
    flags: list[str] = []
    if len(combo.factor_names) > 4:
        flags.append("too_many_factors")
    if float(yearly.get("walk_forward_score") or -9999.0) <= -1000:
        flags.append("yearly_objective_broken")
    if yearly.get("negative_years", 0) > 1:
        flags.append("too_many_negative_years")
    if float(yearly.get("worst_year_drawdown_pct") or 0.0) <= -50:
        flags.append("year_drawdown_too_deep")
    if float(full.get("max_drawdown_pct") or 0.0) <= -60:
        flags.append("full_drawdown_too_deep")
    if float(full.get("profit_factor") or 0.0) < 1.15:
        flags.append("profit_factor_too_low")
    if float(full.get("return_pct") or 0.0) > max(float(yearly.get("median_year_return_pct") or 0.0), 1.0) * 20:
        flags.append("full_sample_dominated_by_few_years")
    return flags


def rank_candidates(
    features: list[dict[str, Any]],
    factors: list[FactorSpec],
    *,
    initial_equity: float,
    min_size: int,
    max_size: int,
    top: int,
    max_candidates: int,
) -> dict[str, Any]:
    factor_map = {factor.name: factor for factor in factors}
    combos = generate_combinations(factors, min_size=min_size, max_size=max_size, max_candidates=max_candidates)
    ranked: list[dict[str, Any]] = []
    for combo in combos:
        policy = policy_from_combination(factor_map, combo)
        full = evaluate_policy(features, policy, initial_equity)
        yearly = yearly_validation(features, policy, initial_equity)
        flags = overfit_flags(full, yearly, combo)
        complexity_penalty = len(combo.factor_names) * 0.05
        robust_score = float(yearly.get("walk_forward_score") or -9999.0) + float(full.get("objective") or -9999.0) * 0.15
        robust_score -= complexity_penalty + len(flags) * 0.75
        ranked.append(
            {
                "combination": asdict(combo),
                "full_sample": full,
                "yearly_validation": yearly,
                "overfit_flags": flags,
                "robust_score": robust_score,
            }
        )
    ranked.sort(
        key=lambda item: (
            len(item["overfit_flags"]),
            -float(item["robust_score"]),
            -float(item["full_sample"].get("profit_factor") or 0.0),
            float(item["full_sample"].get("max_drawdown_pct") or -999.0),
        )
    )
    return {
        "tested_candidates": len(combos),
        "eligible_factors": [
            {
                "name": factor.name,
                "field": factor.current_field,
                "category": factor.category.value,
                "role": factor.role.value,
                "priority": factor.priority,
            }
            for factor in factors
        ],
        "top_candidates": ranked[:top],
        "clean_candidates": [item for item in ranked if not item["overfit_flags"]][:top],
    }


def build_report(summary: dict[str, Any]) -> str:
    lines = [
        "# 2020-2026 因子组合受控寻优",
        "",
        "本报告只使用因子注册表中 `BACKTESTABLE_NOW` 且当前样本存在的字段；不使用新闻、BTC、盘口、funding/OI、链上或任何结果字段。",
        "",
        "## 样本",
        f"- 输入: `{summary['input']}`",
        f"- 数据源: `{summary['data_source']}`",
        f"- 交易数: `{summary['sample']['trades']}`",
        f"- 候选组合数: `{summary['tested_candidates']}`",
        "",
        "## 参与组合的因子",
    ]
    for factor in summary["eligible_factors"]:
        lines.append(
            f"- `{factor['name']}` -> `{factor['field']}` / `{factor['category']}` / `{factor['role']}`"
        )
    lines.extend(["", "## Top candidates"])
    for idx, item in enumerate(summary["top_candidates"][:10], start=1):
        combo = item["combination"]
        full = item["full_sample"]
        yearly = item["yearly_validation"]
        lines.append(
            f"{idx}. `{', '.join(combo['factor_names'])}` thresholds `{combo['thresholds']}` "
            f"robust `{item['robust_score']:.4f}` return `{full['return_pct']:.2f}%` "
            f"DD `{full['max_drawdown_pct']:.2f}%` PF `{full['profit_factor']:.3f}` "
            f"negative_years `{yearly.get('negative_years')}` flags `{item['overfit_flags']}`"
        )
    lines.extend(["", "## 结论"])
    lines.extend(
        [
            "- 这不是实盘切换建议，只是离线组合寻优结果。",
            "- 若 clean candidate 为空，说明当前可回测因子不足以形成稳健组合，不能用收益最高项替换实盘。",
            "- 下一步应对 clean candidates 做参数邻域稳定性、滚动训练/验证和 shadow ledger。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    payload = load_features(input_path)
    features = list(payload.get("features", []))
    factors = eligible_factor_specs(features)
    result = rank_candidates(
        features,
        factors,
        initial_equity=args.initial_equity,
        min_size=args.min_size,
        max_size=args.max_size,
        top=args.top,
        max_candidates=args.max_candidates,
    )
    summary = {
        "method": "controlled_factor_combination_search",
        "input": str(input_path),
        "data_source": payload.get("data_source"),
        "sample": payload.get("sample", {}),
        "constraints": {
            "min_size": args.min_size,
            "max_size": args.max_size,
            "max_candidates": args.max_candidates,
            "forbidden": [
                "FORBIDDEN_OUTCOME_LEAKAGE",
                "LIVE_ONLY_NEEDS_ARCHIVE without archive",
                "NEEDS_NEW_DATA_SOURCE without backfill",
            ],
        },
        **result,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    output_path.with_suffix(".md").write_text(build_report(summary), encoding="utf-8")
    print(json.dumps({"output": str(output_path), "tested_candidates": result["tested_candidates"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
