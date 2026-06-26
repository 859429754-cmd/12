from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean, median
from typing import Any, Callable

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_quant_trader.core.config import load_config
from ai_quant_trader.core.models import PositionSnapshot, Side, SignalAction
from ai_quant_trader.data.market import MarketDataClient
from ai_quant_trader.features.dense_zone import DenseZoneAnalyzer
from ai_quant_trader.features.patterns import PatternDetector
from ai_quant_trader.features.regime import RegimePatternAnalyzer
from ai_quant_trader.strategy.trend import TrendStrategy
from scripts.deepseek_overlay_eval import higher_timeframe_context, run_baseline


SYMBOL = "ETH/USDT:USDT"
TIMEFRAME = "1h"
TIER_SCALE = {"block": 0.0, "weak": 0.25, "normal": 0.5, "strong": 0.75, "full": 1.0}
NUMERIC_FEATURES = [
    "signal_strength",
    "breakout_atr",
    "volume_multiple",
    "atr_pct",
    "pattern_confidence",
    "pattern_aligned_score",
    "dense_trend_score",
    "dense_range_score",
    "dense_strength",
    "regime_trend_score",
    "regime_range_score",
    "regime_risk_score",
    "htf_alignment_score",
    "htf_trend_strength",
    "entry_quality_score",
]
CATEGORICAL_FEATURES = [
    "side",
    "exit_reason",
    "pattern_type",
    "pattern_family",
    "dense_position",
    "dense_breakout_status",
    "regime_candidate",
    "regime_strategy_allowed",
    "regime_breakout_quality",
    "htf_signal_alignment",
]

FACTOR_GROUPS: dict[str, dict[str, Any]] = {
    "core_strategy_trigger": {
        "label": "本地策略触发因子",
        "role": "决定是否产生 LONG/SHORT 候选信号，AI 不能绕过这一层发明方向。",
        "numeric": ["signal_strength", "breakout_atr", "volume_multiple", "atr_pct"],
        "categorical": ["side", "exit_reason"],
    },
    "pattern_structure": {
        "label": "形态确认因子",
        "role": "评估突破是否有结构支撑，避免孤立 K 线突破。",
        "numeric": ["pattern_confidence", "pattern_aligned_score"],
        "categorical": ["pattern_type", "pattern_family"],
    },
    "dense_zone_breakout": {
        "label": "密集区与突破质量因子",
        "role": "判断价格处在价值区内部、突破区、真空区还是失败突破区。",
        "numeric": ["dense_trend_score", "dense_range_score", "dense_strength"],
        "categorical": ["dense_position", "dense_breakout_status"],
    },
    "regime_filter": {
        "label": "趋势/震荡状态因子",
        "role": "识别趋势策略是否处于不适合交易的震荡或假突破环境。",
        "numeric": ["regime_trend_score", "regime_range_score", "regime_risk_score"],
        "categorical": ["regime_candidate", "regime_strategy_allowed", "regime_breakout_quality"],
    },
    "higher_timeframe": {
        "label": "4h 高周期结构因子",
        "role": "确认 1h 信号是否顺着更高周期结构；EMA89 已删除，当前只使用 KC 中轨和 ATR 结构。",
        "numeric": ["htf_alignment_score", "htf_trend_strength"],
        "categorical": ["htf_signal_alignment"],
    },
    "entry_quality_composite": {
        "label": "入场质量综合分",
        "role": "把策略强度、形态、密集区、状态和高周期合成仓位分档候选分。",
        "numeric": ["entry_quality_score"],
        "categorical": [],
    },
    "live_news_orderflow": {
        "label": "新闻与订单流实时因子",
        "role": "只在实盘时交给 DeepSeek/RiskManager；当前离线样本没有完整历史归档，不能统计证明。",
        "numeric": [],
        "categorical": [],
        "live_only": [
            "news_alignment",
            "news_risk_score",
            "orderflow_confirmation_score",
            "btc_market_impact",
            "eth_specific_impact",
        ],
    },
}

DEEPSEEK_SCORE_TO_FACTOR_GROUPS: dict[str, list[str]] = {
    "trend_confirmation_score": ["core_strategy_trigger", "regime_filter", "higher_timeframe"],
    "chop_risk_score": ["regime_filter", "dense_zone_breakout"],
    "news_risk_score": ["live_news_orderflow"],
    "orderflow_confirmation_score": ["live_news_orderflow"],
    "dense_zone_breakout_quality_score": ["dense_zone_breakout"],
    "pattern_confirmation_score": ["pattern_structure"],
    "position_tier": [
        "core_strategy_trigger",
        "pattern_structure",
        "dense_zone_breakout",
        "regime_filter",
        "higher_timeframe",
        "live_news_orderflow",
    ],
}


@dataclass(frozen=True)
class TradeFeature:
    signal_idx: int
    signal_time: str
    entry_time: str
    exit_time: str
    side: str
    exit_reason: str
    pnl: float
    return_pct: float
    mae_pct: float
    mfe_pct: float
    signal_strength: float
    breakout_atr: float
    volume_multiple: float
    atr_pct: float
    pattern_type: str
    pattern_family: str
    pattern_confidence: float
    pattern_aligned_score: float
    dense_position: str
    dense_breakout_status: str
    dense_trend_score: float
    dense_range_score: float
    dense_strength: float
    regime_candidate: str
    regime_strategy_allowed: str
    regime_breakout_quality: str
    regime_trend_score: float
    regime_range_score: float
    regime_risk_score: float
    htf_signal_alignment: str
    htf_alignment_score: float
    htf_trend_strength: float
    entry_quality_score: float
    confirmations: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline 5-tier sizing research for the ETH 1h trend strategy. It does not call DeepSeek."
    )
    parser.add_argument("--start", default="2022-01-01")
    parser.add_argument("--end", default=datetime.now(UTC).strftime("%Y-%m-%d"))
    parser.add_argument("--source", default="binance")
    parser.add_argument("--initial-equity", type=float, default=10_000.0)
    parser.add_argument("--leverage", type=float, default=4.0)
    parser.add_argument("--fee-rate", type=float, default=0.0006)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--split", default="2024-01-01")
    parser.add_argument("--output", default="data/research/pure_strategy_tier_research_eth_2022_2026.json")
    return parser.parse_args()


def clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    if not math.isfinite(float(value)):
        return low
    return max(low, min(high, float(value)))


def pattern_alignment_score(pattern_direction: Any, side: str, confidence: float, pattern_type: str) -> float:
    if isinstance(pattern_direction, Side):
        direction = pattern_direction.value
    else:
        direction = str(pattern_direction or "")
    if direction == side:
        return clip(0.45 + confidence * 0.55)
    if direction in {"long", "short"} and direction != side:
        return clip(0.25 - confidence * 0.15)
    if pattern_type in {"unknown", "insufficient_data"}:
        return 0.5
    return clip(0.35 + confidence * 0.25)


def entry_quality_score(feature: dict[str, float]) -> float:
    range_safety = 1.0 - clip(feature["regime_range_score"])
    risk_safety = 1.0 - clip(feature["regime_risk_score"])
    volume_score = clip((feature["volume_multiple"] - 2.5) / 2.0)
    breakout_score = clip(feature["breakout_atr"] / 1.2)
    return clip(
        feature["signal_strength"] * 0.18
        + breakout_score * 0.14
        + volume_score * 0.12
        + feature["regime_trend_score"] * 0.18
        + feature["dense_trend_score"] * 0.10
        + feature["pattern_aligned_score"] * 0.12
        + feature["htf_alignment_score"] * 0.11
        + range_safety * 0.08
        + risk_safety * 0.07
    )


def confirmation_count(item: TradeFeature) -> int:
    checks = [
        item.signal_strength >= 0.75,
        item.breakout_atr >= 0.55,
        item.volume_multiple >= 3.0,
        item.pattern_aligned_score >= 0.65,
        item.dense_trend_score >= 0.58 or item.dense_breakout_status in {"breakout_up", "breakout_down", "vacuum_travel"},
        item.regime_trend_score >= 0.62 and item.regime_breakout_quality in {"strong", "pending"},
        item.htf_alignment_score >= 0.62,
    ]
    return sum(1 for value in checks if value)


def _policy_hard_block(item: TradeFeature) -> bool:
    if item.dense_breakout_status == "failed_breakout":
        return True
    if item.regime_risk_score >= 0.80:
        return True
    if item.htf_signal_alignment == "conflict" and item.htf_trend_strength >= 0.75 and item.regime_risk_score >= 0.55:
        return True
    return False


def conservative_policy(item: TradeFeature) -> str:
    if _policy_hard_block(item):
        return "block"
    score = item.entry_quality_score
    if item.regime_strategy_allowed != "trend":
        return "block" if score < 0.70 else "weak"
    if item.regime_risk_score >= 0.70:
        return "weak"
    if item.regime_range_score >= 0.62:
        return "weak" if score < 0.76 else "normal"
    if score >= 0.85 and item.confirmations >= 4:
        return "full"
    if score >= 0.75 and item.confirmations >= 3:
        return "strong"
    if score >= 0.65:
        return "normal"
    if score >= 0.55:
        return "weak"
    return "block"


def balanced_policy(item: TradeFeature) -> str:
    if _policy_hard_block(item):
        return "block"
    score = item.entry_quality_score
    if item.regime_strategy_allowed not in {"trend", "none"} and score < 0.68:
        return "weak"
    if item.regime_range_score >= 0.70 and item.regime_trend_score < 0.60:
        return "weak"
    if score >= 0.82 and item.confirmations >= 4 and item.regime_risk_score < 0.45:
        return "full"
    if score >= 0.72 and item.confirmations >= 3:
        return "strong"
    if score >= 0.62 and item.confirmations >= 2:
        return "normal"
    if score >= 0.52:
        return "weak"
    return "block"


def aggressive_policy(item: TradeFeature) -> str:
    if _policy_hard_block(item):
        return "block"
    score = item.entry_quality_score
    if score >= 0.78 and item.confirmations >= 3:
        return "full"
    if score >= 0.68 and item.confirmations >= 2:
        return "strong"
    if score >= 0.58:
        return "normal"
    if score >= 0.48:
        return "weak"
    return "block"


POLICIES: dict[str, Callable[[TradeFeature], str]] = {
    "structural_conservative_proxy": conservative_policy,
    "balanced_candidate_v1": balanced_policy,
    "aggressive_candidate_v1": aggressive_policy,
}


def dataframe_time_index(df: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(df["timestamp"], utc=True, errors="coerce") if "timestamp" in df.columns else pd.Series([], dtype="datetime64[ns, UTC]")


def row_index_for_time(df: pd.DataFrame, value: str, fallback: int) -> int:
    if "timestamp" not in df.columns:
        return max(0, min(fallback, len(df) - 1))
    target = pd.Timestamp(value)
    if target.tzinfo is None:
        target = target.tz_localize("UTC")
    else:
        target = target.tz_convert("UTC")
    times = dataframe_time_index(df)
    matches = times[times == target]
    if not matches.empty:
        return int(matches.index[0])
    return max(0, min(fallback, len(df) - 1))


def favorable_excursion_pct(df: pd.DataFrame, side: str, entry_idx: int, exit_idx: int, entry_price: float) -> float:
    if exit_idx < entry_idx:
        exit_idx = entry_idx
    window = df.iloc[entry_idx : exit_idx + 1]
    if window.empty or entry_price <= 0:
        return 0.0
    if side == "long":
        best = float(window["high"].max())
        return max(0.0, (best - entry_price) / entry_price * 100)
    best = float(window["low"].min())
    return max(0.0, (entry_price - best) / entry_price * 100)


def extract_trade_features(
    df: pd.DataFrame,
    evaluated: list[Any],
    *,
    symbol: str,
    timeframe: str,
    initial_equity: float,
    leverage: float,
    trend_config: Any,
) -> list[TradeFeature]:
    strategy = TrendStrategy(trend_config)
    dense_analyzer = DenseZoneAnalyzer()
    pattern_detector = PatternDetector()
    regime_analyzer = RegimePatternAnalyzer()
    features: list[TradeFeature] = []
    source_columns = [col for col in ["timestamp", "open", "high", "low", "close", "volume"] if col in df.columns]

    for item in evaluated:
        if item.signal_idx < 0 or item.signal_idx >= len(df):
            continue
        window = df.iloc[: item.signal_idx + 1][source_columns].copy()
        signal = strategy.generate_signal(
            symbol=symbol,
            timeframe=timeframe,
            candles=window,
            position=PositionSnapshot(symbol=symbol, qty=0.0, mark_price=float(window["close"].iloc[-1])),
            equity=initial_equity,
            ai_multiplier=1.0,
            leverage=leverage,
        )
        if signal.action not in {SignalAction.LONG, SignalAction.SHORT}:
            continue
        dense_zone = dense_analyzer.calculate(symbol, window)
        pattern = pattern_detector.detect(symbol, window)
        regime = regime_analyzer.analyze(symbol, window, dense_zone, pattern)
        htf = higher_timeframe_context(window, item.trade.side)
        evidence = signal.technical_evidence or {}
        atr = float(evidence.get("atr") or 0.0)
        close = float(evidence.get("close") or signal.current_price or 0.0)
        pattern_score = pattern_alignment_score(pattern.breakout_direction, item.trade.side, pattern.confidence, pattern.pattern_type)
        raw_feature = {
            "signal_strength": float(signal.signal_strength),
            "breakout_atr": float(evidence.get("breakout_atr") or 0.0),
            "volume_multiple": float(evidence.get("volume_multiple") or 0.0),
            "atr_pct": atr / max(close, 1e-9),
            "pattern_aligned_score": pattern_score,
            "dense_trend_score": float(dense_zone.trend_score),
            "regime_trend_score": float(regime.trend_score),
            "regime_range_score": float(regime.range_score),
            "regime_risk_score": float(regime.risk_score),
            "htf_alignment_score": float(htf.get("alignment_score") or 0.0),
        }
        entry_idx = row_index_for_time(df, item.trade.entry_time, item.signal_idx + 1)
        exit_idx = row_index_for_time(df, item.trade.exit_time, entry_idx)
        entry_feature = TradeFeature(
            signal_idx=int(item.signal_idx),
            signal_time=str(item.signal_time),
            entry_time=str(item.trade.entry_time),
            exit_time=str(item.trade.exit_time),
            side=str(item.trade.side),
            exit_reason=str(item.trade.exit_reason),
            pnl=float(item.trade.pnl),
            return_pct=float(item.trade.return_pct),
            mae_pct=float(item.trade.max_adverse_excursion_pct),
            mfe_pct=favorable_excursion_pct(df, item.trade.side, entry_idx, exit_idx, float(item.trade.entry_price)),
            signal_strength=float(signal.signal_strength),
            breakout_atr=float(evidence.get("breakout_atr") or 0.0),
            volume_multiple=float(evidence.get("volume_multiple") or 0.0),
            atr_pct=atr / max(close, 1e-9),
            pattern_type=str(pattern.pattern_type),
            pattern_family=str(pattern.pattern_family),
            pattern_confidence=float(pattern.confidence),
            pattern_aligned_score=pattern_score,
            dense_position=str(dense_zone.current_position),
            dense_breakout_status=str(dense_zone.breakout_status),
            dense_trend_score=float(dense_zone.trend_score),
            dense_range_score=float(dense_zone.range_score),
            dense_strength=float(dense_zone.strength),
            regime_candidate=str(regime.regime_candidate),
            regime_strategy_allowed=str(regime.strategy_allowed),
            regime_breakout_quality=str(regime.breakout_quality),
            regime_trend_score=float(regime.trend_score),
            regime_range_score=float(regime.range_score),
            regime_risk_score=float(regime.risk_score),
            htf_signal_alignment=str(htf.get("signal_alignment") or "unknown"),
            htf_alignment_score=float(htf.get("alignment_score") or 0.0),
            htf_trend_strength=float(htf.get("trend_strength") or 0.0),
            entry_quality_score=entry_quality_score(raw_feature),
            confirmations=0,
        )
        entry_feature = entry_feature.__class__(**{**asdict(entry_feature), "confirmations": confirmation_count(entry_feature)})
        features.append(entry_feature)
    return features


def summarize_numeric_features(items: list[TradeFeature]) -> dict[str, Any]:
    winners = [item for item in items if item.pnl > 0]
    losers = [item for item in items if item.pnl <= 0]
    output: dict[str, Any] = {}
    for name in NUMERIC_FEATURES:
        win_values = [float(getattr(item, name)) for item in winners]
        loss_values = [float(getattr(item, name)) for item in losers]
        all_values = win_values + loss_values
        if not all_values:
            continue
        win_mean = mean(win_values) if win_values else 0.0
        loss_mean = mean(loss_values) if loss_values else 0.0
        variance = mean([(value - mean(all_values)) ** 2 for value in all_values]) if len(all_values) > 1 else 0.0
        effect = (win_mean - loss_mean) / math.sqrt(variance) if variance > 1e-12 else 0.0
        output[name] = {
            "winner_median": median(win_values) if win_values else None,
            "loser_median": median(loss_values) if loss_values else None,
            "winner_mean": win_mean if win_values else None,
            "loser_mean": loss_mean if loss_values else None,
            "effect_size": effect,
        }
    return output


def summarize_categorical_features(items: list[TradeFeature]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for name in CATEGORICAL_FEATURES:
        groups: dict[str, list[TradeFeature]] = defaultdict(list)
        for item in items:
            groups[str(getattr(item, name))].append(item)
        rows = []
        for value, group in groups.items():
            wins = [item for item in group if item.pnl > 0]
            rows.append(
                {
                    "value": value,
                    "count": len(group),
                    "win_rate_pct": len(wins) / max(len(group), 1) * 100,
                    "avg_pnl": sum(item.pnl for item in group) / max(len(group), 1),
                    "total_pnl": sum(item.pnl for item in group),
                }
            )
        rows.sort(key=lambda row: (row["count"], row["total_pnl"]), reverse=True)
        output[name] = rows[:12]
    return output


def summarize_factor_groups(numeric_overlap: dict[str, Any], categorical_overlap: dict[str, Any]) -> dict[str, Any]:
    groups: dict[str, Any] = {}
    for group_name, definition in FACTOR_GROUPS.items():
        numeric_rows = []
        for feature_name in definition.get("numeric", []):
            stats = numeric_overlap.get(feature_name)
            if not stats:
                continue
            numeric_rows.append(
                {
                    "feature": feature_name,
                    "winner_median": stats.get("winner_median"),
                    "loser_median": stats.get("loser_median"),
                    "effect_size": float(stats.get("effect_size") or 0.0),
                }
            )
        numeric_rows.sort(key=lambda row: abs(row["effect_size"]), reverse=True)

        categorical_rows = {
            feature_name: categorical_overlap.get(feature_name, [])[:5]
            for feature_name in definition.get("categorical", [])
            if feature_name in categorical_overlap
        }
        max_abs_effect = max((abs(row["effect_size"]) for row in numeric_rows), default=0.0)
        avg_abs_effect = (
            sum(abs(row["effect_size"]) for row in numeric_rows) / len(numeric_rows) if numeric_rows else 0.0
        )
        if definition.get("live_only"):
            verdict = "live_only_needs_archival_backtest"
        elif max_abs_effect >= 0.25:
            verdict = "material_candidate"
        elif max_abs_effect >= 0.12:
            verdict = "weak_candidate"
        else:
            verdict = "low_discrimination_in_current_sample"
        groups[group_name] = {
            "label": definition["label"],
            "role": definition["role"],
            "verdict": verdict,
            "max_abs_effect_size": max_abs_effect,
            "avg_abs_effect_size": avg_abs_effect,
            "top_numeric": numeric_rows[:6],
            "categorical": categorical_rows,
            "live_only": definition.get("live_only", []),
        }
    return groups


def max_drawdown(curve: list[float]) -> float:
    peak = curve[0] if curve else 0.0
    drawdown = 0.0
    for value in curve:
        peak = max(peak, value)
        drawdown = min(drawdown, (value - peak) / max(peak, 1e-9))
    return drawdown * 100


def evaluate_policy(items: list[TradeFeature], policy: Callable[[TradeFeature], str], initial_equity: float) -> dict[str, Any]:
    baseline_equity = initial_equity
    overlay_equity = initial_equity
    curve = [overlay_equity]
    tier_counts: Counter[str] = Counter()
    taken: list[tuple[TradeFeature, str, float]] = []
    for item in sorted(items, key=lambda value: value.signal_idx):
        tier = policy(item)
        scale = TIER_SCALE[tier]
        tier_counts[tier] += 1
        pnl_ratio = item.pnl / max(baseline_equity, 1e-9)
        baseline_equity += item.pnl
        pnl = overlay_equity * pnl_ratio * scale
        overlay_equity += pnl
        curve.append(overlay_equity)
        if scale > 0:
            taken.append((item, tier, pnl))
    wins = [pnl for _item, _tier, pnl in taken if pnl > 0]
    losses = [pnl for _item, _tier, pnl in taken if pnl <= 0]
    return {
        "final_equity": overlay_equity,
        "total_pnl": overlay_equity - initial_equity,
        "return_pct": (overlay_equity - initial_equity) / max(initial_equity, 1e-9) * 100,
        "max_drawdown_pct": max_drawdown(curve),
        "trades_taken": len(taken),
        "blocked": tier_counts["block"],
        "tier_counts": dict(tier_counts),
        "win_rate_pct": len(wins) / max(len(taken), 1) * 100,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else (999.0 if wins else 0.0),
        "avg_taken_pnl": sum(pnl for _item, _tier, pnl in taken) / max(len(taken), 1),
    }


def build_report(summary: dict[str, Any]) -> str:
    lines = [
        "# ETH 1h 纯策略仓位分档研究",
        "",
        "本报告只使用入场信号当时已经存在的 K 线、形态、密集区和 4h 结构；不调用 DeepSeek，不使用开仓后的结果反推入场仓位。",
        "",
        "## 样本",
        f"- 时间: `{summary['params']['start']}` 到 `{summary['params']['end']}`",
        f"- 数据源: `{summary['params']['source']}` / 实际 `{summary['data_source']}`",
        f"- 交易数: `{summary['sample']['trades']}`，盈利 `{summary['sample']['wins']}`，亏损 `{summary['sample']['losses']}`",
        f"- 样本警告: {summary['sample']['sample_warning']}",
        "",
        "## 策略基准",
        f"- 总收益率: `{summary['baseline']['total_return_pct']:.2f}%`",
        f"- 最大回撤: `{summary['baseline']['max_drawdown_pct']:.2f}%`",
        f"- 胜率: `{summary['baseline']['win_rate_pct']:.2f}%`",
        f"- Profit factor: `{summary['baseline']['profit_factor']:.3f}`",
        "",
        "## 候选分档策略反事实",
    ]
    for name, result in summary["policy_results"].items():
        lines.append(
            f"- `{name}`: PnL `{result['total_pnl']:.2f}`, return `{result['return_pct']:.2f}%`, "
            f"DD `{result['max_drawdown_pct']:.2f}%`, trades `{result['trades_taken']}`, tiers `{result['tier_counts']}`"
        )
    lines.extend(["", "## 盈利/亏损差异最大的数值特征"])
    ranked = sorted(
        summary["numeric_overlap"].items(),
        key=lambda pair: abs(pair[1].get("effect_size") or 0.0),
        reverse=True,
    )
    for name, stats in ranked[:10]:
        lines.append(
            f"- `{name}`: win_median `{stats['winner_median']}`, loss_median `{stats['loser_median']}`, "
            f"effect `{stats['effect_size']:.3f}`"
        )
    lines.extend(["", "## 因子归类与有效性初判"])
    for group_name, group in summary["factor_groups"].items():
        top = ", ".join(
            f"{row['feature']}={row['effect_size']:.3f}" for row in group.get("top_numeric", [])[:3]
        )
        if not top:
            top = "无离线可统计样本"
        lines.append(
            f"- `{group_name}` / {group['label']}: `{group['verdict']}`，"
            f"max_effect `{group['max_abs_effect_size']:.3f}`，top `{top}`。{group['role']}"
        )
    lines.extend(["", "## DeepSeek 分数到因子组映射"])
    for score_name, groups in summary["deepseek_factor_map"].items():
        lines.append(f"- `{score_name}` -> `{', '.join(groups)}`")
    lines.extend(["", "## 初步结论"])
    lines.extend(f"- {item}" for item in summary["conclusions"])
    return "\n".join(lines) + "\n"


async def main() -> None:
    args = parse_args()
    app_config = load_config()
    trend_config = app_config.strategy.trend
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
        raise RuntimeError("真实K线不可用，拒绝用 synthetic 数据做仓位分档研究。")

    baseline, evaluated, df = run_baseline(
        candles=candles,
        config=trend_config,
        split=args.split,
        initial_equity=args.initial_equity,
        fee_rate=args.fee_rate,
        slippage_bps=args.slippage_bps,
        leverage=args.leverage,
    )
    features = extract_trade_features(
        df,
        evaluated,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        initial_equity=args.initial_equity,
        leverage=args.leverage,
        trend_config=trend_config,
    )
    wins = [item for item in features if item.pnl > 0]
    losses = [item for item in features if item.pnl <= 0]
    numeric_overlap = summarize_numeric_features(features)
    categorical_overlap = summarize_categorical_features(features)
    factor_groups = summarize_factor_groups(numeric_overlap, categorical_overlap)
    policy_results = {name: evaluate_policy(features, policy, args.initial_equity) for name, policy in POLICIES.items()}
    sample_warning = "ok"
    if len(features) < 30:
        sample_warning = "样本少于30笔，只能诊断，不能改实盘阈值。"
    elif len(features) < 100:
        sample_warning = "样本少于100笔，阈值只能小幅试验，不能视为高置信。"

    conclusions = [
        "该研究只能优化开仓前一次性分档，不支持持仓后闭K加仓。",
        "缺失新闻和订单流历史归档时，不能把缺失证据当成负面证据；它只能降低置信度。",
            "如 balanced_candidate_v1 在样本外也优于 structural_conservative_proxy，才可以考虑把实盘5档从偏防守调到平衡版。",
        "任何候选阈值必须再经过 walk-forward；不能用全样本最优结果直接上线。",
    ]
    summary = {
        "created_at": datetime.now(UTC).isoformat(),
        "method": {
            "lookahead_guard": "features are extracted from candles up to signal_idx only",
            "deepseek_calls": 0,
            "historical_news": "not used",
            "historical_orderflow": "not used",
            "position_model": "entry-time-only 5-tier sizing; no post-entry add-on",
        },
        "params": {
            "start": args.start,
            "end": args.end,
            "source": args.source,
            "initial_equity": args.initial_equity,
            "leverage": args.leverage,
            "fee_rate": args.fee_rate,
            "slippage_bps": args.slippage_bps,
            "trend_config": trend_config.model_dump(),
        },
        "data_source": data_source,
        "baseline": {key: value for key, value in baseline.items() if key not in {"trade_ledger", "trades", "equity_curve_tail"}},
        "sample": {
            "trades": len(features),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": len(wins) / max(len(features), 1) * 100,
            "sample_warning": sample_warning,
        },
        "numeric_overlap": numeric_overlap,
        "categorical_overlap": categorical_overlap,
        "factor_groups": factor_groups,
        "deepseek_factor_map": DEEPSEEK_SCORE_TO_FACTOR_GROUPS,
        "policy_results": policy_results,
        "features": [asdict(item) for item in features],
        "conclusions": conclusions,
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = output.with_suffix(".md")
    md_path.write_text(build_report(summary), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output),
                "report": str(md_path),
                "data_source": data_source,
                "trades": len(features),
                "baseline_return_pct": baseline.get("total_return_pct"),
                "policy_results": policy_results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
