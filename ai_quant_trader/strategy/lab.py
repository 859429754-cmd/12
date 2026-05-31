from __future__ import annotations

import ast
import hashlib
import itertools
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

import pandas as pd

from ai_quant_trader.core.models import PositionSnapshot, SignalAction, StrategySignal, TrendStrategyConfig
from ai_quant_trader.strategy.trend import TrendStrategy


StrategyCategory = Literal["trend", "range", "grid", "risk_filter", "ai", "research"]
STRATEGY_LAB_DIR = Path("data/strategy_lab")
ACTIVE_STRATEGY_PATH = STRATEGY_LAB_DIR / "active.json"
MIN_AI_OVERLAY_SCORE_IMPROVEMENT = 0.25

ALLOWED_ACTIONS = {
    "LONG": SignalAction.LONG,
    "SHORT": SignalAction.SHORT,
    "EXIT_LONG": SignalAction.EXIT_LONG,
    "EXIT_SHORT": SignalAction.EXIT_SHORT,
    "HOLD": SignalAction.HOLD,
    "long": SignalAction.LONG,
    "short": SignalAction.SHORT,
    "exit_long": SignalAction.EXIT_LONG,
    "exit_short": SignalAction.EXIT_SHORT,
    "hold": SignalAction.HOLD,
}

DENIED_NAMES = {"open", "eval", "exec", "compile", "input", "__import__"}
DENIED_ATTRS = {
    "system",
    "popen",
    "remove",
    "unlink",
    "rmdir",
    "rename",
    "replace",
    "chmod",
    "chown",
    "kill",
    "terminate",
}


@dataclass
class BacktestTrade:
    side: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    return_pct: float
    fee_paid: float = 0.0
    slippage_paid: float = 0.0
    exit_reason: str = "signal_exit"
    stop_loss_price: float | None = None
    max_adverse_excursion: float = 0.0
    max_adverse_excursion_pct: float = 0.0
    intrabar_path: str = ""
    requested_qty: float | None = None
    filled_qty: float | None = None
    fill_ratio: float = 1.0
    funding_paid: float = 0.0
    holding_bars: int = 0


@dataclass(frozen=True)
class BacktestCostModel:
    taker_fee_rate: float = 0.0006
    base_slippage_bps: float = 2.0
    volatility_slippage_factor: float = 0.08
    low_liquidity_slippage_bps: float = 4.0
    max_dynamic_slippage_bps: float = 35.0
    funding_rate_per_8h: float = 0.0
    min_order_qty: float = 0.0
    max_volume_participation: float = 1.0

    @classmethod
    def from_inputs(
        cls,
        fee_rate: float,
        slippage_bps: float,
        *,
        funding_rate_per_8h: float = 0.0,
        min_order_qty: float = 0.0,
        max_volume_participation: float = 1.0,
    ) -> "BacktestCostModel":
        return cls(
            taker_fee_rate=max(float(fee_rate), 0.0),
            base_slippage_bps=max(float(slippage_bps), 0.0),
            funding_rate_per_8h=max(float(funding_rate_per_8h), 0.0),
            min_order_qty=max(float(min_order_qty), 0.0),
            max_volume_participation=min(max(float(max_volume_participation), 0.0), 1.0),
        )

    def slippage(self, price: float, row: pd.Series) -> tuple[float, float]:
        if self.base_slippage_bps <= 0:
            return 0.0, 0.0
        high = float(row.get("high") or price)
        low = float(row.get("low") or price)
        volume = float(row.get("volume") or 0.0)
        vma = float(row.get("vma_20") or row.get("vma_20_filter") or 0.0)
        volatility_bps = max((high - low) / max(price, 1e-9) * 10_000, 0.0)
        dynamic_bps = volatility_bps * self.volatility_slippage_factor
        if vma > 0 and volume > 0 and volume < vma * 0.75:
            dynamic_bps += self.low_liquidity_slippage_bps
        total_bps = min(self.base_slippage_bps + dynamic_bps, self.max_dynamic_slippage_bps)
        return price * total_bps / 10_000, total_bps

    def fill_qty(self, requested_qty: float, row: pd.Series) -> tuple[float, float, str | None]:
        requested = max(float(requested_qty), 0.0)
        if requested <= 0:
            return 0.0, 0.0, "zero_requested_qty"
        volume = max(float(row.get("volume") or 0.0), 0.0)
        volume_cap = requested if self.max_volume_participation >= 1.0 or volume <= 0 else volume * self.max_volume_participation
        filled = min(requested, volume_cap)
        if self.min_order_qty > 0 and filled < self.min_order_qty:
            return 0.0, filled / requested, "below_min_order_qty"
        return filled, filled / requested, None

    def funding_cost(self, entry_price: float, qty: float, holding_bars: int, timeframe: str) -> float:
        if self.funding_rate_per_8h <= 0 or qty <= 0 or holding_bars <= 0:
            return 0.0
        hours = holding_bars * timeframe_hours(timeframe)
        intervals = hours / 8.0
        return abs(entry_price * qty) * self.funding_rate_per_8h * intervals


def timeframe_hours(timeframe: str) -> float:
    raw = str(timeframe or "").strip()
    value = raw.lower()
    try:
        if raw.endswith("M"):
            return max(float(raw[:-1]) * 24.0 * 30, 1 / 60)
        if value.endswith("m"):
            return max(float(value[:-1]) / 60.0, 1 / 60)
        if value.endswith("h"):
            return max(float(value[:-1]), 1 / 60)
        if value.endswith("d"):
            return max(float(value[:-1]) * 24.0, 1 / 60)
        if value.endswith("w"):
            return max(float(value[:-1]) * 24.0 * 7, 1 / 60)
    except ValueError:
        return 1.0
    return 1.0


@dataclass(frozen=True)
class IntrabarExit:
    reason: str
    price: float


def intrabar_path_labels(row: pd.Series) -> list[str]:
    open_price = float(row.get("open") or row.get("close") or 0.0)
    close_price = float(row.get("close") or open_price)
    return ["open", "low", "high", "close"] if close_price >= open_price else ["open", "high", "low", "close"]


def pessimistic_intrabar_exit(
    side: str,
    row: pd.Series,
    stop_loss_price: float | None,
    take_profit_price: float | None = None,
) -> IntrabarExit | None:
    if stop_loss_price is None and take_profit_price is None:
        return None
    high = float(row.get("high") or row.get("close") or 0.0)
    low = float(row.get("low") or row.get("close") or 0.0)
    stop_touched = False
    take_profit_touched = False
    if side == "long":
        stop_touched = stop_loss_price is not None and low <= stop_loss_price
        take_profit_touched = take_profit_price is not None and high >= take_profit_price
    elif side == "short":
        stop_touched = stop_loss_price is not None and high >= stop_loss_price
        take_profit_touched = take_profit_price is not None and low <= take_profit_price
    if stop_touched and take_profit_touched:
        return IntrabarExit("atr_stop", float(stop_loss_price))
    if stop_touched:
        return IntrabarExit("atr_stop", float(stop_loss_price))
    if take_profit_touched:
        return IntrabarExit("take_profit", float(take_profit_price))
    return None


def adverse_excursion(side: str, row: pd.Series, entry_price: float, qty: float) -> tuple[float, float]:
    if side == "long":
        low = float(row.get("low") or row.get("close") or entry_price)
        adverse = (low - entry_price) * qty
        adverse_pct = (low - entry_price) / max(entry_price, 1e-9)
    elif side == "short":
        high = float(row.get("high") or row.get("close") or entry_price)
        adverse = (entry_price - high) * qty
        adverse_pct = (entry_price - high) / max(entry_price, 1e-9)
    else:
        return 0.0, 0.0
    return adverse, adverse_pct


class StrategyCodeError(ValueError):
    pass


def save_strategy_code(
    name: str,
    code: str,
    description: str = "",
    category: StrategyCategory = "research",
) -> dict[str, Any]:
    validation = validate_strategy_code(code)
    STRATEGY_LAB_DIR.mkdir(parents=True, exist_ok=True)
    clean_name = "".join(ch for ch in name.strip() if ch.isalnum() or ch in {"_", "-", " ", "(", ")", "（", "）"}).strip()
    clean_name = clean_name or "未命名策略"
    category = normalize_category(category)
    strategy_id = hashlib.sha256(f"{clean_name}\n{category}\n{code}".encode("utf-8")).hexdigest()[:12]
    path = STRATEGY_LAB_DIR / f"{strategy_id}.py"
    meta_path = STRATEGY_LAB_DIR / f"{strategy_id}.json"
    path.write_text(code, encoding="utf-8")
    meta = {
        "id": strategy_id,
        "name": clean_name,
        "category": category,
        "category_label": category_label(category),
        "description": description,
        "path": str(path),
        "status": "saved_not_live",
        "validation": validation,
        "note": "策略代码已保存。激活后只替换本地技术信号来源，真实开仓仍必须通过实盘模式、标的授权、AI判断和硬风控。",
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def list_strategy_versions() -> list[dict[str, Any]]:
    if not STRATEGY_LAB_DIR.exists():
        return []
    active = get_active_strategy()
    versions = []
    for meta_path in sorted(STRATEGY_LAB_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if meta_path.name == ACTIVE_STRATEGY_PATH.name:
            continue
        try:
            item = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        item.setdefault("category", "research")
        item.setdefault("category_label", category_label(item["category"]))
        if active and active.get("id") == item.get("id"):
            item["status"] = "active"
        versions.append(item)
    return versions


def get_strategy_meta(strategy_id: str) -> dict[str, Any]:
    meta_path = STRATEGY_LAB_DIR / f"{strategy_id}.json"
    if not meta_path.exists():
        raise StrategyCodeError("策略版本不存在")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.setdefault("category", "research")
    meta.setdefault("category_label", category_label(meta["category"]))
    return meta


def delete_strategy(strategy_id: str) -> None:
    active = get_active_strategy()
    if active and active.get("id") == strategy_id:
        raise StrategyCodeError("当前激活策略不能删除，请先停用再删除")
    meta = get_strategy_meta(strategy_id)
    for path in [Path(meta["path"]), STRATEGY_LAB_DIR / f"{strategy_id}.json"]:
        if path.exists():
            path.unlink()


def activate_strategy(strategy_id: str, symbols: list[str], operator_id: str = "console", live_enabled: bool = True) -> dict[str, Any]:
    meta = get_strategy_meta(strategy_id)
    load_strategy_callable(Path(meta["path"]))
    active = {
        "id": strategy_id,
        "name": meta["name"],
        "category": meta.get("category", "research"),
        "category_label": meta.get("category_label", "研究策略"),
        "path": meta["path"],
        "symbols": symbols,
        "live_enabled": live_enabled,
        "operator_id": operator_id,
        "note": "自定义策略已激活。它只替换本地技术信号来源，不能绕过AI、授权、同方向不加仓和配置的总杠杆上限。",
    }
    STRATEGY_LAB_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE_STRATEGY_PATH.write_text(json.dumps(active, ensure_ascii=False, indent=2), encoding="utf-8")
    return active


def deactivate_strategy() -> None:
    if ACTIVE_STRATEGY_PATH.exists():
        ACTIVE_STRATEGY_PATH.unlink()


def get_active_strategy() -> dict[str, Any] | None:
    if not ACTIVE_STRATEGY_PATH.exists():
        return None
    try:
        return json.loads(ACTIVE_STRATEGY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def validate_strategy_code(code: str) -> dict[str, Any]:
    if len(code.strip()) < 20:
        raise StrategyCodeError("策略代码太短")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise StrategyCodeError(f"语法错误：第 {exc.lineno} 行 {exc.msg}") from exc
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise StrategyCodeError("策略代码暂不允许 import；可直接使用 pd、math 和常用内置函数")
        if isinstance(node, ast.Name) and node.id in DENIED_NAMES:
            raise StrategyCodeError(f"策略代码包含禁止调用：{node.id}")
        if isinstance(node, ast.Attribute) and node.attr in DENIED_ATTRS:
            raise StrategyCodeError(f"策略代码包含高风险属性：{node.attr}")
    names = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    if "generate_signal" not in names and "CustomStrategy" not in names:
        raise StrategyCodeError("策略必须提供 generate_signal 函数，或 CustomStrategy.generate_signal 方法")
    return {"ok": True, "message": "策略代码校验通过"}


def load_strategy_callable(path: Path) -> Callable[[pd.DataFrame, PositionSnapshot, dict[str, Any]], dict[str, Any]]:
    code = path.read_text(encoding="utf-8")
    validate_strategy_code(code)
    namespace: dict[str, Any] = {
        "__name__": "strategy_lab_runtime",
        "pd": pd,
        "math": math,
        "__builtins__": {
            "__build_class__": __build_class__,
            "object": object,
            "len": len,
            "min": min,
            "max": max,
            "sum": sum,
            "abs": abs,
            "round": round,
            "float": float,
            "int": int,
            "bool": bool,
            "str": str,
            "dict": dict,
            "list": list,
            "tuple": tuple,
            "set": set,
            "enumerate": enumerate,
            "zip": zip,
            "range": range,
        },
    }
    exec(compile(code, str(path), "exec"), namespace)  # noqa: S102 - 用户显式保存的策略实验室代码
    if callable(namespace.get("generate_signal")):
        return namespace["generate_signal"]
    cls = namespace.get("CustomStrategy")
    if cls is None:
        raise StrategyCodeError("未找到可运行的策略入口")
    instance = cls()
    method = getattr(instance, "generate_signal", None)
    if not callable(method):
        raise StrategyCodeError("CustomStrategy 缺少 generate_signal 方法")
    return method


def generate_custom_signal(
    strategy_id: str,
    candles: pd.DataFrame,
    position: PositionSnapshot,
    symbol: str,
    timeframe: str,
    equity: float,
) -> StrategySignal:
    meta = get_strategy_meta(strategy_id)
    fn = load_strategy_callable(Path(meta["path"]))
    context = {"symbol": symbol, "timeframe": timeframe, "equity": equity, "category": meta.get("category")}
    raw = fn(candles.copy(), position, context)
    if not isinstance(raw, dict):
        raise StrategyCodeError("generate_signal 必须返回 dict")
    action = ALLOWED_ACTIONS.get(str(raw.get("action", "HOLD")))
    if action is None:
        raise StrategyCodeError("action 必须是 LONG、SHORT、EXIT_LONG、EXIT_SHORT 或 HOLD")
    price = float(candles["close"].iloc[-1]) if len(candles) else 0.0
    return StrategySignal(
        symbol=symbol,
        timeframe=timeframe,
        action=action,
        current_price=float(raw.get("current_price") or price),
        suggested_qty=float(raw.get("suggested_qty") or 0.0),
        signal_strength=max(0.0, min(float(raw.get("signal_strength", 0.5 if action != SignalAction.HOLD else 0.0)), 1.0)),
        technical_evidence={
            "source": "strategy_lab",
            "strategy_id": strategy_id,
            "strategy_category": meta.get("category", "research"),
            "reason": str(raw.get("reason", "")),
            **{k: v for k, v in raw.items() if k not in {"action", "current_price", "suggested_qty", "signal_strength", "reason"}},
        },
    )


def backtest_trend_strategy(
    candles: pd.DataFrame,
    symbol: str,
    timeframe: str,
    config: TrendStrategyConfig,
    initial_equity: float = 10_000.0,
    fee_rate: float = 0.0006,
    slippage_bps: float = 2.0,
    leverage: float = 4.0,
    funding_rate_per_8h: float = 0.0,
    min_order_qty: float = 0.0,
    max_volume_participation: float = 1.0,
) -> dict[str, Any]:
    """趋势策略深度回测。

    固定趋势策略的指标可以一次性向量化计算，不能像自定义策略那样每根K线
    都复制全量窗口重算指标；否则 2022-2026 的 1h 回测会变成分钟级等待。
    """
    cost_model = BacktestCostModel.from_inputs(
        fee_rate,
        slippage_bps,
        funding_rate_per_8h=funding_rate_per_8h,
        min_order_qty=min_order_qty,
        max_volume_participation=max_volume_participation,
    )
    strategy = TrendStrategy(config)
    df = strategy.add_indicators(candles)
    warmup = strategy.warmup_candles()
    kcu = f"KCUe_{config.kc_length}_{config.kc_scalar}"
    kcm = f"KCMe_{config.kc_length}_{config.kc_scalar}"
    kcl = f"KCLe_{config.kc_length}_{config.kc_scalar}"

    def action_at(idx: int, position: PositionSnapshot) -> SignalAction:
        return strategy.evaluate_action_from_indicators(df, idx, position)

    equity = initial_equity
    position = PositionSnapshot(symbol=symbol, qty=0.0, mark_price=0.0)
    side: str | None = None
    entry_price = 0.0
    entry_time = ""
    qty = 0.0
    stop_loss_price: float | None = None
    max_adverse_excursion = 0.0
    max_adverse_excursion_pct = 0.0
    entry_fee_paid = 0.0
    entry_slippage_paid = 0.0
    requested_qty = 0.0
    fill_ratio = 1.0
    holding_bars = 0
    pending_action: SignalAction | None = None
    pending_stop_atr = 0.0
    trades: list[BacktestTrade] = []
    skipped_orders: list[dict[str, Any]] = []
    equity_curve = [equity]

    for idx in range(max(1, warmup), len(df)):
        last = df.iloc[idx]
        close_price = float(last["close"])
        open_price = float(last.get("open") or close_price)
        timestamp = str(last.get("timestamp", idx))
        position.mark_price = close_price
        open_slip, _open_slip_bps = cost_model.slippage(open_price, last)

        if pending_action is not None:
            reversal_entry = pending_action in {SignalAction.LONG, SignalAction.SHORT}
            should_close_existing = (
                side == "long" and pending_action in {SignalAction.EXIT_LONG, SignalAction.SHORT}
                or side == "short" and pending_action in {SignalAction.EXIT_SHORT, SignalAction.LONG}
            )
            if side and should_close_existing:
                funding_paid = cost_model.funding_cost(entry_price, qty, holding_bars, timeframe)
                exit_price = open_price - open_slip if side == "long" else open_price + open_slip
                gross = (exit_price - entry_price) * qty if side == "long" else (entry_price - exit_price) * qty
                exit_fee_paid = abs(exit_price * qty) * cost_model.taker_fee_rate
                fee = entry_fee_paid + exit_fee_paid
                slippage_paid = entry_slippage_paid + open_slip * qty
                pnl = gross - fee - funding_paid
                equity += pnl
                trades.append(
                    BacktestTrade(
                        side=side,
                        entry_time=entry_time,
                        exit_time=timestamp,
                        entry_price=entry_price,
                        exit_price=exit_price,
                        qty=qty,
                        pnl=pnl,
                        return_pct=pnl / max(initial_equity, 1e-9),
                        fee_paid=fee,
                        slippage_paid=slippage_paid,
                        exit_reason="reversal" if reversal_entry else "kc_mid_exit",
                        stop_loss_price=stop_loss_price,
                        max_adverse_excursion=max_adverse_excursion,
                        max_adverse_excursion_pct=max_adverse_excursion_pct * 100,
                        intrabar_path="next_open",
                        requested_qty=requested_qty,
                        filled_qty=qty,
                        fill_ratio=fill_ratio,
                        funding_paid=funding_paid,
                        holding_bars=holding_bars,
                    )
                )
                side = None
                entry_price = 0.0
                entry_time = ""
                qty = 0.0
                stop_loss_price = None
                max_adverse_excursion = 0.0
                max_adverse_excursion_pct = 0.0
                entry_fee_paid = 0.0
                entry_slippage_paid = 0.0
                requested_qty = 0.0
                fill_ratio = 1.0
                holding_bars = 0
                position = PositionSnapshot(symbol=symbol, qty=0.0, mark_price=close_price)

            if side is None and reversal_entry:
                side = "long" if pending_action == SignalAction.LONG else "short"
                entry_price = open_price + open_slip if side == "long" else open_price - open_slip
                entry_time = timestamp
                if pending_stop_atr > 0:
                    stop_loss_price = (
                        entry_price - pending_stop_atr * config.atr_stop_multiple
                        if side == "long"
                        else entry_price + pending_stop_atr * config.atr_stop_multiple
                    )
                else:
                    stop_loss_price = None
                notional = equity * config.position_fraction * leverage
                requested_qty = notional / max(entry_price, 1e-9)
                qty, fill_ratio, skip_reason = cost_model.fill_qty(requested_qty, last)
                if skip_reason:
                    skipped_orders.append(
                        {
                            "timestamp": timestamp,
                            "side": side,
                            "reason": skip_reason,
                            "requested_qty": requested_qty,
                            "fillable_qty": qty,
                            "min_order_qty": cost_model.min_order_qty,
                            "max_volume_participation": cost_model.max_volume_participation,
                        }
                    )
                    side = None
                    entry_price = 0.0
                    entry_time = ""
                    qty = 0.0
                    requested_qty = 0.0
                    fill_ratio = 1.0
                    stop_loss_price = None
                    pending_action = None
                    pending_stop_atr = 0.0
                    continue
                notional = qty * entry_price
                position.side = side
                position.qty = qty if side == "long" else -qty
                entry_fee_paid = notional * cost_model.taker_fee_rate
                entry_slippage_paid = open_slip * qty
                holding_bars = 0
            pending_action = None
            pending_stop_atr = 0.0

        intrabar_exit = pessimistic_intrabar_exit(side or "", last, stop_loss_price)
        if side:
            holding_bars += 1
            adverse, adverse_pct = adverse_excursion(side, last, entry_price, qty)
            max_adverse_excursion = min(max_adverse_excursion, adverse)
            max_adverse_excursion_pct = min(max_adverse_excursion_pct, adverse_pct)

        if side and intrabar_exit is not None:
            funding_paid = cost_model.funding_cost(entry_price, qty, holding_bars, timeframe)
            exit_slip, _exit_slip_bps = cost_model.slippage(intrabar_exit.price, last)
            exit_price = intrabar_exit.price - exit_slip if side == "long" else intrabar_exit.price + exit_slip
            gross = (exit_price - entry_price) * qty if side == "long" else (entry_price - exit_price) * qty
            exit_fee_paid = abs(exit_price * qty) * cost_model.taker_fee_rate
            fee = entry_fee_paid + exit_fee_paid
            slippage_paid = entry_slippage_paid + exit_slip * qty
            pnl = gross - fee - funding_paid
            equity += pnl
            trades.append(
                BacktestTrade(
                    side=side,
                    entry_time=entry_time,
                    exit_time=timestamp,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    qty=qty,
                    pnl=pnl,
                    return_pct=pnl / max(initial_equity, 1e-9),
                    fee_paid=fee,
                    slippage_paid=slippage_paid,
                    exit_reason=intrabar_exit.reason,
                    stop_loss_price=stop_loss_price,
                    max_adverse_excursion=max_adverse_excursion,
                    max_adverse_excursion_pct=max_adverse_excursion_pct * 100,
                    intrabar_path="->".join(intrabar_path_labels(last)),
                    requested_qty=requested_qty,
                    filled_qty=qty,
                    fill_ratio=fill_ratio,
                    funding_paid=funding_paid,
                    holding_bars=holding_bars,
                )
            )
            side = None
            entry_price = 0.0
            entry_time = ""
            qty = 0.0
            stop_loss_price = None
            max_adverse_excursion = 0.0
            max_adverse_excursion_pct = 0.0
            entry_fee_paid = 0.0
            entry_slippage_paid = 0.0
            requested_qty = 0.0
            fill_ratio = 1.0
            holding_bars = 0
            position = PositionSnapshot(symbol=symbol, qty=0.0, mark_price=close_price)

        action = action_at(idx, position)
        if (
            side is None and action in {SignalAction.LONG, SignalAction.SHORT}
            or side == "long" and action in {SignalAction.EXIT_LONG, SignalAction.SHORT}
            or side == "short" and action in {SignalAction.EXIT_SHORT, SignalAction.LONG}
        ):
            pending_action = action
            pending_stop_atr = float(last["atr"]) if not math.isnan(float(last["atr"])) else 0.0
        equity_curve.append(equity)

    if side:
        funding_paid = cost_model.funding_cost(entry_price, qty, max(holding_bars, 1), timeframe)
        last = df.iloc[-1]
        price = float(last["close"])
        timestamp = str(last.get("timestamp", len(df) - 1))
        slip, _slip_bps = cost_model.slippage(price, last)
        exit_price = price - slip if side == "long" else price + slip
        gross = (exit_price - entry_price) * qty if side == "long" else (entry_price - exit_price) * qty
        exit_fee_paid = abs(exit_price * qty) * cost_model.taker_fee_rate
        fee = entry_fee_paid + exit_fee_paid
        slippage_paid = entry_slippage_paid + slip * qty
        pnl = gross - fee - funding_paid
        equity += pnl
        trades.append(
            BacktestTrade(
                side=side,
                entry_time=entry_time,
                exit_time=timestamp,
                entry_price=entry_price,
                exit_price=exit_price,
                qty=qty,
                pnl=pnl,
                return_pct=pnl / max(initial_equity, 1e-9),
                fee_paid=fee,
                slippage_paid=slippage_paid,
                exit_reason="end_of_backtest",
                stop_loss_price=stop_loss_price,
                max_adverse_excursion=max_adverse_excursion,
                max_adverse_excursion_pct=max_adverse_excursion_pct * 100,
                intrabar_path="end",
                requested_qty=requested_qty,
                filled_qty=qty,
                fill_ratio=fill_ratio,
                funding_paid=funding_paid,
                holding_bars=max(holding_bars, 1),
            )
        )
        equity_curve.append(equity)

    return _summarize_backtest(
        symbol=symbol,
        timeframe=timeframe,
        initial_equity=initial_equity,
        equity=equity,
        trades=trades,
        equity_curve=equity_curve,
        fee_rate=cost_model.taker_fee_rate,
        slippage_bps=cost_model.base_slippage_bps,
        leverage=leverage,
        funding_rate_per_8h=cost_model.funding_rate_per_8h,
        min_order_qty=cost_model.min_order_qty,
        max_volume_participation=cost_model.max_volume_participation,
        skipped_orders=skipped_orders,
        note="趋势策略毒打回测：强制计入Gate级别taker手续费、基础滑点、波动滑点和低流动性滑点惩罚。",
    )


def backtest_trend_strategy_ai_proxy(
    candles: pd.DataFrame,
    symbol: str,
    timeframe: str,
    config: TrendStrategyConfig,
    initial_equity: float = 10_000.0,
    fee_rate: float = 0.0006,
    slippage_bps: float = 2.0,
    leverage: float = 4.0,
    funding_rate_per_8h: float = 0.0,
    min_order_qty: float = 0.0,
    max_volume_participation: float = 1.0,
) -> dict[str, Any]:
    """低成本 AI 代理回测。

    不调用 DeepSeek，不消耗 API 费用。它用本地可复现规则模拟 AI 对盘面结构
    的过滤：弱突破阻断、极端波动降仓、放量强突破加权。用途是评估“AI 作为
    风控/仓位层”是否改善固定趋势策略，而不是替代真实 AI 实盘判断。
    """
    cost_model = BacktestCostModel.from_inputs(
        fee_rate,
        slippage_bps,
        funding_rate_per_8h=funding_rate_per_8h,
        min_order_qty=min_order_qty,
        max_volume_participation=max_volume_participation,
    )
    baseline = backtest_trend_strategy(
        candles,
        symbol=symbol,
        timeframe=timeframe,
        config=config,
        initial_equity=initial_equity,
        fee_rate=cost_model.taker_fee_rate,
        slippage_bps=cost_model.base_slippage_bps,
        leverage=leverage,
        funding_rate_per_8h=cost_model.funding_rate_per_8h,
        min_order_qty=cost_model.min_order_qty,
        max_volume_participation=cost_model.max_volume_participation,
    )
    strategy = TrendStrategy(config)
    df = strategy.add_indicators(candles)
    warmup = max(strategy.warmup_candles(), 53)
    kcu = f"KCUe_{config.kc_length}_{config.kc_scalar}"
    kcm = f"KCMe_{config.kc_length}_{config.kc_scalar}"
    kcl = f"KCLe_{config.kc_length}_{config.kc_scalar}"
    candidate_map = {
        idx: side
        for idx in range(max(1, warmup), len(df))
        if (side := _trend_candidate_side(df, idx, config, kcu, kcl)) is not None
    }

    equity = initial_equity
    position = PositionSnapshot(symbol=symbol, qty=0.0, mark_price=0.0)
    side: str | None = None
    entry_price = 0.0
    entry_time = ""
    qty = 0.0
    stop_loss_price: float | None = None
    max_adverse_excursion = 0.0
    max_adverse_excursion_pct = 0.0
    entry_fee_paid = 0.0
    entry_slippage_paid = 0.0
    trades: list[BacktestTrade] = []
    equity_curve = [equity]
    stats = {"趋势候选点": len(candidate_map), "AI允许": 0, "AI降仓": 0, "AI阻断": 0, "强突破加权": 0, "候选不匹配阻断": 0}

    for idx in range(max(1, warmup), len(df)):
        last = df.iloc[idx]
        price = float(last["close"])
        timestamp = str(last.get("timestamp", idx))
        position.mark_price = price
        if side is None and idx not in candidate_map:
            equity_curve.append(equity)
            continue
        action, multiplier, proxy_label = _trend_ai_proxy_action(df, idx, position, config, kcu, kcm, kcl)
        if side is None or action in {SignalAction.LONG, SignalAction.SHORT}:
            normalized_label = {"允许": "AI允许", "降仓": "AI降仓", "阻断": "AI阻断"}.get(proxy_label, proxy_label)
            stats[normalized_label] = stats.get(normalized_label, 0) + 1
        slip, _slip_bps = cost_model.slippage(price, last)

        if side is None and action in {SignalAction.LONG, SignalAction.SHORT}:
            candidate = candidate_map.get(idx)
            wanted_side = "long" if action == SignalAction.LONG else "short"
            if candidate != wanted_side:
                stats["候选不匹配阻断"] += 1
                equity_curve.append(equity)
                continue
            side = "long" if action == SignalAction.LONG else "short"
            entry_price = price + slip if side == "long" else price - slip
            entry_time = timestamp
            atr_value = float(last["atr"]) if not math.isnan(float(last["atr"])) else 0.0
            if atr_value > 0:
                stop_loss_price = (
                    entry_price - atr_value * config.atr_stop_multiple
                    if side == "long"
                    else entry_price + atr_value * config.atr_stop_multiple
                )
            else:
                stop_loss_price = None
            notional = equity * config.position_fraction * leverage * multiplier
            qty = notional / max(entry_price, 1e-9)
            position.side = side
            position.qty = qty if side == "long" else -qty
            entry_fee_paid = notional * cost_model.taker_fee_rate
            entry_slippage_paid = slip * qty
            equity_curve.append(equity)
            continue

        intrabar_exit = pessimistic_intrabar_exit(side or "", last, stop_loss_price)
        if side:
            adverse, adverse_pct = adverse_excursion(side, last, entry_price, qty)
            max_adverse_excursion = min(max_adverse_excursion, adverse)
            max_adverse_excursion_pct = min(max_adverse_excursion_pct, adverse_pct)

        should_exit = (
            side == "long" and action == SignalAction.EXIT_LONG
            or side == "short" and action == SignalAction.EXIT_SHORT
            or side == "long" and action == SignalAction.SHORT
            or side == "short" and action == SignalAction.LONG
            or intrabar_exit is not None
        )
        if side and should_exit:
            if intrabar_exit is not None:
                exit_price = intrabar_exit.price - slip if side == "long" else intrabar_exit.price + slip
                exit_reason = intrabar_exit.reason
            else:
                exit_price = price - slip if side == "long" else price + slip
                exit_reason = "reversal" if action in {SignalAction.LONG, SignalAction.SHORT} else "kc_mid_exit"
            gross = (exit_price - entry_price) * qty if side == "long" else (entry_price - exit_price) * qty
            exit_fee_paid = abs(exit_price * qty) * cost_model.taker_fee_rate
            fee = entry_fee_paid + exit_fee_paid
            slippage_paid = entry_slippage_paid + slip * qty
            pnl = gross - fee
            equity += pnl
            trades.append(
                BacktestTrade(
                    side=side,
                    entry_time=entry_time,
                    exit_time=timestamp,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    qty=qty,
                    pnl=pnl,
                    return_pct=pnl / max(initial_equity, 1e-9),
                    fee_paid=fee,
                    slippage_paid=slippage_paid,
                    exit_reason=exit_reason,
                    stop_loss_price=stop_loss_price,
                    max_adverse_excursion=max_adverse_excursion,
                    max_adverse_excursion_pct=max_adverse_excursion_pct * 100,
                    intrabar_path="->".join(intrabar_path_labels(last)),
                )
            )
            side = None
            entry_price = 0.0
            entry_time = ""
            qty = 0.0
            stop_loss_price = None
            max_adverse_excursion = 0.0
            max_adverse_excursion_pct = 0.0
            entry_fee_paid = 0.0
            entry_slippage_paid = 0.0
            position = PositionSnapshot(symbol=symbol, qty=0.0, mark_price=price)
            if action in {SignalAction.LONG, SignalAction.SHORT} and exit_reason == "reversal":
                side = "long" if action == SignalAction.LONG else "short"
                entry_price = price + slip if side == "long" else price - slip
                entry_time = timestamp
                atr_value = float(last["atr"]) if not math.isnan(float(last["atr"])) else 0.0
                if atr_value > 0:
                    stop_loss_price = (
                        entry_price - atr_value * config.atr_stop_multiple
                        if side == "long"
                        else entry_price + atr_value * config.atr_stop_multiple
                    )
                else:
                    stop_loss_price = None
                notional = equity * config.position_fraction * leverage * multiplier
                qty = notional / max(entry_price, 1e-9)
                position.side = side
                position.qty = qty if side == "long" else -qty
                entry_fee_paid = notional * cost_model.taker_fee_rate
                entry_slippage_paid = slip * qty
        equity_curve.append(equity)

    if side:
        last = df.iloc[-1]
        price = float(last["close"])
        timestamp = str(last.get("timestamp", len(df) - 1))
        slip, _slip_bps = cost_model.slippage(price, last)
        exit_price = price - slip if side == "long" else price + slip
        gross = (exit_price - entry_price) * qty if side == "long" else (entry_price - exit_price) * qty
        exit_fee_paid = abs(exit_price * qty) * cost_model.taker_fee_rate
        fee = entry_fee_paid + exit_fee_paid
        slippage_paid = entry_slippage_paid + slip * qty
        pnl = gross - fee
        equity += pnl
        trades.append(
            BacktestTrade(
                side=side,
                entry_time=entry_time,
                exit_time=timestamp,
                entry_price=entry_price,
                exit_price=exit_price,
                qty=qty,
                pnl=pnl,
                return_pct=pnl / max(initial_equity, 1e-9),
                fee_paid=fee,
                slippage_paid=slippage_paid,
                exit_reason="end_of_backtest",
                stop_loss_price=stop_loss_price,
                max_adverse_excursion=max_adverse_excursion,
                max_adverse_excursion_pct=max_adverse_excursion_pct * 100,
                intrabar_path="end",
            )
        )
        equity_curve.append(equity)

    result = _summarize_backtest(
        symbol=symbol,
        timeframe=timeframe,
        initial_equity=initial_equity,
        equity=equity,
        trades=trades,
        equity_curve=equity_curve,
        fee_rate=cost_model.taker_fee_rate,
        slippage_bps=cost_model.base_slippage_bps,
        leverage=leverage,
        note="趋势策略候选+AI终裁代理回测：第一级复用当前 TrendStrategy 合同生成候选点；第二级AI代理只对这些候选点做允许、降仓、阻断，不调用DeepSeek。",
    )
    result["ai_proxy_enabled"] = True
    result["ai_proxy_stats"] = stats
    result["baseline"] = {
        "total_return_pct": baseline["total_return_pct"],
        "max_drawdown_pct": baseline["max_drawdown_pct"],
        "trade_count": baseline["trade_count"],
        "win_rate_pct": baseline["win_rate_pct"],
        "profit_factor": baseline["profit_factor"],
    }
    result["improvement"] = _ai_proxy_improvement(result, baseline)
    result["ai_overlay_score"] = _ai_overlay_score(result)
    result["baseline_overlay_score"] = _ai_overlay_score(baseline)
    result["ai_guard_applied"] = False
    result["ai_symbol_recommendation"] = "AI代理通过样本内防负优化门槛，可以继续观察；实盘前仍需样本外和走步验证。"
    if _is_negative_ai_overlay(result, baseline):
        guarded = dict(baseline)
        guarded["ai_proxy_enabled"] = True
        guarded["ai_guard_applied"] = True
        guarded["ai_proxy_stats"] = stats
        guarded["baseline"] = result["baseline"]
        guarded["raw_ai_proxy"] = {
            "total_return_pct": result["total_return_pct"],
            "max_drawdown_pct": result["max_drawdown_pct"],
            "trade_count": result["trade_count"],
            "win_rate_pct": result["win_rate_pct"],
            "profit_factor": result["profit_factor"],
            "cost_model": result.get("cost_model", {}),
            "trade_ledger": result.get("trade_ledger", []),
            "trades": result.get("trades", []),
            "equity_curve_tail": result.get("equity_curve_tail", []),
        }
        guarded["improvement"] = result["improvement"]
        guarded["ai_overlay_score"] = result["ai_overlay_score"]
        guarded["baseline_overlay_score"] = result["baseline_overlay_score"]
        guarded["ai_symbol_recommendation"] = "AI代理对该标的出现负优化，本次回测已自动回退到基准趋势策略；实盘建议关闭该标的AI过滤或降低AI权重。"
        guarded["note"] = "防负优化保护已触发：AI代理回测劣于基准趋势策略，最终结果回退为基准策略；raw_ai_proxy保留原始AI代理结果供诊断。"
        return guarded
    return result


def _ai_proxy_improvement(result: dict[str, Any], baseline: dict[str, Any]) -> dict[str, float]:
    return {
        "收益率变化": result["total_return_pct"] - baseline["total_return_pct"],
        "最大回撤变化": result["max_drawdown_pct"] - baseline["max_drawdown_pct"],
        "交易次数变化": result["trade_count"] - baseline["trade_count"],
        "胜率变化": result["win_rate_pct"] - baseline["win_rate_pct"],
        "综合评分变化": _ai_overlay_score(result) - _ai_overlay_score(baseline),
    }


def _ai_overlay_score(result: dict[str, Any]) -> float:
    return (
        float(result.get("total_return_pct") or 0.0)
        + float(result.get("profit_factor") or 0.0) * 4.0
        + float(result.get("max_drawdown_pct") or 0.0) * 0.35
    )


def _is_negative_ai_overlay(result: dict[str, Any], baseline: dict[str, Any]) -> bool:
    score_gap = _ai_overlay_score(result) - _ai_overlay_score(baseline)
    return_gap = float(result.get("total_return_pct") or 0.0) - float(baseline.get("total_return_pct") or 0.0)
    drawdown_gap = float(result.get("max_drawdown_pct") or 0.0) - float(baseline.get("max_drawdown_pct") or 0.0)
    profit_factor_gap = float(result.get("profit_factor") or 0.0) - float(baseline.get("profit_factor") or 0.0)
    if score_gap < MIN_AI_OVERLAY_SCORE_IMPROVEMENT:
        return True
    if return_gap < 0.0 and drawdown_gap <= 0.0:
        return True
    if profit_factor_gap < -0.25 and return_gap <= 0.0:
        return True
    return False


def optimize_trend_parameters(
    candles: pd.DataFrame,
    symbol: str,
    timeframe: str,
    base_config: TrendStrategyConfig,
    initial_equity: float = 10_000.0,
    fee_rate: float = 0.0006,
    slippage_bps: float = 2.0,
    leverage: float = 4.0,
    ema_lengths: list[int] | None = None,
    kc_lengths: list[int] | None = None,
    kc_scalars: list[float] | None = None,
    atr_lengths: list[int] | None = None,
    vma_lengths: list[int] | None = None,
    volume_multiples: list[float] | None = None,
    atr_stop_multiples: list[float] | None = None,
    position_fractions: list[float] | None = None,
    use_ema_filters: list[bool] | None = None,
    use_volume_filters: list[bool] | None = None,
    momentum_filters: list[str] | None = None,
    kdj_lengths: list[int] | None = None,
    validation_ratio: float = 0.3,
    min_trades: int = 20,
    max_candidates: int = 120,
    top_n: int = 10,
) -> dict[str, Any]:
    if len(candles) < 300:
        raise StrategyCodeError("K线数量不足，参数寻优至少需要 300 根K线")
    ema_lengths = ema_lengths or [55, 89, 100, 144]
    kc_lengths = kc_lengths or [20]
    kc_scalars = kc_scalars or [2.0, 2.4, 2.8, 3.2]
    atr_lengths = atr_lengths or [14]
    vma_lengths = vma_lengths or [20]
    volume_multiples = volume_multiples or [2.0, 2.2, 2.5, 2.8, 3.0]
    atr_stop_multiples = atr_stop_multiples or [1.5, 2.0, 2.5, 3.0]
    position_fractions = position_fractions or [base_config.position_fraction]
    use_ema_filters = use_ema_filters or [base_config.use_ema_filter]
    use_volume_filters = use_volume_filters or [base_config.use_volume_filter]
    momentum_filters = momentum_filters or [base_config.momentum_filter]
    momentum_filters = [item for item in momentum_filters if item in {"none", "kdj"}]
    if not momentum_filters:
        momentum_filters = [base_config.momentum_filter]
    kdj_lengths = kdj_lengths or [base_config.kdj_length]
    validation_ratio = min(max(float(validation_ratio), 0.15), 0.5)
    warmup = max(base_config.ema_length, base_config.kc_length, base_config.vma_length, base_config.atr_length) + 10
    split_idx = max(int(len(candles) * (1 - validation_ratio)), warmup + 50)
    if split_idx >= len(candles) - 80:
        split_idx = len(candles) - 80
    train = candles.iloc[:split_idx].reset_index(drop=True)
    validation = candles.iloc[max(0, split_idx - warmup) :].reset_index(drop=True)
    baseline = backtest_trend_strategy(
        candles,
        symbol=symbol,
        timeframe=timeframe,
        config=base_config,
        initial_equity=initial_equity,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
        leverage=leverage,
    )

    raw_candidates = [
        (
            ema_len,
            kc_len,
            kc_scalar,
            atr_len,
            vma_len,
            volume_multiple,
            atr_stop_multiple,
            position_fraction,
            use_ema,
            use_volume,
            momentum_filter,
            kdj_len if momentum_filter == "kdj" else base_config.kdj_length,
        )
        for (
            ema_len,
            kc_len,
            kc_scalar,
            atr_len,
            vma_len,
            volume_multiple,
            atr_stop_multiple,
            position_fraction,
            use_ema,
            use_volume,
        ) in itertools.product(
            ema_lengths,
            kc_lengths,
            kc_scalars,
            atr_lengths,
            vma_lengths,
            volume_multiples,
            atr_stop_multiples,
            position_fractions,
            use_ema_filters,
            use_volume_filters,
        )
        for momentum_filter in momentum_filters
        for kdj_len in (kdj_lengths if momentum_filter == "kdj" else [base_config.kdj_length])
    ]
    base_tuple = (
        base_config.ema_length,
        base_config.kc_length,
        base_config.kc_scalar,
        base_config.atr_length,
        base_config.vma_length,
        base_config.volume_multiple,
        base_config.atr_stop_multiple,
        base_config.position_fraction,
        base_config.use_ema_filter,
        base_config.use_volume_filter,
        base_config.momentum_filter,
        base_config.kdj_length,
    )

    def distance(candidate: tuple[int, int, float, int, int, float, float, float, bool, bool, str, int]) -> float:
        (
            ema_len,
            kc_len,
            kc_scalar,
            atr_len,
            vma_len,
            volume_multiple,
            atr_stop_multiple,
            position_fraction,
            use_ema,
            use_volume,
            momentum_filter,
            kdj_len,
        ) = candidate
        return (
            abs(ema_len - base_config.ema_length) / max(base_config.ema_length, 1)
            + abs(kc_len - base_config.kc_length) / max(base_config.kc_length, 1)
            + abs(kc_scalar - base_config.kc_scalar)
            + abs(atr_len - base_config.atr_length) / max(base_config.atr_length, 1)
            + abs(vma_len - base_config.vma_length) / max(base_config.vma_length, 1)
            + abs(volume_multiple - base_config.volume_multiple)
            + abs(atr_stop_multiple - base_config.atr_stop_multiple)
            + abs(position_fraction - base_config.position_fraction)
            + (0 if use_ema == base_config.use_ema_filter else 1)
            + (0 if use_volume == base_config.use_volume_filter else 1)
            + (0 if momentum_filter == base_config.momentum_filter else 1)
            + abs(kdj_len - base_config.kdj_length) / max(base_config.kdj_length, 1)
        )

    raw_candidates = sorted(set(raw_candidates) | {base_tuple}, key=distance)[: max(1, max_candidates)]
    candidates: list[dict[str, Any]] = []
    for (
        ema_len,
        kc_len,
        kc_scalar,
        atr_len,
        vma_len,
        volume_multiple,
        atr_stop_multiple,
        position_fraction,
        use_ema,
        use_volume,
        momentum_filter,
        kdj_len,
    ) in raw_candidates:
        config = base_config.model_copy(
            update={
                "ema_length": int(ema_len),
                "kc_length": int(kc_len),
                "kc_scalar": float(kc_scalar),
                "atr_length": int(atr_len),
                "vma_length": int(vma_len),
                "volume_multiple": float(volume_multiple),
                "atr_stop_multiple": float(atr_stop_multiple),
                "position_fraction": float(position_fraction),
                "variant": "with_volume" if use_volume else "no_volume",
                "use_ema_filter": bool(use_ema),
                "use_volume_filter": bool(use_volume),
                "momentum_filter": str(momentum_filter),
                "kdj_length": int(kdj_len),
            }
        )
        train_result = backtest_trend_strategy(
            train,
            symbol=symbol,
            timeframe=timeframe,
            config=config,
            initial_equity=initial_equity,
            fee_rate=fee_rate,
            slippage_bps=slippage_bps,
            leverage=leverage,
        )
        validation_result = backtest_trend_strategy(
            validation,
            symbol=symbol,
            timeframe=timeframe,
            config=config,
            initial_equity=initial_equity,
            fee_rate=fee_rate,
            slippage_bps=slippage_bps,
            leverage=leverage,
        )
        score = _parameter_score(train_result, validation_result, min_trades)
        candidates.append(
            {
                "params": {
                    "ema_length": config.ema_length,
                    "kc_length": config.kc_length,
                    "kc_scalar": config.kc_scalar,
                    "vma_length": config.vma_length,
                    "atr_length": config.atr_length,
                    "volume_multiple": config.volume_multiple,
                    "atr_stop_multiple": config.atr_stop_multiple,
                    "position_fraction": config.position_fraction,
                    "variant": config.variant,
                    "use_ema_filter": config.use_ema_filter,
                    "use_volume_filter": config.use_volume_filter,
                    "momentum_filter": config.momentum_filter,
                    "kdj_length": config.kdj_length,
                },
                "score": score,
                "train": _compact_backtest_metrics(train_result),
                "validation": _compact_backtest_metrics(validation_result),
                "warnings": _parameter_warnings(train_result, validation_result, min_trades),
            }
        )
    candidates.sort(key=lambda item: item["score"], reverse=True)
    best = candidates[0] if candidates else None
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "execution_model": "close_signal_next_open_fill_intrabar_stop",
        "data_split": {
            "train_candles": len(train),
            "validation_candles": len(validation),
            "validation_ratio": validation_ratio,
        },
        "baseline": _compact_backtest_metrics(baseline),
        "baseline_params": base_config.model_dump(mode="json"),
        "best": best,
        "candidates": candidates[:top_n],
        "searched_candidates": len(raw_candidates),
        "selection_policy": "按验证集收益、回撤、利润因子、交易次数和训练/验证一致性综合评分；不自动修改实盘参数。",
    }


def _compact_backtest_metrics(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "total_return_pct": result.get("total_return_pct", 0.0),
        "max_drawdown_pct": result.get("max_drawdown_pct", 0.0),
        "trade_count": result.get("trade_count", 0),
        "win_rate_pct": result.get("win_rate_pct", 0.0),
        "profit_factor": result.get("profit_factor", 0.0),
        "final_equity": result.get("final_equity", 0.0),
        "cost_pct_of_initial_equity": (result.get("cost_model") or {}).get("cost_pct_of_initial_equity", 0.0),
    }


def _parameter_score(train: dict[str, Any], validation: dict[str, Any], min_trades: int) -> float:
    train_return = float(train.get("total_return_pct") or 0.0)
    validation_return = float(validation.get("total_return_pct") or 0.0)
    validation_drawdown = float(validation.get("max_drawdown_pct") or 0.0)
    validation_pf = min(float(validation.get("profit_factor") or 0.0), 5.0)
    trades = int(validation.get("trade_count") or 0)
    trade_penalty = max(min_trades - trades, 0) * 2.0
    consistency_penalty = max(train_return - validation_return * 2.5, 0.0) * 0.08 if train_return > 0 else 0.0
    return validation_return + validation_drawdown * 0.8 + validation_pf * 4.0 - trade_penalty - consistency_penalty


def _parameter_warnings(train: dict[str, Any], validation: dict[str, Any], min_trades: int) -> list[str]:
    warnings: list[str] = []
    train_return = float(train.get("total_return_pct") or 0.0)
    validation_return = float(validation.get("total_return_pct") or 0.0)
    validation_trades = int(validation.get("trade_count") or 0)
    if validation_trades < min_trades:
        warnings.append("验证集交易次数不足，样本稳定性弱")
    if train_return > 0 and validation_return < train_return * 0.25:
        warnings.append("训练集和验证集收益差距过大，存在过拟合风险")
    if validation_return <= 0:
        warnings.append("验证集收益非正，不建议采用")
    if float(validation.get("max_drawdown_pct") or 0.0) < -35:
        warnings.append("验证集回撤过深，实盘风险偏高")
    return warnings


def _trend_ai_proxy_action(
    df: pd.DataFrame,
    idx: int,
    position: PositionSnapshot,
    config: TrendStrategyConfig,
    kcu: str,
    kcm: str,
    kcl: str,
) -> tuple[SignalAction, float, str]:
    strategy = TrendStrategy(config)
    action = strategy.evaluate_action_from_indicators(df, idx, position)
    if action in {SignalAction.EXIT_LONG, SignalAction.EXIT_SHORT}:
        return action, 1.0, "允许"
    if action == SignalAction.HOLD:
        return SignalAction.HOLD, 0.0, "允许"

    last = df.iloc[idx]
    prev = df.iloc[idx - 1]
    close = float(last["close"])
    volume = float(last["volume"])
    vma = float(last["vma_20"]) if not math.isnan(float(last["vma_20"])) else 0.0
    atr_value = float(last["atr"]) if not math.isnan(float(last["atr"])) else 0.0
    if vma <= 0 or atr_value <= 0 or any(math.isnan(float(last[col])) for col in [kcu, kcm, kcl]):
        return SignalAction.HOLD, 0.0, "阻断"
    if any(math.isnan(float(prev[col])) for col in [kcu, kcm, kcl]):
        return SignalAction.HOLD, 0.0, "阻断"

    recent = df.iloc[max(0, idx - 48) : idx + 1]
    recent_high = float(recent["high"].max())
    recent_low = float(recent["low"].min())
    range_atr = (recent_high - recent_low) / max(atr_value, 1e-9)
    volume_multiple = volume / vma
    atr_pct = atr_value / max(close, 1e-9)
    if action == SignalAction.LONG:
        breakout_atr = max((close - float(last[kcu])) / atr_value, 0.0)
    else:
        breakout_atr = max((float(last[kcl]) - close) / atr_value, 0.0)

    weak_breakout = breakout_atr < 0.25
    chop_risk = range_atr < 7.0 and breakout_atr < 0.5
    extreme_volatility = atr_pct > 0.065
    if weak_breakout or chop_risk:
        return SignalAction.HOLD, 0.0, "阻断"
    if extreme_volatility:
        return action, 0.25, "降仓"
    if breakout_atr >= 0.75 and volume_multiple >= max(config.volume_multiple * 1.45, 2.0):
        return action, 1.0, "允许"
    return action, 0.75, "降仓"


def _trend_candidate_side(
    df: pd.DataFrame,
    idx: int,
    config: TrendStrategyConfig,
    kcu: str,
    kcl: str,
) -> str | None:
    action = TrendStrategy(config).evaluate_action_from_indicators(
        df,
        idx,
        PositionSnapshot(symbol="", qty=0.0, mark_price=0.0),
    )
    if action == SignalAction.LONG:
        return "long"
    if action == SignalAction.SHORT:
        return "short"
    return None


def backtest_custom_strategy(
    candles: pd.DataFrame,
    symbol: str,
    timeframe: str,
    strategy_id: str,
    initial_equity: float = 10_000.0,
    fee_rate: float = 0.0006,
    slippage_bps: float = 2.0,
    warmup: int = 120,
) -> dict[str, Any]:
    def signal_fn(window: pd.DataFrame, position: PositionSnapshot, equity: float) -> StrategySignal:
        return generate_custom_signal(strategy_id, window, position, symbol, timeframe, equity)

    result = _backtest_signal_function(
        candles,
        symbol=symbol,
        timeframe=timeframe,
        initial_equity=initial_equity,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
        warmup=warmup,
        signal_fn=signal_fn,
        note="策略实验室自定义策略回测，包含手续费和滑点估算。",
    )
    meta = get_strategy_meta(strategy_id)
    result["strategy_id"] = strategy_id
    result["strategy_name"] = meta.get("name")
    result["strategy_category"] = meta.get("category")
    result["strategy_category_label"] = meta.get("category_label")
    return result


def _backtest_signal_function(
    candles: pd.DataFrame,
    symbol: str,
    timeframe: str,
    initial_equity: float,
    fee_rate: float,
    slippage_bps: float,
    warmup: int,
    signal_fn: Callable[[pd.DataFrame, PositionSnapshot, float], StrategySignal],
    note: str,
) -> dict[str, Any]:
    cost_model = BacktestCostModel.from_inputs(fee_rate, slippage_bps)
    equity = initial_equity
    position = PositionSnapshot(symbol=symbol, qty=0.0, mark_price=0.0)
    side: str | None = None
    entry_price = 0.0
    entry_time = ""
    qty = 0.0
    entry_fee_paid = 0.0
    entry_slippage_paid = 0.0
    trades: list[BacktestTrade] = []
    equity_curve = [equity]

    for idx in range(max(1, warmup), len(candles)):
        window = candles.iloc[: idx + 1].copy()
        last = window.iloc[-1]
        price = float(last["close"])
        timestamp = str(last.get("timestamp", idx))
        position.mark_price = price
        signal = signal_fn(window, position, equity)
        slip, _slip_bps = cost_model.slippage(price, last)

        if side is None and signal.action in {SignalAction.LONG, SignalAction.SHORT}:
            side = "long" if signal.action == SignalAction.LONG else "short"
            entry_price = price + slip if side == "long" else price - slip
            entry_time = timestamp
            notional = equity * 0.95
            qty = notional / max(entry_price, 1e-9)
            position.side = "long" if side == "long" else "short"
            position.qty = qty if side == "long" else -qty
            entry_fee_paid = notional * cost_model.taker_fee_rate
            entry_slippage_paid = slip * qty
            equity_curve.append(equity)
            continue

        should_exit = (
            side == "long" and signal.action == SignalAction.EXIT_LONG
            or side == "short" and signal.action == SignalAction.EXIT_SHORT
            or side == "long" and signal.action == SignalAction.SHORT
            or side == "short" and signal.action == SignalAction.LONG
        )
        if side and should_exit:
            exit_price = price - slip if side == "long" else price + slip
            gross = (exit_price - entry_price) * qty if side == "long" else (entry_price - exit_price) * qty
            exit_fee_paid = abs(exit_price * qty) * cost_model.taker_fee_rate
            fee = entry_fee_paid + exit_fee_paid
            slippage_paid = entry_slippage_paid + slip * qty
            pnl = gross - fee
            equity += pnl
            trades.append(
                BacktestTrade(
                    side=side,
                    entry_time=entry_time,
                    exit_time=timestamp,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    qty=qty,
                    pnl=pnl,
                    return_pct=pnl / max(initial_equity, 1e-9),
                    fee_paid=fee,
                    slippage_paid=slippage_paid,
                )
            )
            side = None
            entry_price = 0.0
            entry_time = ""
            qty = 0.0
            entry_fee_paid = 0.0
            entry_slippage_paid = 0.0
            position = PositionSnapshot(symbol=symbol, qty=0.0, mark_price=price)
        equity_curve.append(equity)

    return _summarize_backtest(
        symbol=symbol,
        timeframe=timeframe,
        initial_equity=initial_equity,
        equity=equity,
        trades=trades,
        equity_curve=equity_curve,
        fee_rate=cost_model.taker_fee_rate,
        slippage_bps=cost_model.base_slippage_bps,
        leverage=1.0,
        note=note,
    )


def _summarize_backtest(
    symbol: str,
    timeframe: str,
    initial_equity: float,
    equity: float,
    trades: list[BacktestTrade],
    equity_curve: list[float],
    fee_rate: float,
    slippage_bps: float,
    leverage: float,
    note: str,
    funding_rate_per_8h: float = 0.0,
    min_order_qty: float = 0.0,
    max_volume_participation: float = 1.0,
    skipped_orders: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    total_return = (equity - initial_equity) / max(initial_equity, 1e-9)
    wins = [trade for trade in trades if trade.pnl > 0]
    losses = [trade for trade in trades if trade.pnl <= 0]
    peak = equity_curve[0]
    max_drawdown = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, (value - peak) / max(peak, 1e-9))
    profit_factor = sum(t.pnl for t in wins) / abs(sum(t.pnl for t in losses)) if losses else (999.0 if wins else 0.0)
    total_fee_paid = sum(t.fee_paid for t in trades)
    total_slippage_paid = sum(t.slippage_paid for t in trades)
    total_funding_paid = sum(t.funding_paid for t in trades)
    total_cost_paid = total_fee_paid + total_slippage_paid + total_funding_paid
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "initial_equity": initial_equity,
        "final_equity": equity,
        "total_return_pct": total_return * 100,
        "max_drawdown_pct": max_drawdown * 100,
        "trade_count": len(trades),
        "win_rate_pct": (len(wins) / len(trades) * 100) if trades else 0.0,
        "profit_factor": profit_factor,
        "fee_rate": fee_rate,
        "slippage_bps": slippage_bps,
        "leverage": leverage,
        "execution_model": "close_signal_next_open_fill_intrabar_stop",
        "position_fraction": trades[0].qty * trades[0].entry_price / max(initial_equity * leverage, 1e-9) if trades else None,
        "cost_model": {
            "enabled": True,
            "mode": "taker_fee_dynamic_slippage",
            "taker_fee_rate": fee_rate,
            "base_slippage_bps": slippage_bps,
            "funding_rate_per_8h": funding_rate_per_8h,
            "min_order_qty": min_order_qty,
            "max_volume_participation": max_volume_participation,
            "total_fee_paid": total_fee_paid,
            "total_slippage_paid": total_slippage_paid,
            "total_funding_paid": total_funding_paid,
            "total_cost_paid": total_cost_paid,
            "cost_pct_of_initial_equity": total_cost_paid / max(initial_equity, 1e-9) * 100,
        },
        "skipped_orders": skipped_orders or [],
        "trade_ledger": [trade.__dict__ for trade in trades],
        "trades": [trade.__dict__ for trade in trades],
        "equity_curve_tail": equity_curve[-300:],
        "note": note,
    }


def normalize_category(category: str | None) -> StrategyCategory:
    value = str(category or "research").lower()
    if value in {"trend", "range", "grid", "risk_filter", "ai", "research"}:
        return value  # type: ignore[return-value]
    return "research"


def category_label(category: str | None) -> str:
    return {
        "trend": "趋势策略",
        "range": "震荡策略",
        "grid": "网格策略",
        "risk_filter": "风控过滤器",
        "ai": "AI策略",
        "research": "研究策略",
    }.get(str(category or "research"), "研究策略")
