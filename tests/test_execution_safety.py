from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_quant_trader.core.models import ExchangeConnectionStatus, PositionSnapshot, Side
from ai_quant_trader.execution.safety import ExchangeSafetyMonitor
from ai_quant_trader.strategy.trend_state import TrendStateStore


class HealthyGateway:
    async def fetch_balance_summary(self):
        return {"ok": True, "usdt_total": 1000.0}

    async def fetch_positions(self, symbols):
        return [PositionSnapshot(symbol=symbol, side=Side.FLAT, qty=0.0, mark_price=0.0) for symbol in symbols]

    async def fetch_open_orders(self, symbols):
        return []


def test_exchange_safety_stale_private_state_blocks_new_entries() -> None:
    monitor = ExchangeSafetyMonitor(stale_after_seconds=300)
    monitor.mark_success("pytest_success")
    monitor._state = monitor._state.model_copy(  # noqa: SLF001
        update={"last_success_at": datetime.now(UTC) - timedelta(seconds=301)}
    )

    state = monitor.state

    assert state.status == ExchangeConnectionStatus.DEGRADED_READONLY
    assert state.can_open_new_entries is False
    assert state.reason == "exchange_private_state_stale_over_5m"
    assert "Gate" in state.manual_action


@pytest.mark.asyncio
async def test_reconciliation_required_blocks_orphan_exchange_position(tmp_path: Path) -> None:
    class OrphanPositionGateway(HealthyGateway):
        async def fetch_positions(self, symbols):
            return [PositionSnapshot(symbol=symbols[0], side=Side.LONG, qty=0.1, mark_price=2000.0)]

    monitor = ExchangeSafetyMonitor(stale_after_seconds=300)
    report = await monitor.reconcile(
        OrphanPositionGateway(),
        ["ETH/USDT:USDT"],
        TrendStateStore(str(tmp_path / "state_trend.json")),
        live=True,
    )

    assert report.status == ExchangeConnectionStatus.RECONCILIATION_REQUIRED
    assert monitor.state.can_open_new_entries is False
    assert any("orphan_position_without_local_trend_state" in issue for issue in report.issues)


@pytest.mark.asyncio
async def test_reconciliation_ok_allows_new_entries_after_private_state_verified(tmp_path: Path) -> None:
    monitor = ExchangeSafetyMonitor(stale_after_seconds=300)
    report = await monitor.reconcile(
        HealthyGateway(),
        ["ETH/USDT:USDT"],
        TrendStateStore(str(tmp_path / "state_trend.json")),
        live=True,
    )

    assert report.status == ExchangeConnectionStatus.OK
    assert monitor.state.can_open_new_entries is True
    assert monitor.state.reason == "exchange_reconciliation_ok"
