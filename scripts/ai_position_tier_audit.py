from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


TIER_ORDER = ("block", "weak", "normal", "strong", "full", "unknown")
TIER_SCALE = {"block": 0.0, "weak": 0.25, "normal": 0.5, "strong": 0.75, "full": 1.0}
TERMINAL_FILL_STATUSES = {"filled", "partially_filled"}


@dataclass
class TradeAudit:
    symbol: str
    account_slot: str
    side: str
    tier: str
    scale: float
    entry_client_order_id: str
    entry_price: float
    entry_qty: float
    entry_row_id: int
    exit_client_order_id: str | None = None
    exit_price: float | None = None
    exit_row_id: int | None = None
    actual_pnl_usdt: float | None = None
    baseline_pnl_usdt: float | None = None
    ai_delta_pnl_usdt: float | None = None
    winner_upside_missed_usdt: float = 0.0
    loser_loss_saved_usdt: float = 0.0
    winner_extra_profit_usdt: float = 0.0
    loser_extra_loss_usdt: float = 0.0
    strategy_baseline_notional: float | None = None
    ai_desired_notional: float | None = None
    decision_score: float | None = None
    ai_confidence: float | None = None
    reason: str = ""
    warnings: list[str] = field(default_factory=list)
    shadow_tiers: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def closed(self) -> bool:
        return self.exit_price is not None and self.actual_pnl_usdt is not None


def _load_rows(conn: sqlite3.Connection, table: str, symbol: str | None) -> list[dict[str, Any]]:
    try:
        if symbol:
            rows = conn.execute(
                f"SELECT id, created_at, symbol, payload FROM {table} WHERE symbol = ? ORDER BY id ASC",
                (symbol,),
            ).fetchall()
        else:
            rows = conn.execute(f"SELECT id, created_at, symbol, payload FROM {table} ORDER BY id ASC").fetchall()
    except sqlite3.OperationalError:
        return []
    output = []
    for row_id, created_at, row_symbol, payload in rows:
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            parsed = {}
        output.append({"id": int(row_id), "created_at": created_at, "symbol": row_symbol, "payload": parsed})
    return output


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _order_price(payload: dict[str, Any]) -> float | None:
    order = payload.get("order") if isinstance(payload.get("order"), dict) else {}
    raw = order.get("raw") if isinstance(order.get("raw"), dict) else {}
    for source in (order, raw, payload):
        for key in ("average", "avg_price", "price", "fill_price", "mark_price"):
            value = _as_float(source.get(key))
            if value and value > 0:
                return value
    return None


def _event_qty(payload: dict[str, Any]) -> float | None:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    order = payload.get("order") if isinstance(payload.get("order"), dict) else {}
    raw = order.get("raw") if isinstance(order.get("raw"), dict) else {}
    for source in (payload, order, raw, metadata):
        for key in ("amount", "filled", "filled_amount", "qty", "risk_clipped_qty"):
            value = _as_float(source.get(key))
            if value and value > 0:
                return value
    return None


def _final_fill_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest_by_client: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = row.get("payload") or {}
        status = str(payload.get("status") or "").lower()
        client_order_id = str(payload.get("client_order_id") or "")
        if not client_order_id or status not in TERMINAL_FILL_STATUSES:
            continue
        latest_by_client[client_order_id] = row
    return sorted(latest_by_client.values(), key=lambda item: int(item["id"]))


def _is_entry(payload: dict[str, Any]) -> bool:
    order_type = str(payload.get("order_type") or "").lower()
    return order_type == "market" and not bool(payload.get("reduce_only"))


def _is_exit(payload: dict[str, Any]) -> bool:
    order_type = str(payload.get("order_type") or "").lower()
    return bool(payload.get("reduce_only")) or order_type == "stop_loss"


def _trade_key(payload: dict[str, Any]) -> tuple[str, str]:
    return (str(payload.get("symbol") or ""), str(payload.get("account_slot") or "default"))


def _entry_side(payload: dict[str, Any]) -> str:
    side = str(payload.get("side") or "").lower()
    if side == "buy":
        return "long"
    if side == "sell":
        return "short"
    return "unknown"


def _tier_from_metadata_or_reason(metadata: dict[str, Any], reason: str) -> tuple[str, float]:
    tier = str(metadata.get("risk_position_tier") or "")
    scale = _as_float(metadata.get("risk_position_scale"))
    if tier not in TIER_ORDER:
        reason_lower = reason.lower()
        for candidate in ("weak", "normal", "strong", "full", "block"):
            if f"{candidate}_size" in reason_lower or reason_lower.startswith(candidate):
                tier = candidate
                break
    if tier not in TIER_ORDER:
        tier = "unknown"
    if scale is None:
        scale = TIER_SCALE.get(tier, 0.0)
    return tier, scale


def build_trade_audit(rows: list[dict[str, Any]], account_slot: str | None = None) -> list[TradeAudit]:
    trades: list[TradeAudit] = []
    open_by_key: dict[tuple[str, str], TradeAudit] = {}
    for row in _final_fill_events(rows):
        payload = row.get("payload") or {}
        if account_slot and str(payload.get("account_slot") or "default") != account_slot:
            continue
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        key = _trade_key(payload)
        if _is_entry(payload):
            entry_price = _order_price(payload)
            entry_qty = _event_qty(payload)
            if not entry_price or not entry_qty:
                continue
            reason = str(payload.get("reason") or "")
            tier, scale = _tier_from_metadata_or_reason(metadata, reason)
            trade = TradeAudit(
                symbol=key[0],
                account_slot=key[1],
                side=_entry_side(payload),
                tier=tier,
                scale=scale,
                entry_client_order_id=str(payload.get("client_order_id") or ""),
                entry_price=entry_price,
                entry_qty=entry_qty,
                entry_row_id=int(row["id"]),
                strategy_baseline_notional=_as_float(metadata.get("strategy_baseline_notional")),
                ai_desired_notional=_as_float(metadata.get("ai_desired_notional")),
                decision_score=_as_float(metadata.get("risk_decision_score")),
                ai_confidence=_as_float(metadata.get("ai_confidence")),
                reason=reason,
            )
            if key in open_by_key:
                open_by_key[key].warnings.append("replaced_by_new_entry_before_exit")
            open_by_key[key] = trade
            trades.append(trade)
            continue

        if not _is_exit(payload):
            continue
        trade = open_by_key.get(key)
        if trade is None:
            continue
        exit_price = _order_price(payload)
        if not exit_price:
            continue
        exit_qty = _event_qty(payload) or trade.entry_qty
        qty = min(trade.entry_qty, exit_qty)
        pnl_per_unit = exit_price - trade.entry_price if trade.side == "long" else trade.entry_price - exit_price
        actual_pnl = pnl_per_unit * qty
        baseline_pnl = None
        ai_delta = None
        baseline_notional = _baseline_notional_for_shadow(trade)
        if baseline_notional and baseline_notional > 0 and trade.entry_price > 0:
            baseline_qty = baseline_notional / trade.entry_price
            trade.shadow_tiers = _shadow_tiers(pnl_per_unit, baseline_qty)
            baseline_pnl = pnl_per_unit * baseline_qty
            ai_delta = actual_pnl - baseline_pnl
            if actual_pnl > 0 and baseline_pnl > actual_pnl:
                trade.winner_upside_missed_usdt = baseline_pnl - actual_pnl
            if actual_pnl < 0 and baseline_pnl < actual_pnl:
                trade.loser_loss_saved_usdt = actual_pnl - baseline_pnl
            if actual_pnl > baseline_pnl and actual_pnl > 0:
                trade.winner_extra_profit_usdt = actual_pnl - (baseline_pnl or 0.0)
            if actual_pnl < baseline_pnl and actual_pnl < 0:
                trade.loser_extra_loss_usdt = baseline_pnl - actual_pnl

        trade.exit_client_order_id = str(payload.get("client_order_id") or "")
        trade.exit_price = exit_price
        trade.exit_row_id = int(row["id"])
        trade.actual_pnl_usdt = actual_pnl
        trade.baseline_pnl_usdt = baseline_pnl
        trade.ai_delta_pnl_usdt = ai_delta
        open_by_key.pop(key, None)
    return trades


def _baseline_notional_for_shadow(trade: TradeAudit) -> float | None:
    if trade.strategy_baseline_notional and trade.strategy_baseline_notional > 0:
        return trade.strategy_baseline_notional
    if trade.scale > 0 and trade.entry_price > 0 and trade.entry_qty > 0:
        trade.warnings.append("strategy_baseline_notional_estimated_from_actual_qty_and_tier")
        return trade.entry_price * trade.entry_qty / trade.scale
    return None


def _shadow_tiers(pnl_per_unit: float, baseline_qty: float) -> dict[str, dict[str, float]]:
    output: dict[str, dict[str, float]] = {}
    for tier in ("weak", "normal", "strong", "full"):
        scale = TIER_SCALE[tier]
        qty = baseline_qty * scale
        output[tier] = {
            "scale": scale,
            "qty": qty,
            "pnl_usdt": pnl_per_unit * qty,
        }
    return output


def _empty_tier_stats() -> dict[str, Any]:
    return {
        "entries": 0,
        "closed": 0,
        "open": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": None,
        "total_actual_pnl_usdt": 0.0,
        "total_baseline_pnl_usdt": 0.0,
        "total_ai_delta_pnl_usdt": 0.0,
        "winner_upside_missed_usdt": 0.0,
        "loser_loss_saved_usdt": 0.0,
        "winner_extra_profit_usdt": 0.0,
        "loser_extra_loss_usdt": 0.0,
        "avg_position_scale": None,
        "avg_decision_score": None,
        "avg_ai_confidence": None,
    }


def summarize_trades(trades: list[TradeAudit], min_sample_warning: int = 30) -> dict[str, Any]:
    by_tier = {tier: _empty_tier_stats() for tier in TIER_ORDER}
    overall = _empty_tier_stats()
    scale_values: dict[str, list[float]] = {tier: [] for tier in TIER_ORDER}
    score_values: dict[str, list[float]] = {tier: [] for tier in TIER_ORDER}
    confidence_values: dict[str, list[float]] = {tier: [] for tier in TIER_ORDER}
    overall_scales: list[float] = []
    overall_scores: list[float] = []
    overall_confidences: list[float] = []

    shadow_summary = _empty_shadow_summary()

    for trade in trades:
        tier = trade.tier if trade.tier in by_tier else "unknown"
        stats = by_tier[tier]
        for target in (stats, overall):
            target["entries"] += 1
            target["open"] += 0 if trade.closed else 1
        scale_values[tier].append(trade.scale)
        overall_scales.append(trade.scale)
        if trade.decision_score is not None:
            score_values[tier].append(trade.decision_score)
            overall_scores.append(trade.decision_score)
        if trade.ai_confidence is not None:
            confidence_values[tier].append(trade.ai_confidence)
            overall_confidences.append(trade.ai_confidence)

        if not trade.closed or trade.actual_pnl_usdt is None:
            continue
        _accumulate_shadow_summary(shadow_summary, trade)
        for target in (stats, overall):
            target["closed"] += 1
            if trade.actual_pnl_usdt > 0:
                target["wins"] += 1
            elif trade.actual_pnl_usdt < 0:
                target["losses"] += 1
            target["total_actual_pnl_usdt"] += trade.actual_pnl_usdt
            target["total_baseline_pnl_usdt"] += trade.baseline_pnl_usdt or 0.0
            target["total_ai_delta_pnl_usdt"] += trade.ai_delta_pnl_usdt or 0.0
            target["winner_upside_missed_usdt"] += trade.winner_upside_missed_usdt
            target["loser_loss_saved_usdt"] += trade.loser_loss_saved_usdt
            target["winner_extra_profit_usdt"] += trade.winner_extra_profit_usdt
            target["loser_extra_loss_usdt"] += trade.loser_extra_loss_usdt

    for tier, stats in by_tier.items():
        _finish_stats(stats, scale_values[tier], score_values[tier], confidence_values[tier])
    _finish_stats(overall, overall_scales, overall_scores, overall_confidences)
    _finish_shadow_summary(shadow_summary)
    return {
        "sample_warning": overall["closed"] < min_sample_warning,
        "min_closed_trades_for_reliable_read": min_sample_warning,
        "overall": overall,
        "by_tier": by_tier,
        "shadow_by_tier": shadow_summary,
        "trades": [trade.__dict__ for trade in trades],
    }


def _empty_shadow_summary() -> dict[str, Any]:
    return {
        tier: {
            "closed": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": None,
            "total_pnl_usdt": 0.0,
            "avg_pnl_usdt": None,
            "scale": TIER_SCALE[tier],
        }
        for tier in ("weak", "normal", "strong", "full")
    }


def _accumulate_shadow_summary(summary: dict[str, Any], trade: TradeAudit) -> None:
    for tier, payload in (trade.shadow_tiers or {}).items():
        if tier not in summary:
            continue
        pnl = float(payload.get("pnl_usdt") or 0.0)
        item = summary[tier]
        item["closed"] += 1
        item["total_pnl_usdt"] += pnl
        if pnl > 0:
            item["wins"] += 1
        elif pnl < 0:
            item["losses"] += 1


def _finish_shadow_summary(summary: dict[str, Any]) -> None:
    for item in summary.values():
        closed = int(item["closed"])
        if closed:
            item["win_rate"] = item["wins"] / closed
            item["avg_pnl_usdt"] = item["total_pnl_usdt"] / closed
        for key, value in list(item.items()):
            if isinstance(value, float):
                item[key] = round(value, 8)


def _finish_stats(
    stats: dict[str, Any],
    scale_values: list[float],
    score_values: list[float],
    confidence_values: list[float],
) -> None:
    if stats["closed"]:
        stats["win_rate"] = stats["wins"] / stats["closed"]
    if scale_values:
        stats["avg_position_scale"] = sum(scale_values) / len(scale_values)
    if score_values:
        stats["avg_decision_score"] = sum(score_values) / len(score_values)
    if confidence_values:
        stats["avg_ai_confidence"] = sum(confidence_values) / len(confidence_values)
    for key, value in list(stats.items()):
        if isinstance(value, float):
            stats[key] = round(value, 8)


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# AI Position Tier Audit",
        "",
        "This report compares actual AI-sized trades with the strategy baseline notional saved in order metadata.",
        "Positive `AI delta` means the AI tier sizing improved PnL versus that baseline; negative means it hurt PnL.",
        "",
    ]
    if summary["sample_warning"]:
        lines.extend(
            [
                "> Warning: closed trade sample is below the configured reliability threshold. Treat this as diagnostics, not proof of edge.",
                "",
            ]
        )
    overall = summary["overall"]
    lines.extend(
        [
            "## Overall",
            "",
            f"- Entries: {overall['entries']}",
            f"- Closed: {overall['closed']}",
            f"- Open: {overall['open']}",
            f"- Win rate: {_pct(overall['win_rate'])}",
            f"- Actual PnL: {_money(overall['total_actual_pnl_usdt'])}",
            f"- Baseline PnL: {_money(overall['total_baseline_pnl_usdt'])}",
            f"- AI delta: {_money(overall['total_ai_delta_pnl_usdt'])}",
            f"- Winner upside missed: {_money(overall['winner_upside_missed_usdt'])}",
            f"- Loser loss saved: {_money(overall['loser_loss_saved_usdt'])}",
            "",
            "## By Tier",
            "",
            "| Tier | Entries | Closed | Win rate | Actual PnL | Baseline PnL | AI delta | Missed winner upside | Saved loser loss | Avg scale |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for tier in TIER_ORDER:
        stats = summary["by_tier"][tier]
        lines.append(
            "| "
            + " | ".join(
                [
                    tier,
                    str(stats["entries"]),
                    str(stats["closed"]),
                    _pct(stats["win_rate"]),
                    _money(stats["total_actual_pnl_usdt"]),
                    _money(stats["total_baseline_pnl_usdt"]),
                    _money(stats["total_ai_delta_pnl_usdt"]),
                    _money(stats["winner_upside_missed_usdt"]),
                    _money(stats["loser_loss_saved_usdt"]),
                    _pct(stats["avg_position_scale"]),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Shadow Ledger By Tier",
            "",
            "| Shadow tier | Closed | Win rate | Total PnL | Avg PnL |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for tier in ("weak", "normal", "strong", "full"):
        stats = summary["shadow_by_tier"][tier]
        lines.append(
            "| "
            + " | ".join(
                [
                    tier,
                    str(stats["closed"]),
                    _pct(stats["win_rate"]),
                    _money(stats["total_pnl_usdt"]),
                    _money(stats["avg_pnl_usdt"]),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _money(value: Any) -> str:
    if value is None:
        return "--"
    return f"{float(value):.4f}"


def _pct(value: Any) -> str:
    if value is None:
        return "--"
    return f"{float(value) * 100:.2f}%"


def run_audit(
    db_path: Path,
    symbol: str | None = None,
    account_slot: str | None = None,
    min_sample_warning: int = 30,
) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    try:
        rows = _load_rows(conn, "order_lifecycle", symbol)
    finally:
        conn.close()
    return summarize_trades(build_trade_audit(rows, account_slot=account_slot), min_sample_warning=min_sample_warning)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit AI position tier effects from order lifecycle metadata.")
    parser.add_argument("--db", default="data/trader.sqlite3", help="SQLite database path.")
    parser.add_argument("--symbol", default="ETH/USDT:USDT", help="Optional symbol filter. Use empty string for all symbols.")
    parser.add_argument("--account-slot", default="", help="Optional account slot filter, for example account1 or account2.")
    parser.add_argument("--output-dir", default="reports", help="Directory for JSON and Markdown reports.")
    parser.add_argument("--min-sample-warning", type=int, default=30, help="Closed trade count below this emits a warning.")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"database_not_found:{db_path}")
    symbol = args.symbol or None
    account_slot = args.account_slot or None
    summary = run_audit(db_path, symbol=symbol, account_slot=account_slot, min_sample_warning=args.min_sample_warning)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "ai_position_tier_audit.json"
    md_path = output_dir / "ai_position_tier_audit.md"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render_markdown(summary), encoding="utf-8")

    print(json.dumps({"ok": True, "json": str(json_path), "markdown": str(md_path), "overall": summary["overall"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
