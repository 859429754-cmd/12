from __future__ import annotations

from pathlib import Path

import pytest

from ai_quant_trader.storage.sqlite import SQLiteStore
from scripts.ai_position_tier_audit import build_trade_audit, run_audit, summarize_trades


SYMBOL = "ETH/USDT:USDT"


def make_store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(str(tmp_path / "trader.sqlite3"), str(tmp_path / "audit.jsonl"))


def _entry(
    *,
    client_order_id: str,
    tier: str,
    scale: float,
    side: str,
    qty: float,
    price: float,
    baseline_notional: float,
) -> dict:
    return {
        "client_order_id": client_order_id,
        "symbol": SYMBOL,
        "status": "filled",
        "account_slot": "account1",
        "order_type": "market",
        "side": side,
        "amount": qty,
        "reduce_only": False,
        "gateway_mode": "mock",
        "reason": "pytest_entry",
        "order": {"price": price, "status": "closed"},
        "metadata": {
            "risk_position_tier": tier,
            "risk_position_scale": scale,
            "strategy_baseline_notional": baseline_notional,
            "ai_desired_notional": baseline_notional * scale,
            "risk_decision_score": 0.8,
            "ai_confidence": 0.75,
        },
    }


def _exit(*, client_order_id: str, side: str, qty: float, price: float) -> dict:
    return {
        "client_order_id": client_order_id,
        "symbol": SYMBOL,
        "status": "filled",
        "account_slot": "account1",
        "order_type": "market",
        "side": side,
        "amount": qty,
        "reduce_only": True,
        "gateway_mode": "mock",
        "reason": "pytest_exit",
        "order": {"price": price, "status": "closed"},
        "metadata": {},
    }


def _with_account(payload: dict, account_slot: str) -> dict:
    copied = dict(payload)
    copied["account_slot"] = account_slot
    return copied


def test_ai_position_tier_audit_detects_saved_loss_and_missed_upside(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    try:
        store.insert("order_lifecycle", _entry(client_order_id="weak_loser_e", tier="weak", scale=0.25, side="buy", qty=2.5, price=100, baseline_notional=1000), SYMBOL)
        store.insert("order_lifecycle", _exit(client_order_id="weak_loser_x", side="sell", qty=2.5, price=90), SYMBOL)
        store.insert("order_lifecycle", _entry(client_order_id="weak_winner_e", tier="weak", scale=0.25, side="buy", qty=2.5, price=100, baseline_notional=1000), SYMBOL)
        store.insert("order_lifecycle", _exit(client_order_id="weak_winner_x", side="sell", qty=2.5, price=110), SYMBOL)
        store.insert("order_lifecycle", _entry(client_order_id="full_winner_e", tier="full", scale=1.0, side="buy", qty=10, price=100, baseline_notional=1000), SYMBOL)
        store.insert("order_lifecycle", _exit(client_order_id="full_winner_x", side="sell", qty=10, price=110), SYMBOL)

        summary = run_audit(store.db_path, symbol=SYMBOL, min_sample_warning=3)
    finally:
        store.close()

    assert summary["sample_warning"] is False
    assert summary["overall"]["closed"] == 3
    assert summary["by_tier"]["weak"]["closed"] == 2
    assert summary["by_tier"]["weak"]["loser_loss_saved_usdt"] == pytest.approx(75.0)
    assert summary["by_tier"]["weak"]["winner_upside_missed_usdt"] == pytest.approx(75.0)
    assert summary["by_tier"]["weak"]["total_ai_delta_pnl_usdt"] == pytest.approx(0.0)
    assert summary["by_tier"]["full"]["total_ai_delta_pnl_usdt"] == pytest.approx(0.0)


def test_ai_position_tier_audit_keeps_open_trades_separate() -> None:
    rows = [
        {
            "id": 1,
            "created_at": "2026-01-01 00:00:00",
            "symbol": SYMBOL,
            "payload": _entry(client_order_id="open_e", tier="strong", scale=0.75, side="sell", qty=7.5, price=100, baseline_notional=1000),
        }
    ]

    trades = build_trade_audit(rows)
    summary = summarize_trades(trades, min_sample_warning=1)

    assert summary["overall"]["entries"] == 1
    assert summary["overall"]["open"] == 1
    assert summary["overall"]["closed"] == 0
    assert summary["by_tier"]["strong"]["avg_position_scale"] == pytest.approx(0.75)


def test_ai_position_tier_audit_can_filter_account_slot(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    try:
        store.insert("order_lifecycle", _with_account(_entry(client_order_id="a1_e", tier="weak", scale=0.25, side="buy", qty=2.5, price=100, baseline_notional=1000), "account1"), SYMBOL)
        store.insert("order_lifecycle", _with_account(_exit(client_order_id="a1_x", side="sell", qty=2.5, price=90), "account1"), SYMBOL)
        store.insert("order_lifecycle", _with_account(_entry(client_order_id="a2_e", tier="full", scale=1.0, side="buy", qty=10, price=100, baseline_notional=1000), "account2"), SYMBOL)
        store.insert("order_lifecycle", _with_account(_exit(client_order_id="a2_x", side="sell", qty=10, price=110), "account2"), SYMBOL)

        account1 = run_audit(store.db_path, symbol=SYMBOL, account_slot="account1", min_sample_warning=1)
        account2 = run_audit(store.db_path, symbol=SYMBOL, account_slot="account2", min_sample_warning=1)
    finally:
        store.close()

    assert account1["overall"]["closed"] == 1
    assert account1["by_tier"]["weak"]["loser_loss_saved_usdt"] == pytest.approx(75.0)
    assert account2["overall"]["closed"] == 1
    assert account2["by_tier"]["full"]["total_actual_pnl_usdt"] == pytest.approx(100.0)


def test_ai_position_tier_audit_recovers_legacy_tier_from_reason() -> None:
    entry = _entry(client_order_id="legacy_e", tier="", scale=0.0, side="sell", qty=1, price=100, baseline_notional=0)
    entry["reason"] = "weak_size_by_partial_consensus"
    entry["metadata"] = {}
    rows = [
        {"id": 1, "created_at": "2026-01-01 00:00:00", "symbol": SYMBOL, "payload": entry},
        {"id": 2, "created_at": "2026-01-01 01:00:00", "symbol": SYMBOL, "payload": _exit(client_order_id="legacy_x", side="buy", qty=1, price=90)},
    ]

    summary = summarize_trades(build_trade_audit(rows), min_sample_warning=1)

    assert summary["by_tier"]["weak"]["closed"] == 1
    assert summary["by_tier"]["weak"]["avg_position_scale"] == pytest.approx(0.25)
