from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_quant_trader.brain.deepseek import DeepSeekBrain
from ai_quant_trader.core.config import load_config
from ai_quant_trader.core.models import (
    AggregatedOrderflow,
    Alignment,
    NewsDigest,
    PositionSnapshot,
    SignalAction,
    StrategySignal,
    TrendStrategyConfig,
    VetoAction,
)
from ai_quant_trader.data.market import MarketDataClient
from ai_quant_trader.features.dense_zone import DenseZoneAnalyzer
from ai_quant_trader.features.patterns import PatternDetector
from ai_quant_trader.features.regime import RegimePatternAnalyzer
from ai_quant_trader.strategy.lab import BacktestCostModel, BacktestTrade, _summarize_backtest
from ai_quant_trader.strategy.trend import TrendStrategy


SYMBOL = "ETH/USDT:USDT"
TIMEFRAME = "1h"


@dataclass
class EvaluatedTrade:
    trade: BacktestTrade
    signal_idx: int
    signal_time: str
    period: str


@dataclass
class AiReview:
    signal_idx: int
    signal_time: str
    period: str
    baseline_side: str
    baseline_pnl: float
    baseline_exit_reason: str
    veto_action: str
    confidence: float
    direction: str
    regime: str
    trend_continuation_score: float
    chop_risk_score: float
    false_breakout_risk: float
    event_risk_score: float
    local_regime_candidate: str
    local_strategy_allowed: str
    local_breakout_quality: str
    local_trend_score: float
    local_range_score: float
    local_risk_score: float
    htf_trend_direction: str
    htf_trend_strength: float
    htf_signal_alignment: str
    htf_alignment_score: float
    brief_reason: str
    reason_codes: list[str]
    data_quality_warnings: list[str]
    thinking_enabled: bool = False
    reasoning_content_chars: int = 0
    reasoning_content_sha256: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Low-call DeepSeek overlay evaluation for trend strategy entry candidates.")
    parser.add_argument("--start", default="2022-01-01")
    parser.add_argument("--end", default=datetime.now(UTC).strftime("%Y-%m-%d"))
    parser.add_argument("--split", default="2024-01-01")
    parser.add_argument("--max-calls", type=int, default=32)
    parser.add_argument("--initial-equity", type=float, default=200.0)
    parser.add_argument("--leverage", type=float, default=4.0)
    parser.add_argument("--position-fraction", type=float, default=1.0)
    parser.add_argument("--fee-rate", type=float, default=0.0006)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--source", default="binance")
    parser.add_argument("--model", default="")
    parser.add_argument("--deepseek-timeout", type=int, default=25)
    parser.add_argument("--deepseek-retries", type=int, default=1)
    parser.add_argument("--reuse-reviews-from", default="")
    parser.add_argument("--output", default="data/optimization/deepseek_overlay_eval_eth_2022_present.json")
    return parser.parse_args()


def timestamp_text(row: pd.Series, fallback: int) -> str:
    value = row.get("timestamp", fallback)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return str(value)


def build_config(base: TrendStrategyConfig, position_fraction: float) -> TrendStrategyConfig:
    return base.model_copy(
        update={
            "variant": "with_volume",
            "kc_length": 20,
            "kc_scalar": 2.8,
            "atr_length": 14,
            "atr_stop_multiple": 1.5,
            "vma_length": 20,
            "volume_multiple": 2.5,
            "position_fraction": position_fraction,
            "use_volume_filter": True,
            "momentum_filter": "kdj",
            "kdj_length": 9,
            "kdj_k_smooth": 3,
            "kdj_d_smooth": 3,
        }
    )


def period_name(signal_time: str, split: str) -> str:
    return "bear_2022_to_2024_01" if signal_time[:10] < split else "bull_2024_to_present"


def run_baseline(
    candles: pd.DataFrame,
    config: TrendStrategyConfig,
    split: str,
    initial_equity: float,
    fee_rate: float,
    slippage_bps: float,
    leverage: float,
) -> tuple[dict[str, Any], list[EvaluatedTrade], pd.DataFrame]:
    cost_model = BacktestCostModel.from_inputs(fee_rate, slippage_bps)
    strategy = TrendStrategy(config)
    df = strategy.add_indicators(candles)
    warmup = strategy.warmup_candles()

    equity = initial_equity
    position = PositionSnapshot(symbol=SYMBOL, qty=0.0, mark_price=0.0)
    side: str | None = None
    entry_price = 0.0
    entry_time = ""
    entry_signal_idx = -1
    entry_signal_time = ""
    qty = 0.0
    stop_loss_price: float | None = None
    entry_fee_paid = 0.0
    entry_slippage_paid = 0.0
    max_adverse_excursion = 0.0
    max_adverse_excursion_pct = 0.0
    pending_action: SignalAction | None = None
    pending_signal_idx = -1
    pending_signal_time = ""
    pending_stop_atr = 0.0
    trades: list[BacktestTrade] = []
    evaluated: list[EvaluatedTrade] = []
    equity_curve = [equity]

    for idx in range(max(1, warmup), len(df)):
        last = df.iloc[idx]
        close_price = float(last["close"])
        open_price = float(last.get("open") or close_price)
        timestamp = timestamp_text(last, idx)
        position.mark_price = close_price
        open_slip, _ = cost_model.slippage(open_price, last)

        if pending_action is not None:
            reversal_entry = pending_action in {SignalAction.LONG, SignalAction.SHORT}
            should_close_existing = (
                side == "long"
                and pending_action in {SignalAction.EXIT_LONG, SignalAction.SHORT}
                or side == "short"
                and pending_action in {SignalAction.EXIT_SHORT, SignalAction.LONG}
            )
            if side and should_close_existing:
                trade = close_trade(
                    side=side,
                    entry_time=entry_time,
                    exit_time=timestamp,
                    entry_price=entry_price,
                    exit_price=open_price - open_slip if side == "long" else open_price + open_slip,
                    qty=qty,
                    entry_fee_paid=entry_fee_paid,
                    entry_slippage_paid=entry_slippage_paid,
                    exit_row=last,
                    cost_model=cost_model,
                    stop_loss_price=stop_loss_price,
                    max_adverse_excursion=max_adverse_excursion,
                    max_adverse_excursion_pct=max_adverse_excursion_pct,
                    initial_equity=initial_equity,
                    exit_reason="reversal" if reversal_entry else "kc_mid_exit",
                    intrabar_path="next_open",
                )
                equity += trade.pnl
                trades.append(trade)
                evaluated.append(
                    EvaluatedTrade(
                        trade=trade,
                        signal_idx=entry_signal_idx,
                        signal_time=entry_signal_time,
                        period=period_name(entry_signal_time, split),
                    )
                )
                side = None
                position = PositionSnapshot(symbol=SYMBOL, qty=0.0, mark_price=close_price)
                entry_price = 0.0
                entry_time = ""
                entry_signal_idx = -1
                entry_signal_time = ""
                qty = 0.0
                stop_loss_price = None
                entry_fee_paid = 0.0
                entry_slippage_paid = 0.0
                max_adverse_excursion = 0.0
                max_adverse_excursion_pct = 0.0

            if side is None and reversal_entry:
                side = "long" if pending_action == SignalAction.LONG else "short"
                entry_price = open_price + open_slip if side == "long" else open_price - open_slip
                entry_time = timestamp
                entry_signal_idx = pending_signal_idx
                entry_signal_time = pending_signal_time
                stop_loss_price = (
                    entry_price - pending_stop_atr * config.atr_stop_multiple
                    if side == "long"
                    else entry_price + pending_stop_atr * config.atr_stop_multiple
                )
                notional = equity * config.position_fraction * leverage
                qty = notional / max(entry_price, 1e-9)
                position.side = side
                position.qty = qty if side == "long" else -qty
                entry_fee_paid = notional * cost_model.taker_fee_rate
                entry_slippage_paid = open_slip * qty

            pending_action = None
            pending_signal_idx = -1
            pending_signal_time = ""
            pending_stop_atr = 0.0

        if side:
            adverse, adverse_pct = adverse_excursion(side, last, entry_price, qty)
            max_adverse_excursion = min(max_adverse_excursion, adverse)
            max_adverse_excursion_pct = min(max_adverse_excursion_pct, adverse_pct)
            stop_hit = stop_price_hit(side, last, stop_loss_price)
            if stop_hit is not None:
                exit_slip, _ = cost_model.slippage(stop_hit, last)
                trade = close_trade(
                    side=side,
                    entry_time=entry_time,
                    exit_time=timestamp,
                    entry_price=entry_price,
                    exit_price=stop_hit - exit_slip if side == "long" else stop_hit + exit_slip,
                    qty=qty,
                    entry_fee_paid=entry_fee_paid,
                    entry_slippage_paid=entry_slippage_paid,
                    exit_row=last,
                    cost_model=cost_model,
                    stop_loss_price=stop_loss_price,
                    max_adverse_excursion=max_adverse_excursion,
                    max_adverse_excursion_pct=max_adverse_excursion_pct,
                    initial_equity=initial_equity,
                    exit_reason="atr_stop",
                    intrabar_path="stop",
                )
                equity += trade.pnl
                trades.append(trade)
                evaluated.append(
                    EvaluatedTrade(
                        trade=trade,
                        signal_idx=entry_signal_idx,
                        signal_time=entry_signal_time,
                        period=period_name(entry_signal_time, split),
                    )
                )
                side = None
                position = PositionSnapshot(symbol=SYMBOL, qty=0.0, mark_price=close_price)
                entry_price = 0.0
                entry_time = ""
                entry_signal_idx = -1
                entry_signal_time = ""
                qty = 0.0
                stop_loss_price = None
                entry_fee_paid = 0.0
                entry_slippage_paid = 0.0
                max_adverse_excursion = 0.0
                max_adverse_excursion_pct = 0.0

        action = strategy.evaluate_action_from_indicators(df, idx, position)
        if (
            side is None
            and action in {SignalAction.LONG, SignalAction.SHORT}
            or side == "long"
            and action in {SignalAction.EXIT_LONG, SignalAction.SHORT}
            or side == "short"
            and action in {SignalAction.EXIT_SHORT, SignalAction.LONG}
        ):
            pending_action = action
            pending_signal_idx = idx
            pending_signal_time = timestamp
            pending_stop_atr = float(last["atr"]) if not math.isnan(float(last["atr"])) else 0.0
        equity_curve.append(equity)

    if side:
        last = df.iloc[-1]
        price = float(last["close"])
        timestamp = timestamp_text(last, len(df) - 1)
        slip, _ = cost_model.slippage(price, last)
        trade = close_trade(
            side=side,
            entry_time=entry_time,
            exit_time=timestamp,
            entry_price=entry_price,
            exit_price=price - slip if side == "long" else price + slip,
            qty=qty,
            entry_fee_paid=entry_fee_paid,
            entry_slippage_paid=entry_slippage_paid,
            exit_row=last,
            cost_model=cost_model,
            stop_loss_price=stop_loss_price,
            max_adverse_excursion=max_adverse_excursion,
            max_adverse_excursion_pct=max_adverse_excursion_pct,
            initial_equity=initial_equity,
            exit_reason="end_of_backtest",
            intrabar_path="end",
        )
        equity += trade.pnl
        trades.append(trade)
        evaluated.append(
            EvaluatedTrade(
                trade=trade,
                signal_idx=entry_signal_idx,
                signal_time=entry_signal_time,
                period=period_name(entry_signal_time, split),
            )
        )
        equity_curve.append(equity)

    summary = _summarize_backtest(
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        initial_equity=initial_equity,
        equity=equity,
        trades=trades,
        equity_curve=equity_curve,
        fee_rate=cost_model.taker_fee_rate,
        slippage_bps=cost_model.base_slippage_bps,
        leverage=leverage,
        note="Baseline scheme A for DeepSeek entry-filter evaluation.",
    )
    return summary, evaluated, df


def close_trade(
    side: str,
    entry_time: str,
    exit_time: str,
    entry_price: float,
    exit_price: float,
    qty: float,
    entry_fee_paid: float,
    entry_slippage_paid: float,
    exit_row: pd.Series,
    cost_model: BacktestCostModel,
    stop_loss_price: float | None,
    max_adverse_excursion: float,
    max_adverse_excursion_pct: float,
    initial_equity: float,
    exit_reason: str,
    intrabar_path: str,
) -> BacktestTrade:
    gross = (exit_price - entry_price) * qty if side == "long" else (entry_price - exit_price) * qty
    exit_fee_paid = abs(exit_price * qty) * cost_model.taker_fee_rate
    fee = entry_fee_paid + exit_fee_paid
    slippage_paid = entry_slippage_paid
    return BacktestTrade(
        side=side,
        entry_time=entry_time,
        exit_time=exit_time,
        entry_price=entry_price,
        exit_price=exit_price,
        qty=qty,
        pnl=gross - fee,
        return_pct=(gross - fee) / max(initial_equity, 1e-9),
        fee_paid=fee,
        slippage_paid=slippage_paid,
        exit_reason=exit_reason,
        stop_loss_price=stop_loss_price,
        max_adverse_excursion=max_adverse_excursion,
        max_adverse_excursion_pct=max_adverse_excursion_pct * 100,
        intrabar_path=intrabar_path,
    )


def adverse_excursion(side: str, row: pd.Series, entry_price: float, qty: float) -> tuple[float, float]:
    low = float(row["low"])
    high = float(row["high"])
    adverse_price = low if side == "long" else high
    pnl = (adverse_price - entry_price) * qty if side == "long" else (entry_price - adverse_price) * qty
    pct = (adverse_price - entry_price) / entry_price if side == "long" else (entry_price - adverse_price) / entry_price
    return min(0.0, pnl), min(0.0, pct)


def stop_price_hit(side: str, row: pd.Series, stop_loss_price: float | None) -> float | None:
    if stop_loss_price is None:
        return None
    if side == "long" and float(row["low"]) <= stop_loss_price:
        return stop_loss_price
    if side == "short" and float(row["high"]) >= stop_loss_price:
        return stop_loss_price
    return None


def choose_stratified_sample(trades: list[EvaluatedTrade], max_calls: int) -> list[EvaluatedTrade]:
    if len(trades) <= max_calls:
        return trades
    buckets: dict[tuple[str, str], list[EvaluatedTrade]] = {}
    for trade in trades:
        outcome = "win" if trade.trade.pnl > 0 else "loss"
        buckets.setdefault((trade.period, outcome), []).append(trade)
    per_bucket = max(1, max_calls // max(len(buckets), 1))
    selected: list[EvaluatedTrade] = []
    for key in sorted(buckets):
        selected.extend(pick_evenly(buckets[key], per_bucket))
    remaining = [trade for trade in trades if trade not in selected]
    selected.extend(pick_evenly(remaining, max_calls - len(selected)))
    return sorted(selected[:max_calls], key=lambda item: item.signal_idx)


def pick_evenly(items: list[EvaluatedTrade], count: int) -> list[EvaluatedTrade]:
    if count <= 0 or not items:
        return []
    if len(items) <= count:
        return items
    if count == 1:
        return [items[len(items) // 2]]
    step = (len(items) - 1) / (count - 1)
    indexes = sorted({round(i * step) for i in range(count)})
    return [items[idx] for idx in indexes]


def empty_historical_news(signal_time: str) -> NewsDigest:
    return NewsDigest(
        generated_at=datetime.now(UTC),
        items=[],
        macro_risk_level="unknown",
        crypto_sentiment=Alignment.UNKNOWN,
        summary=f"历史新闻未接入授权归档源；评测时间点 {signal_time} 仅使用截至当时的K线、形态和密集区结构。",
        warnings=["historical_news_archive_missing", "do_not_use_model_memory_for_past_news"],
    )


def higher_timeframe_context(window: pd.DataFrame, side: str) -> dict[str, Any]:
    if "timestamp" not in window.columns or len(window) < 120:
        return {
            "timeframe": "4h",
            "trend_direction": "unknown",
            "trend_strength": 0.0,
            "signal_alignment": "unknown",
            "alignment_score": 0.0,
            "warning": "insufficient_4h_history",
        }
    frame = window[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    frame = frame.dropna(subset=["timestamp"]).set_index("timestamp").sort_index()
    if frame.empty:
        return {
            "timeframe": "4h",
            "trend_direction": "unknown",
            "trend_strength": 0.0,
            "signal_alignment": "unknown",
            "alignment_score": 0.0,
            "warning": "invalid_timestamp",
        }
    grouped = frame.resample("4h", label="left", closed="left")
    htf = grouped.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    counts = grouped["close"].count()
    htf = htf[counts >= 4].dropna()
    if len(htf) < 30:
        return {
            "timeframe": "4h",
            "trend_direction": "unknown",
            "trend_strength": 0.0,
            "signal_alignment": "unknown",
            "alignment_score": 0.0,
            "warning": "insufficient_closed_4h_candles",
        }

    close = htf["close"].astype(float)
    high = htf["high"].astype(float)
    low = htf["low"].astype(float)
    kc_mid = close.ewm(span=20, adjust=False, min_periods=20).mean()
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr14 = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    last_close = float(close.iloc[-1])
    last_kc_mid = float(kc_mid.iloc[-1]) if not math.isnan(float(kc_mid.iloc[-1])) else last_close
    last_atr = float(atr14.iloc[-1]) if not math.isnan(float(atr14.iloc[-1])) else max(last_close * 0.01, 1e-9)
    clean_mid = kc_mid.dropna()
    slope_lookback = min(6, max(len(clean_mid) - 1, 1))
    kc_mid_slope = (last_kc_mid - float(clean_mid.iloc[-1 - slope_lookback])) / max(last_close, 1e-9)
    distance_atr = (last_close - last_kc_mid) / max(last_atr, 1e-9)

    if last_close > last_kc_mid and kc_mid_slope > 0:
        direction = "long"
    elif last_close < last_kc_mid and kc_mid_slope < 0:
        direction = "short"
    else:
        direction = "mixed"

    strength = min(1.0, abs(distance_atr) / 3.0 * 0.65 + min(abs(kc_mid_slope) / 0.025, 1.0) * 0.35)
    if direction == side:
        alignment = "aligned"
        alignment_score = 0.65 + strength * 0.35
    elif direction in {"long", "short"} and direction != side:
        alignment = "conflict"
        alignment_score = 0.35 - strength * 0.25
    else:
        alignment = "neutral"
        alignment_score = 0.5

    return {
        "timeframe": "4h",
        "trend_direction": direction,
        "trend_strength": round(strength, 6),
        "signal_alignment": alignment,
        "alignment_score": round(max(0.0, min(1.0, alignment_score)), 6),
        "closed_candles": int(len(htf)),
        "last_close": round(last_close, 6),
        "kc_mid_20": round(last_kc_mid, 6),
        "atr14": round(last_atr, 6),
        "kc_mid_slope_6x4h": round(kc_mid_slope, 8),
        "distance_from_kc_mid_atr": round(distance_atr, 6),
    }


async def review_trade(
    brain: DeepSeekBrain,
    df: pd.DataFrame,
    config: TrendStrategyConfig,
    evaluated: EvaluatedTrade,
    timeout_seconds: int,
) -> AiReview:
    strategy = TrendStrategy(config)
    source_columns = [col for col in ["timestamp", "open", "high", "low", "close", "volume"] if col in df.columns]
    window = df.iloc[: evaluated.signal_idx + 1][source_columns].copy()
    position = PositionSnapshot(symbol=SYMBOL, qty=0.0, mark_price=float(window["close"].iloc[-1]))
    signal = strategy.generate_signal(
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        candles=window,
        position=position,
        equity=200.0,
        ai_multiplier=1.0,
    )
    if signal.action not in {SignalAction.LONG, SignalAction.SHORT}:
        signal = StrategySignal(
            symbol=SYMBOL,
            timeframe=TIMEFRAME,
            action=SignalAction.LONG if evaluated.trade.side == "long" else SignalAction.SHORT,
            current_price=float(window["close"].iloc[-1]),
            signal_strength=0.5,
            technical_evidence={"reason": "reconstructed_baseline_entry"},
        )
    dense_zone = DenseZoneAnalyzer().calculate(SYMBOL, window)
    pattern = PatternDetector().detect(SYMBOL, window)
    regime = RegimePatternAnalyzer().analyze(SYMBOL, window, dense_zone, pattern)
    signal = RegimePatternAnalyzer().enrich_signal(signal, regime)
    htf_context = higher_timeframe_context(window, evaluated.trade.side)
    orderflow = AggregatedOrderflow(
        symbol=SYMBOL,
        timestamp=datetime.now(UTC),
        alignment_hint=Alignment.UNKNOWN,
        data_quality=0.0,
        source_count=0,
        warnings=["historical_orderflow_archive_missing"],
    )
    news = empty_historical_news(evaluated.signal_time)
    decision = await compact_deepseek_review(
        brain=brain,
        signal=signal,
        orderflow=orderflow,
        dense_zone=dense_zone,
        pattern=pattern,
        news=news,
        htf_context=htf_context,
        baseline_pnl=evaluated.trade.pnl,
        timeout_seconds=timeout_seconds,
    )
    return AiReview(
        signal_idx=evaluated.signal_idx,
        signal_time=evaluated.signal_time,
        period=evaluated.period,
        baseline_side=evaluated.trade.side,
        baseline_pnl=evaluated.trade.pnl,
        baseline_exit_reason=evaluated.trade.exit_reason,
        veto_action=str(decision.get("veto_action", "block")),
        confidence=float(decision.get("confidence", 0.0)),
        direction=str(decision.get("direction", "flat")),
        regime=str(decision.get("regime", "uncertain")),
        trend_continuation_score=float(decision.get("trend_continuation_score", 0.0)),
        chop_risk_score=float(decision.get("chop_risk_score", 1.0)),
        false_breakout_risk=float(decision.get("false_breakout_risk", 1.0)),
        event_risk_score=float(decision.get("event_risk_score", 0.0)),
        local_regime_candidate=regime.regime_candidate,
        local_strategy_allowed=regime.strategy_allowed,
        local_breakout_quality=regime.breakout_quality,
        local_trend_score=float(regime.trend_score),
        local_range_score=float(regime.range_score),
        local_risk_score=float(regime.risk_score),
        htf_trend_direction=str(htf_context.get("trend_direction", "unknown")),
        htf_trend_strength=float(htf_context.get("trend_strength", 0.0)),
        htf_signal_alignment=str(htf_context.get("signal_alignment", "unknown")),
        htf_alignment_score=float(decision.get("htf_alignment_score", htf_context.get("alignment_score", 0.0))),
        brief_reason=str(decision.get("brief_reason", "")),
        reason_codes=[str(item) for item in decision.get("reason_codes", [])[:8]],
        data_quality_warnings=[str(item) for item in decision.get("data_quality_warnings", [])[:8]],
        thinking_enabled=bool(decision.get("thinking_enabled", False)),
        reasoning_content_chars=int(decision.get("reasoning_content_chars", 0)),
        reasoning_content_sha256=str(decision.get("reasoning_content_sha256", "")),
    )


async def compact_deepseek_review(
    brain: DeepSeekBrain,
    signal: StrategySignal,
    orderflow: AggregatedOrderflow,
    dense_zone: Any,
    pattern: Any,
    news: NewsDigest,
    htf_context: dict[str, Any],
    baseline_pnl: float,
    timeout_seconds: int,
) -> dict[str, Any]:
    if not brain.api_key:
        return {
            "veto_action": "block",
            "confidence": 0.0,
            "direction": "flat",
            "regime": "uncertain",
            "trend_continuation_score": 0.0,
            "chop_risk_score": 1.0,
            "false_breakout_risk": 1.0,
            "event_risk_score": 0.0,
            "htf_alignment_score": 0.0,
            "thinking_enabled": True,
            "reasoning_content_chars": 0,
            "reasoning_content_sha256": "",
            "brief_reason": "DeepSeek API key missing; cannot evaluate.",
            "reason_codes": ["missing_deepseek_api_key"],
            "data_quality_warnings": ["historical_news_archive_missing", "historical_orderflow_archive_missing"],
        }

    payload = {
        "task": "Evaluate whether an already-triggered ETHUSDT 1h trend breakout entry should be allowed, reduced, or blocked. Use only the supplied timestamp data. Do not use model memory of future events.",
        "required_json": {
            "veto_action": "allow|reduce|block",
            "confidence": "0-1",
            "direction": "long|short|flat",
            "regime": "trend|range|uncertain",
            "trend_continuation_score": "0-1, high means this breakout can continue as a trend",
            "chop_risk_score": "0-1, high means range/chop whipsaw risk",
            "false_breakout_risk": "0-1, high means breakout likely fails",
            "event_risk_score": "0-1, high means news/event risk if supplied data proves it",
            "htf_alignment_score": "0-1, high means 4h closed-candle trend supports this 1h entry",
            "brief_reason": "Chinese one sentence",
            "reason_codes": ["short_codes"],
            "data_quality_warnings": ["warnings"],
        },
        "baseline_trade_outcome_is_hidden_from_decision": True,
        "signal": {
            "symbol": signal.symbol,
            "timeframe": signal.timeframe,
            "action": str(signal.action),
            "current_price": signal.current_price,
            "signal_strength": signal.signal_strength,
            "technical_evidence": compact_dict(signal.technical_evidence),
        },
        "dense_zone": compact_dict(
            dense_zone.model_dump(),
            {
                "poc",
                "vah",
                "val",
                "current_position",
                "zone_low",
                "zone_high",
                "zone_mid",
                "breakout_status",
                "retest_status",
                "trend_score",
                "range_score",
                "structure_label",
            },
        ),
        "pattern": compact_dict(pattern.model_dump()),
        "regime_pattern": {
            "regime_candidate": signal.technical_evidence.get("regime_candidate"),
            "strategy_allowed": signal.technical_evidence.get("strategy_allowed"),
            "breakout_quality": signal.technical_evidence.get("breakout_quality"),
            "trend_score": signal.technical_evidence.get("regime_trend_score"),
            "range_score": signal.technical_evidence.get("regime_range_score"),
            "risk_score": signal.technical_evidence.get("regime_risk_score"),
            "reason_codes": signal.technical_evidence.get("regime_reason_codes"),
        },
        "higher_timeframe_4h": htf_context,
        "orderflow": {
            "alignment_hint": str(orderflow.alignment_hint),
            "data_quality": orderflow.data_quality,
            "warnings": orderflow.warnings,
        },
        "news": {
            "summary": news.summary,
            "macro_risk_level": news.macro_risk_level,
            "news_direction": str(news.news_direction),
            "crypto_sentiment": str(news.crypto_sentiment),
            "warnings": news.warnings,
        },
        "decision_policy": [
            "score first, veto second: the numeric scores are more important than veto_action for offline evaluation",
            "allow when trend_continuation_score is high and false_breakout_risk/chop_risk are acceptable",
            "missing news/orderflow is a confidence haircut, not an automatic reduce/block reason",
            "reduce when structure is mixed, trend score is only moderate, or breakout is weak",
            "use only fully closed 4h candles; if 4h trend strongly aligns, avoid reducing a 1h signal unless false_breakout_risk is extreme",
            "if 1h signal conflicts with 4h trend and chop/false-breakout risk is high, reduce size more aggressively",
            "block only when range/high-risk/failed-breakout evidence is strong; do not block a strong breakout only because historical news/orderflow is missing",
        ],
    }

    def call() -> dict[str, Any]:
        request_body = {
            "model": brain.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是量化风控审查器。只输出JSON，不要Markdown。"
                        "不要调用历史记忆，不要猜新闻；新闻缺失只降低置信度，不能单独作为阻断理由。"
                        "先给四个0到1分数：趋势延续、震荡风险、假突破风险、事件风险。"
                        "必须参考已收盘4小时K线：高周期同向时保护趋势盈利，高周期反向时提高假突破风险。"
                        "只有震荡结构、假突破、高风险证据强时才block，强突破优先reduce而不是block。"
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        }
        response = requests.post(
            f"{brain.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {brain.api_key}", "Content-Type": "application/json"},
            json=request_body,
            timeout=max(5, timeout_seconds),
        )
        response.raise_for_status()
        message = response.json()["choices"][0]["message"]
        reasoning_content = str(message.get("reasoning_content") or "")
        content = message["content"]
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("DeepSeek did not return a JSON object.")
        decision = normalize_compact_decision(parsed)
        decision["thinking_enabled"] = True
        decision["reasoning_content_chars"] = len(reasoning_content)
        decision["reasoning_content_sha256"] = (
            hashlib.sha256(reasoning_content.encode("utf-8")).hexdigest() if reasoning_content else ""
        )
        return decision

    try:
        return await asyncio.to_thread(call)
    except Exception as exc:  # noqa: BLE001 - evaluation must record failures without leaking secrets
        return {
            "veto_action": "block",
            "confidence": 0.0,
            "direction": "flat",
            "regime": "uncertain",
            "trend_continuation_score": 0.0,
            "chop_risk_score": 1.0,
            "false_breakout_risk": 1.0,
            "event_risk_score": 0.0,
            "htf_alignment_score": 0.0,
            "thinking_enabled": True,
            "reasoning_content_chars": 0,
            "reasoning_content_sha256": "",
            "brief_reason": f"DeepSeek调用失败，按保守规则阻断：{type(exc).__name__}",
            "reason_codes": ["deepseek_call_failed"],
            "data_quality_warnings": ["historical_news_archive_missing", "historical_orderflow_archive_missing"],
        }


def compact_dict(data: dict[str, Any], allowed: set[str] | None = None) -> dict[str, Any]:
    items = data.items() if allowed is None else ((key, data.get(key)) for key in allowed)
    output: dict[str, Any] = {}
    for key, value in items:
        if isinstance(value, float):
            output[key] = round(value, 6)
        elif isinstance(value, (str, int, bool)) or value is None:
            output[key] = value
        elif isinstance(value, list):
            output[key] = value[:8]
    return output


def normalize_compact_decision(parsed: dict[str, Any]) -> dict[str, Any]:
    veto = str(parsed.get("veto_action", "block")).strip().lower()
    if veto not in {"allow", "reduce", "block"}:
        veto = "block"
    direction = str(parsed.get("direction", "flat")).strip().lower()
    if direction not in {"long", "short", "flat"}:
        direction = "flat"
    regime = str(parsed.get("regime", "uncertain")).strip().lower()
    if regime not in {"trend", "range", "uncertain"}:
        regime = "uncertain"
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    trend_continuation_score = clipped_score(parsed.get("trend_continuation_score"), 0.0)
    chop_risk_score = clipped_score(parsed.get("chop_risk_score"), 0.5)
    false_breakout_risk = clipped_score(parsed.get("false_breakout_risk"), 0.5)
    event_risk_score = clipped_score(parsed.get("event_risk_score"), 0.0)
    htf_alignment_score = clipped_score(parsed.get("htf_alignment_score"), 0.0)
    return {
        "veto_action": veto,
        "confidence": max(0.0, min(1.0, confidence)),
        "direction": direction,
        "regime": regime,
        "trend_continuation_score": trend_continuation_score,
        "chop_risk_score": chop_risk_score,
        "false_breakout_risk": false_breakout_risk,
        "event_risk_score": event_risk_score,
        "htf_alignment_score": htf_alignment_score,
        "thinking_enabled": bool(parsed.get("thinking_enabled", False)),
        "reasoning_content_chars": int(parsed.get("reasoning_content_chars", 0) or 0),
        "reasoning_content_sha256": str(parsed.get("reasoning_content_sha256", "")),
        "brief_reason": str(parsed.get("brief_reason", ""))[:240],
        "reason_codes": parsed.get("reason_codes") if isinstance(parsed.get("reason_codes"), list) else [],
        "data_quality_warnings": parsed.get("data_quality_warnings")
        if isinstance(parsed.get("data_quality_warnings"), list)
        else [],
    }


def clipped_score(value: Any, default: float) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        score = default
    return max(0.0, min(1.0, score))


def summarize_reviews(reviews: list[AiReview]) -> dict[str, Any]:
    by_period: dict[str, list[AiReview]] = {}
    for review in reviews:
        by_period.setdefault(review.period, []).append(review)

    def vetoed(review: AiReview) -> bool:
        return review.veto_action in {str(VetoAction.BLOCK), "block", str(VetoAction.REDUCE), "reduce"}

    def blocked(review: AiReview) -> bool:
        return review.veto_action in {str(VetoAction.BLOCK), "block"}

    def scaled_pnl(review: AiReview) -> float:
        if blocked(review):
            return 0.0
        if review.veto_action in {str(VetoAction.REDUCE), "reduce"}:
            return review.baseline_pnl * 0.5
        return review.baseline_pnl

    output: dict[str, Any] = {}
    for period, items in sorted(by_period.items()):
        wins = [item for item in items if item.baseline_pnl > 0]
        losses = [item for item in items if item.baseline_pnl <= 0]
        vetoed_wins = [item for item in wins if vetoed(item)]
        vetoed_losses = [item for item in losses if vetoed(item)]
        output[period] = {
            "sample_count": len(items),
            "winning_samples": len(wins),
            "losing_samples": len(losses),
            "loss_veto_rate_pct": len(vetoed_losses) / max(len(losses), 1) * 100,
            "win_veto_rate_pct": len(vetoed_wins) / max(len(wins), 1) * 100,
            "loss_block_rate_pct": len([item for item in losses if blocked(item)]) / max(len(losses), 1) * 100,
            "win_block_rate_pct": len([item for item in wins if blocked(item)]) / max(len(wins), 1) * 100,
            "net_sample_pnl_if_block_reduce_skipped": sum(
                item.baseline_pnl for item in items if not vetoed(item)
            ),
            "net_sample_pnl_if_block_skipped_reduce_half": sum(scaled_pnl(item) for item in items),
            "baseline_sample_pnl": sum(item.baseline_pnl for item in items),
        }
    return output


def summarize_policy_grid(reviews: list[AiReview], initial_equity: float = 200.0) -> dict[str, Any]:
    policies = {
        "direct_block_reduce_half": policy_direct_block_reduce_half,
        "conservative_ai_risk_scaler": policy_conservative_ai_risk_scaler,
        "trend_preservation_gate": policy_trend_preservation_gate,
        "block_only_high_conflict": policy_block_only_high_conflict,
        "precision_chop_reducer": policy_precision_chop_reducer,
        "strict_false_breakout_reducer": policy_strict_false_breakout_reducer,
        "graded_false_breakout_scaler": policy_graded_false_breakout_scaler,
        "htf_balanced_scaler": policy_htf_balanced_scaler,
        "htf_extreme_false_breakout_quarter": policy_htf_extreme_false_breakout_quarter,
        "htf_graded_risk_scaler": policy_htf_graded_risk_scaler,
        "controlled_raw_advice_interpreter": policy_controlled_raw_advice_interpreter,
        "raw_advice_4level_guarded": policy_raw_advice_4level_guarded,
        "raw_advice_5level_guarded": policy_raw_advice_5level_guarded,
        "raw_advice_5_trade_levels_plus_block": policy_raw_advice_5_trade_levels_plus_block,
    }
    return {name: summarize_policy(reviews, fn, initial_equity) for name, fn in policies.items()}


def policy_direct_block_reduce_half(review: AiReview) -> float:
    if review.veto_action == "block":
        return 0.0
    if review.veto_action == "reduce":
        return 0.5
    return 1.0


def policy_conservative_ai_risk_scaler(review: AiReview) -> float:
    # AI can reduce risk, but it can only block when both AI and local structure agree risk is high.
    strong_local_risk = (
        review.local_regime_candidate in {"range", "high_risk"}
        and review.local_strategy_allowed != "trend"
        and review.local_risk_score >= 0.45
    )
    strong_ai_risk = (
        review.confidence >= 0.72
        and review.chop_risk_score >= 0.72
        and review.false_breakout_risk >= 0.65
        and review.trend_continuation_score <= 0.35
    )
    if review.veto_action == "block" and strong_local_risk and strong_ai_risk:
        return 0.0
    if review.chop_risk_score >= 0.65 or review.false_breakout_risk >= 0.65 or review.veto_action == "reduce":
        return 0.5
    if review.trend_continuation_score >= 0.68 and review.event_risk_score <= 0.35:
        return 1.0
    return 0.75


def policy_trend_preservation_gate(review: AiReview) -> float:
    # Protects trend-following convexity: strong trend candidates cannot be fully vetoed by AI.
    strong_trend = (
        review.trend_continuation_score >= 0.58
        or review.local_breakout_quality in {"strong", "pending"}
        or review.local_trend_score >= review.local_range_score + 0.12
    )
    failed_or_dangerous = (
        review.local_breakout_quality == "failed"
        or review.local_regime_candidate == "high_risk"
        or review.event_risk_score >= 0.85
    )
    if failed_or_dangerous and review.confidence >= 0.65:
        return 0.0
    if strong_trend:
        if review.chop_risk_score >= 0.75 or review.false_breakout_risk >= 0.75:
            return 0.5
        return 1.0
    if review.chop_risk_score >= 0.72 and review.false_breakout_risk >= 0.6:
        return 0.5
    return 0.75


def policy_block_only_high_conflict(review: AiReview) -> float:
    direction_conflict = review.direction in {"long", "short"} and review.direction != review.baseline_side
    if (
        direction_conflict
        and review.confidence >= 0.75
        and review.trend_continuation_score <= 0.35
        and max(review.chop_risk_score, review.false_breakout_risk, review.event_risk_score) >= 0.75
    ):
        return 0.0
    if review.veto_action == "block" or direction_conflict:
        return 0.5
    return 1.0


def policy_precision_chop_reducer(review: AiReview) -> float:
    # Candidate production shape: DeepSeek is not allowed to block. It only cuts size
    # when it is very specifically identifying chop + false-breakout risk.
    extreme_chop = (
        review.chop_risk_score >= 0.75
        and review.false_breakout_risk >= 0.80
        and review.trend_continuation_score <= 0.30
    )
    local_confirms_chop = (
        review.local_regime_candidate in {"range", "transition"}
        and review.local_breakout_quality in {"none", "weak"}
    )
    if extreme_chop and local_confirms_chop and review.confidence >= 0.35:
        return 0.5
    return 1.0


def policy_strict_false_breakout_reducer(review: AiReview) -> float:
    # Tuned as the safest current candidate on the 128-call Flash Thinking sample:
    # tiny intervention, no full veto, and only cuts when AI sees severe chop/false-breakout
    # while trend continuation is very weak.
    if (
        review.chop_risk_score >= 0.60
        and review.false_breakout_risk >= 0.60
        and review.trend_continuation_score <= 0.25
        and review.confidence >= 0.65
    ):
        return 0.5
    return 1.0


def policy_graded_false_breakout_scaler(review: AiReview) -> float:
    # Gradual position sizing: never blocks, only scales down as AI risk becomes extreme.
    if review.confidence < 0.60:
        return 1.0
    if review.trend_continuation_score > 0.40:
        return 1.0
    risk = max(review.chop_risk_score, review.false_breakout_risk)
    if risk >= 0.85 and review.chop_risk_score >= 0.70 and review.false_breakout_risk >= 0.70:
        return 0.25
    if risk >= 0.75 and review.chop_risk_score >= 0.65 and review.false_breakout_risk >= 0.65:
        return 0.50
    if risk >= 0.65 and review.chop_risk_score >= 0.60 and review.false_breakout_risk >= 0.60:
        return 0.75
    return 1.0


def policy_htf_balanced_scaler(review: AiReview) -> float:
    # Uses 4h context as a guardrail: aligned high-timeframe trend protects winners;
    # conflicting high-timeframe trend allows stronger cuts on weak 1h breakouts.
    if review.htf_signal_alignment == "aligned" and review.htf_alignment_score >= 0.65:
        if review.false_breakout_risk >= 0.85 and review.trend_continuation_score <= 0.20 and review.confidence >= 0.70:
            return 0.75
        return 1.0
    if review.htf_signal_alignment == "conflict" and review.htf_alignment_score <= 0.35:
        if review.false_breakout_risk >= 0.75 and review.chop_risk_score >= 0.65 and review.confidence >= 0.65:
            return 0.50
        if review.false_breakout_risk >= 0.65 and review.trend_continuation_score <= 0.35 and review.confidence >= 0.60:
            return 0.75
    return policy_strict_false_breakout_reducer(review)


def policy_htf_extreme_false_breakout_quarter(review: AiReview) -> float:
    # Current best 4h-aware candidate on the 128-call Flash Thinking sample.
    # It protects any 4h-aligned trend trade, and only cuts to 25% size when the
    # higher timeframe is not aligned and DeepSeek flags a very high false-breakout risk.
    htf_protects = review.htf_signal_alignment == "aligned" and review.htf_alignment_score >= 0.55
    if htf_protects:
        return 1.0
    if (
        review.false_breakout_risk >= 0.80
        and review.chop_risk_score >= 0.50
        and review.confidence >= 0.60
    ):
        return 0.25
    return 1.0


def policy_htf_graded_risk_scaler(review: AiReview) -> float:
    # Combined 4h + graded sizing policy:
    # 4h aligned protects trend trades; neutral/conflict lets AI risk scores reduce size.
    risk = max(review.chop_risk_score, review.false_breakout_risk)
    if review.htf_signal_alignment == "aligned" and review.htf_alignment_score >= 0.55:
        if risk >= 0.90 and review.false_breakout_risk >= 0.80 and review.confidence >= 0.70:
            return 0.50
        if risk >= 0.75 and review.false_breakout_risk >= 0.70 and review.confidence >= 0.65:
            return 0.75
        return 1.0
    if review.htf_signal_alignment == "conflict" and review.htf_alignment_score <= 0.35:
        if risk >= 0.75 and review.confidence >= 0.60:
            return 0.25
        if risk >= 0.50 and review.confidence >= 0.55:
            return 0.50
        return 0.75
    if risk >= 0.85 and review.confidence >= 0.60:
        return 0.25
    if risk >= 0.75 and review.confidence >= 0.60:
        return 0.50
    if risk >= 0.60 and review.confidence >= 0.55:
        return 0.75
    return 1.0


def policy_controlled_raw_advice_interpreter(review: AiReview) -> float:
    # Uses DeepSeek's raw allow/reduce/block as an input, but execution remains
    # bounded by local rules. Block is converted to small size except for true
    # event-risk conditions, which are unavailable in the historical no-news test.
    risk = max(review.chop_risk_score, review.false_breakout_risk)
    htf_aligned = review.htf_signal_alignment == "aligned" and review.htf_alignment_score >= 0.55
    htf_conflict = review.htf_signal_alignment == "conflict" and review.htf_alignment_score <= 0.35

    if review.veto_action == "allow":
        if htf_conflict and risk >= 0.75 and review.confidence >= 0.65:
            return 0.75
        return 1.0

    if review.veto_action == "reduce":
        if htf_aligned and risk < 0.90:
            return 1.0
        if risk >= 0.85 and review.confidence >= 0.65:
            return 0.25
        if risk >= 0.70 and review.confidence >= 0.60:
            return 0.50
        return 0.75

    if review.veto_action == "block":
        if review.event_risk_score >= 0.85 and review.confidence >= 0.70:
            return 0.0
        if htf_conflict and review.false_breakout_risk >= 0.85 and review.chop_risk_score >= 0.70 and review.confidence >= 0.65:
            return 0.25
        if htf_aligned:
            return 0.75
        return 0.25

    return 1.0


def policy_raw_advice_4level_guarded(review: AiReview) -> float:
    # Minimal hard-guard interpretation of DeepSeek raw advice.
    # The model chooses allow/reduce/block; local code maps it into fixed,
    # auditable four-level sizing. No free-form model percentages are accepted.
    risk = max(review.chop_risk_score, review.false_breakout_risk, review.event_risk_score)
    if review.veto_action == "allow":
        if risk >= 0.85 and review.confidence >= 0.70:
            return 0.75
        return 1.0
    if review.veto_action == "reduce":
        if risk >= 0.85 and review.confidence >= 0.70:
            return 0.25
        if risk >= 0.70 and review.confidence >= 0.60:
            return 0.50
        return 0.75
    if review.veto_action == "block":
        return 0.0
    return 0.0


def policy_raw_advice_5level_guarded(review: AiReview) -> float:
    # Current execution tiers: full/strong/normal/weak/block = 100%, 75%, 50%, 25%, 0%.
    risk = max(review.chop_risk_score, review.false_breakout_risk, review.event_risk_score)
    if review.veto_action == "allow":
        if risk >= 0.90 and review.confidence >= 0.75:
            return 0.75
        return 1.0
    if review.veto_action == "reduce":
        if risk >= 0.80 and review.confidence >= 0.65:
            return 0.25
        if risk >= 0.70 and review.confidence >= 0.60:
            return 0.50
        return 0.75
    if review.veto_action == "block":
        return 0.0
    return 0.0


def policy_raw_advice_5_trade_levels_plus_block(review: AiReview) -> float:
    # Legacy experiment kept aligned with the current five-tier execution contract.
    risk = max(review.chop_risk_score, review.false_breakout_risk, review.event_risk_score)
    if review.veto_action == "allow":
        if risk >= 0.90 and review.confidence >= 0.75:
            return 0.75
        return 1.0
    if review.veto_action == "reduce":
        if risk >= 0.90 and review.confidence >= 0.75:
            return 0.25
        if risk >= 0.80 and review.confidence >= 0.65:
            return 0.25
        if risk >= 0.70 and review.confidence >= 0.60:
            return 0.50
        return 0.75
    if review.veto_action == "block":
        return 0.0
    return 0.0


def summarize_policy(reviews: list[AiReview], weight_fn: Any, initial_equity: float) -> dict[str, Any]:
    by_period: dict[str, list[AiReview]] = {}
    for review in reviews:
        by_period.setdefault(review.period, []).append(review)

    output: dict[str, Any] = {"all": summarize_policy_items(reviews, weight_fn, initial_equity)}
    for period, items in sorted(by_period.items()):
        output[period] = summarize_policy_items(items, weight_fn, initial_equity)
    return output


def summarize_policy_items(items: list[AiReview], weight_fn: Any, initial_equity: float) -> dict[str, Any]:
    weighted_pnl = 0.0
    baseline_pnl = 0.0
    blocked_wins = blocked_losses = reduced_wins = reduced_losses = 0
    wins = losses = 0
    for item in items:
        weight = max(0.0, min(1.0, float(weight_fn(item))))
        weighted_pnl += item.baseline_pnl * weight
        baseline_pnl += item.baseline_pnl
        if item.baseline_pnl > 0:
            wins += 1
            if weight == 0:
                blocked_wins += 1
            elif weight < 1:
                reduced_wins += 1
        else:
            losses += 1
            if weight == 0:
                blocked_losses += 1
            elif weight < 1:
                reduced_losses += 1
    replay = replay_policy_equity(items, weight_fn, initial_equity)
    return {
        "sample_count": len(items),
        "baseline_sample_pnl": baseline_pnl,
        "weighted_sample_pnl": weighted_pnl,
        "pnl_delta": weighted_pnl - baseline_pnl,
        "replay_initial_equity": replay["initial_equity"],
        "replay_final_equity": replay["final_equity"],
        "replay_return_pct": replay["return_pct"],
        "replay_max_drawdown_pct": replay["max_drawdown_pct"],
        "replay_profit_factor": replay["profit_factor"],
        "blocked_wins": blocked_wins,
        "blocked_losses": blocked_losses,
        "reduced_wins": reduced_wins,
        "reduced_losses": reduced_losses,
        "win_block_rate_pct": blocked_wins / max(wins, 1) * 100,
        "loss_block_rate_pct": blocked_losses / max(losses, 1) * 100,
    }


def replay_policy_equity(items: list[AiReview], weight_fn: Any, initial_equity: float) -> dict[str, float]:
    baseline_equity = initial_equity
    overlay_equity = initial_equity
    overlay_curve = [overlay_equity]
    overlay_pnls: list[float] = []
    for item in sorted(items, key=lambda review: review.signal_idx):
        baseline_before = max(baseline_equity, 1e-9)
        pnl_ratio = item.baseline_pnl / baseline_before
        baseline_equity += item.baseline_pnl
        weight = max(0.0, min(1.0, float(weight_fn(item))))
        overlay_pnl = overlay_equity * pnl_ratio * weight
        overlay_equity += overlay_pnl
        overlay_pnls.append(overlay_pnl)
        overlay_curve.append(overlay_equity)

    peak = overlay_curve[0]
    max_drawdown = 0.0
    for value in overlay_curve:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, (value - peak) / max(peak, 1e-9))
    wins = [pnl for pnl in overlay_pnls if pnl > 0]
    losses = [pnl for pnl in overlay_pnls if pnl <= 0]
    profit_factor = sum(wins) / abs(sum(losses)) if losses else (999.0 if wins else 0.0)
    return {
        "initial_equity": initial_equity,
        "final_equity": overlay_equity,
        "return_pct": (overlay_equity - initial_equity) / max(initial_equity, 1e-9) * 100,
        "max_drawdown_pct": max_drawdown * 100,
        "profit_factor": profit_factor,
    }


def load_reusable_reviews(path_text: str) -> dict[int, AiReview]:
    if not path_text:
        return {}
    path = Path(path_text)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    reviews: dict[int, AiReview] = {}
    for item in data.get("reviews", []):
        try:
            review = AiReview(**item)
        except TypeError:
            continue
        reviews[review.signal_idx] = review
    return reviews


async def main() -> None:
    args = parse_args()
    app_config = load_config()
    trend_config = build_config(app_config.strategy.trend, args.position_fraction)
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

    baseline, evaluated, df = run_baseline(
        candles=candles,
        config=trend_config,
        split=args.split,
        initial_equity=args.initial_equity,
        fee_rate=args.fee_rate,
        slippage_bps=args.slippage_bps,
        leverage=args.leverage,
    )
    sample = choose_stratified_sample(evaluated, args.max_calls)
    brain = DeepSeekBrain(
        base_url=app_config.ai.base_url,
        model=args.model or app_config.ai.decision_model,
    )
    original_chat_json = brain._chat_json

    async def bounded_chat_json(payload: dict[str, Any], timeout_seconds: int, retries: int) -> dict[str, Any]:
        return await original_chat_json(
            payload,
            timeout_seconds=max(5, args.deepseek_timeout),
            retries=max(1, args.deepseek_retries),
        )

    brain._chat_json = bounded_chat_json  # type: ignore[method-assign]
    cached_reviews = load_reusable_reviews(args.reuse_reviews_from)
    reviews: list[AiReview] = []
    for item in sample:
        cached = cached_reviews.get(item.signal_idx)
        if cached is not None:
            reviews.append(cached)
            continue
        reviews.append(await review_trade(brain, df, trend_config, item, args.deepseek_timeout))
        await asyncio.sleep(0.2)

    result = {
        "created_at": datetime.now(UTC).isoformat(),
        "method": {
            "description": "DeepSeek is called only at baseline technical entry signals. Sample is stratified by period and win/loss. No model memory is used for historical news.",
            "news_mode": "no_authorized_historical_news_archive_available",
            "orderflow_mode": "no_historical_orderflow_archive_available",
            "thinking_mode": "enabled",
            "reasoning_effort": "high",
            "reasoning_content_storage": "sha256_and_length_only",
            "max_calls": args.max_calls,
            "actual_calls": len(reviews),
            "reused_reviews": len([review for review in reviews if review.signal_idx in cached_reviews]),
        },
        "params": {
            "start": args.start,
            "end": args.end,
            "split": args.split,
            "source": args.source,
            "initial_equity": args.initial_equity,
            "leverage": args.leverage,
            "position_fraction": args.position_fraction,
            "fee_rate": args.fee_rate,
            "slippage_bps": args.slippage_bps,
            "trend_config": trend_config.model_dump(),
            "deepseek_model": brain.model,
        },
        "baseline": {
            key: value
            for key, value in baseline.items()
            if key not in {"trade_ledger", "trades", "equity_curve_tail"}
        },
        "baseline_periods": summarize_periods(evaluated),
        "sample_summary": summarize_reviews(reviews),
        "policy_grid": summarize_policy_grid(reviews, args.initial_equity),
        "reviews": [asdict(review) for review in reviews],
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "actual_calls": len(reviews), "baseline": result["baseline"], "sample_summary": result["sample_summary"]}, ensure_ascii=False, indent=2))


def summarize_periods(evaluated: list[EvaluatedTrade]) -> dict[str, Any]:
    periods: dict[str, list[EvaluatedTrade]] = {}
    for item in evaluated:
        periods.setdefault(item.period, []).append(item)
    output: dict[str, Any] = {}
    for period, items in sorted(periods.items()):
        wins = [item for item in items if item.trade.pnl > 0]
        losses = [item for item in items if item.trade.pnl <= 0]
        output[period] = {
            "trades": len(items),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": len(wins) / max(len(items), 1) * 100,
            "pnl": sum(item.trade.pnl for item in items),
        }
    return output


if __name__ == "__main__":
    asyncio.run(main())
