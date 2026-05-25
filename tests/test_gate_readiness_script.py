from __future__ import annotations

from pathlib import Path

import pytest

from ai_quant_trader.core.models import PositionSnapshot, Side
from scripts import gate_live_readiness
from tests.test_console_api import write_config


class FakeGateway:
    async def fetch_balance_summary(self):
        return {"ok": True, "account_slot": "trend", "usdt_total": 1000.0, "usdt_free": 900.0}

    async def fetch_positions(self, symbols):
        return [PositionSnapshot(symbol=symbol, side=Side.FLAT, qty=0.0, mark_price=0.0) for symbol in symbols]

    async def fetch_open_orders(self, symbols):
        return []

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_gate_live_readiness_writes_reconciliation_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    write_config(config_path, db_path, audit_path, ["ETH/USDT:USDT"])
    monkeypatch.setattr(gate_live_readiness, "create_exchange_gateway", lambda *_args, **_kwargs: FakeGateway())

    result = await gate_live_readiness.check(["ETH/USDT:USDT"], str(tmp_path / "missing.env"), str(config_path), write_store=True)

    assert result["balance_ok"] is True
    assert result["readiness_written"] is True
    assert result["reconciliation"]["status"] == "ok"
    assert db_path.exists()
