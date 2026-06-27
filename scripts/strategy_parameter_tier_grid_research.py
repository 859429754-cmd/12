from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any, Callable

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_quant_trader.core.config import load_config
from ai_quant_trader.core.models import TrendStrategyConfig
from ai_quant_trader.data.market import MarketDataClient
from scripts.ai_tier_weight_research import (
    DEFAULT_WALK_FORWARD_PERIODS,
    TIER_SCALE,
    ResearchRow,
    apply_research_caps,
    clip,
    current_factor_policy,
    empirical_percentile,
    evaluate_policy,
    evaluate_walk_forward,
    load_json,
    score_to_tier,
    weighted_policy,
)
from scripts.deepseek_overlay_eval import run_baseline
from scripts.pure_strategy_tier_research import (
    SYMBOL,
    TIMEFRAME,
    TradeFeature,
    extract_trade_features,
)


DEFAULT_OUTPUT = Path("data/research/strategy_parameter_tier_grid_eth_2022_2026.json")
DEFAULT_REPORT = Path("docs/research/2026-06-27-strategy-parameter-tier-grid.md")
DEFAULT_ORDERFLOW = Path("data/research/historical_orderflow_proxy_eth_2022_2026.json")
DEFAULT_TIER_RESEARCH = Path("data/research/ai_tier_weight_research_eth_2022_2026.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Research ETH 1h trend strategy parameters together with the current three AI five-tier sizing policies. "
            "This script does not call DeepSeek and does not modify live configuration."
        )
    )
    parser.add_argument("--start", default="2022-01-01")
    parser.add_argument("--end", default="2026-06-26")
    parser.add_argument("--source", default="binance")
    parser.add_argument("--split", default="2024-01-01")
    parser.add_argument("--initial-equity", type=float, default=10_000.0)
    parser.add_argument("--leverage", type=float, default=4.0)
    parser.add_argument("--fee-rate", type=float, default=0.0006)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--embargo-days", type=int, default=7)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--orderflow", default=str(DEFAULT_ORDERFLOW))
    parser.add_argument("--tier-research", default=str(DEFAULT_TIER_RESEARCH))
    parser.add_argument("--max-combos", type=int, default=0, help="Limit grid size for smoke runs; 0 means all combos.")
    parser.add_argument("--preset", choices=["near", "coarse"], default="coarse")
    parser.add_argument("--kc-lengths", default="", help="Comma-separated KC lengths. Default depends on preset.")
    parser.add_argument("--kc-scalars", default="", help="Comma-separated KC ATR multipliers. Default depends on preset.")
    parser.add_argument("--volume-multiples", default="", help="Comma-separated volume multiples. Default depends on preset.")
    parser.add_argument("--atr-stop-multiples", default="", help="Comma-separated ATR stop multiples. Default depends on preset.")
    return parser.parse_args()


def parse_int_grid(value: str, fallback: list[int]) -> list[int]:
    if not value.strip():
        return fallback
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def parse_float_grid(value: str, fallback: list[float]) -> list[float]:
    if not value.strip():
        return fallback
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def strategy_grid(
    base: TrendStrategyConfig,
    *,
    kc_lengths: list[int] | None = None,
    kc_scalars: list[float] | None = None,
    volume_multiples: list[float] | None = None,
    atr_stop_multiples: list[float] | None = None,
) -> list[dict[str, Any]]:
    kc_lengths = sorted(set(kc_lengths or [18, 20, 22, int(base.kc_length)]))
    kc_scalars = sorted(set(kc_scalars or [2.6, 2.8, 3.0, float(base.kc_scalar)]))
    volume_multiples = sorted(set(volume_multiples or [2.3, 2.5, 2.7, float(base.volume_multiple)]))
    atr_stop_multiples = sorted(set(atr_stop_multiples or [1.2, 1.5, 1.8, float(base.atr_stop_multiple)]))
    output: list[dict[str, Any]] = []
    for kc_length in kc_lengths:
        for kc_scalar in kc_scalars:
            for volume_multiple in volume_multiples:
                for atr_stop_multiple in atr_stop_multiples:
                    output.append(
                        {
                            "kc_length": kc_length,
                            "kc_scalar": kc_scalar,
                            "vma_length": base.vma_length,
                            "atr_length": base.atr_length,
                            "volume_multiple": volume_multiple,
                            "atr_stop_multiple": atr_stop_multiple,
                            "momentum_filter": "kdj",
                            "kdj_length": base.kdj_length,
                            "kdj_k_smooth": base.kdj_k_smooth,
                            "kdj_d_smooth": base.kdj_d_smooth,
                            "position_fraction": base.position_fraction,
                            "variant": "with_volume",
                            "use_volume_filter": True,
                        }
                    )
    return output


def config_from_params(base: TrendStrategyConfig, params: dict[str, Any]) -> TrendStrategyConfig:
    return base.model_copy(update=params)


def build_orderflow_scores(
    features: list[TradeFeature],
    orderflow_payload: dict[str, Any],
    split: str,
    window: str = "60",
) -> tuple[dict[int, dict[str, float]], dict[str, Any]]:
    rows = orderflow_payload.get("rows", {}).get(window, [])
    orderflow_by_idx = {int(row["signal_idx"]): row for row in rows if "signal_idx" in row}
    fields = ["trade_count", "total_quote", "large_trade_quote", "max_trade_quote"]
    train_orderflow_rows = [
        orderflow_by_idx[item.signal_idx]
        for item in features
        if item.signal_time[:10] < split and item.signal_idx in orderflow_by_idx
    ]
    train_values = {field: [float(row.get(field) or 0.0) for row in train_orderflow_rows] for field in fields}
    scores: dict[int, dict[str, float]] = {}
    covered = 0
    for item in features:
        row = orderflow_by_idx.get(item.signal_idx)
        if not row:
            scores[item.signal_idx] = {
                "orderflow_confirmation_score": 0.5,
                "orderflow_direction_score": 0.0,
                "orderflow_missing": 1.0,
            }
            continue
        covered += 1
        percentiles = [
            empirical_percentile(train_values[field], float(row.get(field) or 0.0)) if train_values[field] else 0.5
            for field in fields
        ]
        directional_cvd = clip(float(row.get("directional_cvd_quote_ratio") or 0.0) / 0.35)
        directional_large = clip(float(row.get("directional_large_trade_ratio") or 0.0) / 0.35)
        scores[item.signal_idx] = {
            "orderflow_confirmation_score": sum(percentiles) / len(percentiles),
            "orderflow_direction_score": (directional_cvd + directional_large) / 2,
            "orderflow_missing": 0.0,
        }
    return scores, {
        "covered": covered,
        "total": len(features),
        "coverage_pct": covered / max(len(features), 1) * 100,
        "window_minutes": int(window),
    }


def rows_from_trade_features(
    features: list[TradeFeature],
    orderflow_payload: dict[str, Any],
    split: str,
    initial_equity: float,
) -> tuple[list[ResearchRow], dict[str, Any]]:
    orderflow_scores, coverage = build_orderflow_scores(features, orderflow_payload, split)
    rows: list[ResearchRow] = []
    baseline_equity = float(initial_equity)
    for item in sorted(features, key=lambda feature: feature.signal_idx):
        of_scores = orderflow_scores.get(item.signal_idx, {})
        raw = asdict(item)
        scores = {
            "technical_signal_score": clip(float(item.signal_strength)),
            "breakout_score": clip(float(item.breakout_atr) / 1.2),
            "volume_score": clip((float(item.volume_multiple) - 2.5) / 2.0),
            "orderflow_confirmation_score": clip(of_scores.get("orderflow_confirmation_score", 0.5)),
            "orderflow_direction_score": clip(of_scores.get("orderflow_direction_score", 0.0)),
            "news_direction_alignment_score": 0.0,
            "pattern_confirmation_score": clip(float(item.pattern_aligned_score)),
            "range_safety_score": 1.0 - clip(float(item.regime_range_score)),
            "regime_risk_safety_score": 1.0 - clip(float(item.regime_risk_score)),
            "trend_confirmation_score": clip(float(item.regime_trend_score)),
            "dense_zone_breakout_score": clip(float(item.dense_trend_score)),
            "news_safety_score": 0.5,
            "btc_leader_score": 0.5,
            "eth_btc_rotation_score": 0.5,
            "htf_alignment_score": clip(float(item.htf_alignment_score)),
        }
        rows.append(
            ResearchRow(
                signal_idx=int(item.signal_idx),
                signal_time=str(item.signal_time),
                entry_time=str(item.entry_time),
                side=str(item.side),
                pnl=float(item.pnl),
                baseline_equity_before=baseline_equity,
                scores=scores,
                raw={
                    **raw,
                    "dense_breakout_status": item.dense_breakout_status,
                    "regime_risk_score": item.regime_risk_score,
                    "regime_range_score": item.regime_range_score,
                    "regime_trend_score": item.regime_trend_score,
                    "htf_signal_alignment": item.htf_signal_alignment,
                    "htf_trend_strength": item.htf_trend_strength,
                    "regime_strategy_allowed": item.regime_strategy_allowed,
                    "regime_breakout_quality": item.regime_breakout_quality,
                    "orderflow_missing": of_scores.get("orderflow_missing", 1.0),
                },
            )
        )
        baseline_equity += float(item.pnl)
    return rows, coverage


def build_policy_set(tier_research: dict[str, Any]) -> dict[str, Callable[[ResearchRow], str]]:
    rolling = tier_research["walk_forward_candidates"][0]
    single = tier_research["optimized_candidates"][0]

    def pure_full(_row: ResearchRow) -> str:
        return "full"

    def barbell_aggressive_guarded(row: ResearchRow) -> str:
        base_score = (
            row.scores["technical_signal_score"] * 0.14
            + row.scores["orderflow_confirmation_score"] * 0.27
            + row.scores["pattern_confirmation_score"] * 0.13
            + row.scores["trend_confirmation_score"] * 0.10
            + row.scores["dense_zone_breakout_score"] * 0.10
            + row.scores["range_safety_score"] * 0.12
            + row.scores["htf_alignment_score"] * 0.07
            + row.scores["news_safety_score"] * 0.04
            + row.scores["btc_leader_score"] * 0.02
            + row.scores["eth_btc_rotation_score"] * 0.01
        )
        tier = score_to_tier(base_score, {"full": 0.78, "strong": 0.70, "normal": 0.60, "weak": 0.50})
        if row.scores["orderflow_confirmation_score"] >= 0.75 and row.scores["pattern_confirmation_score"] >= 0.62:
            if tier == "strong":
                tier = "full"
            elif tier == "normal":
                tier = "strong"
        if row.scores["range_safety_score"] < 0.35 or row.scores["orderflow_confirmation_score"] < 0.28:
            tier = "weak" if tier in {"normal", "strong", "full"} else tier
        return apply_research_caps(row, tier)

    return {
        "pure_strategy_full_size": pure_full,
        "current_conservative": current_factor_policy,
        "rolling_walk_forward_candidate": weighted_policy(rolling["weights"], rolling["thresholds"]),
        "single_split_high_return_candidate": weighted_policy(single["weights"], single["thresholds"]),
        "barbell_aggressive_guarded_v1": barbell_aggressive_guarded,
    }


def rows_for_period(rows: list[ResearchRow], start: str, end: str) -> list[ResearchRow]:
    start_day = pd.Timestamp(start, tz="UTC")
    end_day = pd.Timestamp(end, tz="UTC")
    output = []
    for row in rows:
        ts = pd.Timestamp(row.signal_time)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        if start_day <= ts < end_day:
            output.append(row)
    return output


def annual_results(rows: list[ResearchRow], policy: Callable[[ResearchRow], str], initial_equity: float) -> dict[str, Any]:
    output: dict[str, Any] = {}
    years = sorted({str(pd.Timestamp(row.signal_time).year) for row in rows})
    for year in years:
        period_rows = [row for row in rows if str(pd.Timestamp(row.signal_time).year) == year]
        if period_rows:
            output[year] = compact_eval(evaluate_policy(period_rows, policy, initial_equity))
    return output


def compact_eval(result: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "return_pct",
        "max_drawdown_pct",
        "trades_taken",
        "blocked",
        "win_rate_pct",
        "profit_factor",
        "avg_trade_return_pct",
        "trade_sharpe",
        "tier_counts",
        "ruined",
        "objective",
    ]
    return {key: result.get(key) for key in keys if key in result}


def overfit_flags(
    *,
    full: dict[str, Any],
    train: dict[str, Any],
    validation: dict[str, Any],
    walk_forward: dict[str, Any],
    pure_full: dict[str, Any],
    coverage: dict[str, Any],
    min_trades: int,
) -> list[str]:
    flags: list[str] = []
    validation_return = float(validation.get("return_pct") or 0.0)
    train_return = float(train.get("return_pct") or 0.0)
    full_return = float(full.get("return_pct") or 0.0)
    if int(full.get("trades_taken") or 0) < min_trades:
        flags.append("trade_count_too_low")
    if validation_return <= 0:
        flags.append("validation_return_non_positive")
    if train_return > 0 and validation_return < train_return * 0.35:
        flags.append("train_validation_gap_large")
    if int(walk_forward.get("negative_folds") or 0) > 2:
        flags.append("too_many_negative_walk_forward_folds")
    if float(walk_forward.get("worst_validation_drawdown_pct") or 0.0) <= -50:
        flags.append("walk_forward_drawdown_too_deep")
    if float(coverage.get("coverage_pct") or 0.0) < 70:
        flags.append("orderflow_proxy_coverage_low")
    if full_return > float(pure_full.get("return_pct") or 0.0) * 2.5 and validation_return < 25:
        flags.append("paper_return_spike_not_supported_by_validation")
    return flags


def evaluate_combo(
    *,
    candles: pd.DataFrame,
    params: dict[str, Any],
    base_config: TrendStrategyConfig,
    policies: dict[str, Callable[[ResearchRow], str]],
    orderflow_payload: dict[str, Any],
    split: str,
    initial_equity: float,
    fee_rate: float,
    slippage_bps: float,
    leverage: float,
    embargo_days: int,
) -> dict[str, Any]:
    config = config_from_params(base_config, params)
    baseline, evaluated, df = run_baseline(
        candles=candles,
        config=config,
        split=split,
        initial_equity=initial_equity,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
        leverage=leverage,
    )
    features = extract_trade_features(
        df,
        evaluated,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        initial_equity=initial_equity,
        leverage=leverage,
        trend_config=config,
    )
    rows, coverage = rows_from_trade_features(features, orderflow_payload, split, initial_equity)
    train_rows = [row for row in rows if row.signal_time[:10] < split]
    validation_rows = [row for row in rows if row.signal_time[:10] >= split]
    policy_results: dict[str, Any] = {}
    pure_full = evaluate_policy(rows, policies["pure_strategy_full_size"], initial_equity)
    for name, policy in policies.items():
        full = evaluate_policy(rows, policy, initial_equity)
        train = evaluate_policy(train_rows, policy, initial_equity) if train_rows else {}
        validation = evaluate_policy(validation_rows, policy, initial_equity) if validation_rows else {}
        walk_forward = evaluate_walk_forward(
            rows,
            policy,
            periods=DEFAULT_WALK_FORWARD_PERIODS,
            initial_equity=initial_equity,
            embargo_days=embargo_days,
        )
        flags = overfit_flags(
            full=full,
            train=train,
            validation=validation,
            walk_forward=walk_forward,
            pure_full=pure_full,
            coverage=coverage,
            min_trades=180,
        )
        beats_pure = (
            name != "pure_strategy_full_size"
            and float(full.get("return_pct") or 0.0) > float(pure_full.get("return_pct") or 0.0)
            and float(full.get("max_drawdown_pct") or 0.0) > float(pure_full.get("max_drawdown_pct") or 0.0)
            and float(walk_forward.get("walk_forward_score") or -9999.0)
            >= float(
                evaluate_walk_forward(
                    rows,
                    policies["pure_strategy_full_size"],
                    periods=DEFAULT_WALK_FORWARD_PERIODS,
                    initial_equity=initial_equity,
                    embargo_days=embargo_days,
                ).get("walk_forward_score")
                or -9999.0
            )
        )
        robust_score = (
            float(full.get("return_pct") or 0.0) / max(abs(float(full.get("max_drawdown_pct") or 0.0)), 1.0)
            + float(walk_forward.get("walk_forward_score") or 0.0)
            + min(float(validation.get("return_pct") or 0.0), 200.0) / 20.0
            - int(walk_forward.get("negative_folds") or 0) * 2.0
            - len(flags) * 5.0
        )
        policy_results[name] = {
            "full": compact_eval(full),
            "train": compact_eval(train),
            "validation": compact_eval(validation),
            "walk_forward": {
                key: walk_forward.get(key)
                for key in [
                    "walk_forward_score",
                    "ok_folds",
                    "negative_folds",
                    "ruined_folds",
                    "median_validation_return_pct",
                    "min_validation_return_pct",
                    "worst_validation_drawdown_pct",
                    "total_validation_trades",
                ]
            },
            "overfit_flags": flags,
            "beats_pure": beats_pure,
            "robust_score": robust_score,
            "annual": annual_results(rows, policy, initial_equity),
        }
    return {
        "params": params,
        "trade_count": len(rows),
        "baseline": {
            key: baseline.get(key)
            for key in ["total_return_pct", "max_drawdown_pct", "trade_count", "win_rate_pct", "profit_factor"]
        },
        "orderflow_coverage": coverage,
        "policy_results": policy_results,
    }


def neighborhood_stability(results: list[dict[str, Any]], policy_name: str) -> dict[str, dict[str, Any]]:
    key_fields = ["kc_length", "kc_scalar", "volume_multiple", "atr_stop_multiple"]
    index_values = {
        field: sorted({item["params"][field] for item in results})
        for field in key_fields
    }

    def distance(a: dict[str, Any], b: dict[str, Any]) -> int:
        total = 0
        for field in key_fields:
            values = index_values[field]
            total += abs(values.index(a[field]) - values.index(b[field]))
        return total

    output: dict[str, dict[str, Any]] = {}
    for item in results:
        params = item["params"]
        neighbors = [
            other
            for other in results
            if other is not item and distance(params, other["params"]) <= 1
        ]
        neighbor_scores = [
            float(other["policy_results"][policy_name]["robust_score"])
            for other in neighbors
            if policy_name in other["policy_results"]
        ]
        key = params_key(params)
        output[key] = {
            "neighbors": len(neighbor_scores),
            "median_neighbor_robust_score": median(neighbor_scores) if neighbor_scores else None,
            "best_neighbor_robust_score": max(neighbor_scores) if neighbor_scores else None,
        }
    return output


def params_key(params: dict[str, Any]) -> str:
    return (
        f"kc{params['kc_length']}_x{params['kc_scalar']}"
        f"_vol{params['volume_multiple']}_atrStop{params['atr_stop_multiple']}"
    )


def rank_candidates(results: list[dict[str, Any]], policy_names: list[str]) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    stabilities = {policy: neighborhood_stability(results, policy) for policy in policy_names}
    for combo in results:
        for policy in policy_names:
            result = combo["policy_results"][policy]
            stability = stabilities[policy].get(params_key(combo["params"]), {})
            flags = list(result["overfit_flags"])
            median_neighbor = stability.get("median_neighbor_robust_score")
            if median_neighbor is not None and float(result["robust_score"]) > max(float(median_neighbor) * 1.8, float(median_neighbor) + 25):
                flags.append("neighborhood_instability")
            ranked.append(
                {
                    "policy": policy,
                    "params": combo["params"],
                    "trade_count": combo["trade_count"],
                    "orderflow_coverage": combo["orderflow_coverage"],
                    "full": result["full"],
                    "validation": result["validation"],
                    "walk_forward": result["walk_forward"],
                    "beats_pure": result["beats_pure"],
                    "overfit_flags": flags,
                    "robust_score": result["robust_score"] - (5.0 if "neighborhood_instability" in flags else 0.0),
                    "neighborhood": stability,
                }
            )
    ranked.sort(
        key=lambda item: (
            item["beats_pure"],
            -len(item["overfit_flags"]),
            float(item["robust_score"]),
            float(item["full"].get("return_pct") or 0.0),
        ),
        reverse=True,
    )
    return ranked


def pf_sharpe_score(item: dict[str, Any]) -> float:
    full = item["full"]
    profit_factor = min(float(full.get("profit_factor") or 0.0), 5.0)
    sharpe = max(min(float(full.get("trade_sharpe") or 0.0), 10.0), -10.0)
    drawdown_penalty = abs(float(full.get("max_drawdown_pct") or 0.0)) / 25.0
    weak_walk_forward_penalty = max(0.0, -float(item["walk_forward"].get("walk_forward_score") or 0.0)) / 2.0
    return profit_factor + sharpe - drawdown_penalty - weak_walk_forward_penalty


def build_report(summary: dict[str, Any]) -> str:
    grid_params = summary.get("grid_params") or {}
    kc_lengths = " / ".join(str(item) for item in grid_params.get("kc_lengths", [])) or "(unknown)"
    kc_scalars = " / ".join(str(item) for item in grid_params.get("kc_scalars", [])) or "(unknown)"
    volume_multiples = " / ".join(str(item) for item in grid_params.get("volume_multiples", [])) or "(unknown)"
    atr_stop_multiples = " / ".join(str(item) for item in grid_params.get("atr_stop_multiples", [])) or "(unknown)"

    def candidate_line(idx: int, item: dict[str, Any]) -> str:
        params = item["params"]
        return (
            f"{idx}. `{item['policy']}` params `{params}` "
            f"return `{float(item['full'].get('return_pct') or 0.0):.2f}%`, "
            f"DD `{float(item['full'].get('max_drawdown_pct') or 0.0):.2f}%`, "
            f"PF `{float(item['full'].get('profit_factor') or 0.0):.2f}`, "
            f"Sharpe `{float(item['full'].get('trade_sharpe') or 0.0):.2f}`, "
            f"WF `{float(item['walk_forward'].get('walk_forward_score') or 0.0):.4f}`, "
            f"negative_folds `{item['walk_forward'].get('negative_folds')}`, "
            f"beats_pure `{item['beats_pure']}`, flags `{item['overfit_flags']}`"
        )

    lines = [
        "# ETH 1h 策略参数 x AI 五档组合研究",
        "",
        "以后以本文为本轮参数研究记录准绳：本研究只做离线实验，不调用 DeepSeek，不修改云端实盘参数。",
        "",
        "## 研究目标",
        "",
        "- 同时搜索趋势策略核心参数和 AI 五档仓位逻辑。",
        "- 检查 AI 五档是否能在收益、回撤、rolling 稳定性上超过纯策略。",
        "- 将过拟合排查作为硬过滤，不采用全样本收益最高但分段不稳的候选。",
        "",
        "## 数据与口径",
        f"- 时间: `{summary['params']['start']}` 到 `{summary['params']['end']}`",
        f"- 数据源: `{summary['params']['source']}` / 实际 `{summary['data_source']}`",
        f"- 初始权益: `{summary['params']['initial_equity']}`",
        f"- 杠杆: `{summary['params']['leverage']}`",
        f"- 手续费: `{summary['params']['fee_rate']}`",
        f"- 滑点: `{summary['params']['slippage_bps']} bps`",
        f"- 搜索组合数: `{summary['searched_combos']}`",
        "",
        "## 搜索参数",
        "",
        "```text",
        f"kc_length: {kc_lengths}",
        f"kc_scalar: {kc_scalars}",
        f"volume_multiple: {volume_multiples}",
        f"atr_stop_multiple: {atr_stop_multiples}",
        "固定: vma_length=20, atr_length=14, KDJ(9,3,3), position_fraction=当前配置",
        "```",
        "",
        "## 过拟合排查规则",
        "",
        "- 训练/验证收益差距过大 -> `train_validation_gap_large`",
        "- 验证集收益非正 -> `validation_return_non_positive`",
        "- rolling 负收益窗口超过 2 个 -> `too_many_negative_walk_forward_folds`",
        "- rolling 最差验证回撤深于 -50% -> `walk_forward_drawdown_too_deep`",
        "- 邻近参数表现明显断崖 -> `neighborhood_instability`",
        "- 订单流 proxy 覆盖率过低 -> `orderflow_proxy_coverage_low`",
        "",
        "## 综合稳健榜",
        "",
    ]
    for idx, item in enumerate(summary["top_candidates"][:12], start=1):
        lines.append(candidate_line(idx, item))

    if summary.get("top_return_candidates"):
        lines.extend(["", "## 无过拟合收益榜", ""])
        for idx, item in enumerate(summary["top_return_candidates"][:10], start=1):
            lines.append(candidate_line(idx, item))

    if summary.get("top_walk_forward_candidates"):
        lines.extend(["", "## Walk-forward 稳定榜", ""])
        for idx, item in enumerate(summary["top_walk_forward_candidates"][:10], start=1):
            lines.append(candidate_line(idx, item))

    if summary.get("top_pf_sharpe_candidates"):
        lines.extend(
            [
                "",
                "## 盈利因子 + 夏普筛选榜",
                "",
                "此榜只在无过拟合旗标候选中排序；盈利因子与 trade-level annualized Sharpe 不能替代最大回撤和 walk-forward 硬闸。",
                "",
            ]
        )
        for idx, item in enumerate(summary["top_pf_sharpe_candidates"][:10], start=1):
            lines.append(candidate_line(idx, item))

    if summary.get("top_neighborhood_watchlist"):
        lines.extend(
            [
                "",
                "## 邻域不稳观察榜",
                "",
                "这些候选只因 `neighborhood_instability` 被排除出干净候选；它们可以进入 shadow ledger，但不能直接切实盘。",
                "",
            ]
        )
        for idx, item in enumerate(summary["top_neighborhood_watchlist"][:10], start=1):
            lines.append(candidate_line(idx, item))

    lines.extend(["", "## 结论"])
    lines.extend(f"- {line}" for line in summary["conclusions"])
    return "\n".join(lines) + "\n"


async def main() -> None:
    args = parse_args()
    app_config = load_config()
    base_config = app_config.strategy.trend
    tier_research = load_json(Path(args.tier_research))
    orderflow_payload = load_json(Path(args.orderflow))
    market = MarketDataClient(app_config)
    try:
        candles = await market.fetch_ohlcv_history(
            SYMBOL,
            TIMEFRAME,
            start=args.start,
            end=args.end,
            source=args.source,
            max_candles=60_000,
        )
    finally:
        await market.close()
    data_source = str(candles.attrs.get("data_source") or "unknown")
    if data_source == "synthetic":
        raise RuntimeError("真实K线不可用，拒绝用 synthetic 数据做参数与五档研究。")

    policies = build_policy_set(tier_research)
    if args.preset == "near":
        default_kc_lengths = [18, 20, 22, int(base_config.kc_length)]
        default_kc_scalars = [2.6, 2.8, 3.0, float(base_config.kc_scalar)]
        default_volume_multiples = [2.3, 2.5, 2.7, float(base_config.volume_multiple)]
        default_atr_stop_multiples = [1.2, 1.5, 1.8, float(base_config.atr_stop_multiple)]
    else:
        default_kc_lengths = [16, 20, 24, 28, int(base_config.kc_length)]
        default_kc_scalars = [2.2, 2.6, 2.8, 3.0, 3.4, float(base_config.kc_scalar)]
        default_volume_multiples = [2.0, 2.3, 2.5, 2.8, 3.2, float(base_config.volume_multiple)]
        default_atr_stop_multiples = [1.0, 1.3, 1.5, 1.8, 2.4, float(base_config.atr_stop_multiple)]
    grid_kc_lengths = parse_int_grid(args.kc_lengths, default_kc_lengths)
    grid_kc_scalars = parse_float_grid(args.kc_scalars, default_kc_scalars)
    grid_volume_multiples = parse_float_grid(args.volume_multiples, default_volume_multiples)
    grid_atr_stop_multiples = parse_float_grid(args.atr_stop_multiples, default_atr_stop_multiples)
    grid = strategy_grid(
        base_config,
        kc_lengths=grid_kc_lengths,
        kc_scalars=grid_kc_scalars,
        volume_multiples=grid_volume_multiples,
        atr_stop_multiples=grid_atr_stop_multiples,
    )
    if args.max_combos > 0:
        grid = grid[: args.max_combos]
    results: list[dict[str, Any]] = []
    for index, params in enumerate(grid, start=1):
        print(f"[strategy_parameter_tier_grid] combo {index}/{len(grid)} {params}", file=sys.stderr)
        results.append(
            evaluate_combo(
                candles=candles,
                params=params,
                base_config=base_config,
                policies=policies,
                orderflow_payload=orderflow_payload,
                split=args.split,
                initial_equity=args.initial_equity,
                fee_rate=args.fee_rate,
                slippage_bps=args.slippage_bps,
                leverage=args.leverage,
                embargo_days=args.embargo_days,
            )
        )
    policy_names = [name for name in policies if name != "pure_strategy_full_size"]
    ranked = rank_candidates(results, policy_names)
    top_clean = [item for item in ranked if item["beats_pure"] and not item["overfit_flags"]]
    top_return = sorted(
        top_clean,
        key=lambda item: float(item["full"].get("return_pct") or 0.0),
        reverse=True,
    )
    top_walk_forward = sorted(
        top_clean,
        key=lambda item: (
            float(item["walk_forward"].get("walk_forward_score") or -9999.0),
            float(item["full"].get("return_pct") or 0.0),
        ),
        reverse=True,
    )
    top_pf_sharpe = sorted(
        [
            item
            for item in top_clean
            if float(item["full"].get("profit_factor") or 0.0) >= 1.15
            and float(item["full"].get("trade_sharpe") or 0.0) > 0.0
        ],
        key=pf_sharpe_score,
        reverse=True,
    )
    neighborhood_watchlist = sorted(
        [
            item
            for item in ranked
            if item["beats_pure"] and item["overfit_flags"] == ["neighborhood_instability"]
        ],
        key=lambda item: float(item["robust_score"]),
        reverse=True,
    )
    conclusions = [
        "如果没有 beats_pure 且无过拟合旗标的候选，本轮不能给出替换实盘参数建议。",
        "历史新闻方向归档不完整，新闻方向分仍只允许做实时确认和风险 cap，不能在本研究中 hindsight 寻优。",
        "订单流 proxy 覆盖率会影响高订单流权重策略的可信度；覆盖不足时不能把结果视为完整实盘模拟。",
        "任何候选最多进入 shadow ledger / 小仓灰度，不允许直接大资金切换。",
    ]
    if top_clean:
        best = top_clean[0]
        conclusions.insert(
            0,
            f"本轮最干净候选为 {best['policy']} + {best['params']}；仍需 shadow ledger 复核后才可灰度。",
        )
    else:
        conclusions.insert(0, "本轮未找到同时超过纯策略且无过拟合旗标的干净候选。")

    summary = {
        "created_at": datetime.now(UTC).isoformat(),
        "params": {
            "start": args.start,
            "end": args.end,
            "source": args.source,
            "split": args.split,
            "initial_equity": args.initial_equity,
            "leverage": args.leverage,
            "fee_rate": args.fee_rate,
            "slippage_bps": args.slippage_bps,
            "base_trend_config": base_config.model_dump(mode="json"),
        },
        "data_source": data_source,
        "grid_params": {
            "preset": args.preset,
            "kc_lengths": grid_kc_lengths,
            "kc_scalars": grid_kc_scalars,
            "volume_multiples": grid_volume_multiples,
            "atr_stop_multiples": grid_atr_stop_multiples,
        },
        "searched_combos": len(results),
        "policies": list(policies.keys()),
        "results": results,
        "top_candidates": ranked[:30],
        "top_clean_candidates": top_clean[:10],
        "top_return_candidates": top_return[:10],
        "top_walk_forward_candidates": top_walk_forward[:10],
        "top_pf_sharpe_candidates": top_pf_sharpe[:10],
        "top_neighborhood_watchlist": neighborhood_watchlist[:10],
        "conclusions": conclusions,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(build_report(summary), encoding="utf-8")
    print(json.dumps({"output": str(output_path), "report": str(report_path), "top_clean": len(top_clean)}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
