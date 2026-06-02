from __future__ import annotations

from pathlib import Path

import pytest

from ai_quant_trader.core.models import OrderRequest, OrderResult, PositionSnapshot, Side
from ai_quant_trader.execution.lifecycle import (
    DuplicateClientOrderIdError,
    OrderLifecycleManager,
    OrderRejected,
    OrderSubmissionUncertain,
)
from ai_quant_trader.storage.sqlite import SQLiteStore


class RecordingGateway:
    mode = "mock"

    def __init__(
        self,
        *,
        status: str = "closed",
        fail: bool = False,
        recovered: OrderResult | None = None,
        fail_cancel: bool = False,
    ) -> None:
        self.status = status
        self.fail = fail
        self.recovered = recovered
        self.fail_cancel = fail_cancel
        self.submits = 0

    async def create_market_order(self, request: OrderRequest) -> OrderResult:
        self.submits += 1
        if self.fail:
            raise TimeoutError("submit_timeout")
        return OrderResult(
            symbol=request.symbol,
            side=request.side,
            amount=request.amount,
            status=self.status,
            dry_run=True,
            exchange_order_id=f"ex_{self.submits}",
            raw=request.model_dump(mode="json"),
        )

    async def create_stop_loss_order(self, request: OrderRequest, stop_price: float, price_type: int = 1) -> OrderResult:
        return await self.create_market_order(request)

    async def find_order_by_client_order_id(self, symbol: str, client_order_id: str) -> OrderResult | None:
        return self.recovered

    async def fetch_order_by_exchange_id(self, symbol: str, exchange_order_id: str) -> OrderResult | None:
        return self.recovered

    async def cancel_order(self, symbol: str, order_id: str, *, trigger: bool = False) -> bool:
        if self.fail_cancel:
            raise TimeoutError("cancel_timeout")
        return True

    async def fetch_positions(self, symbols: list[str]) -> list[PositionSnapshot]:
        return [PositionSnapshot(symbol=symbols[0], side=Side.LONG, qty=0.25, mark_price=2000.0)]


def make_store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(str(tmp_path / "trader.sqlite3"), str(tmp_path / "audit.jsonl"))


def make_request(client_order_id: str = "client_1") -> OrderRequest:
    return OrderRequest(
        symbol="ETH/USDT:USDT",
        side="buy",
        amount=0.01,
        reduce_only=False,
        client_order_id=client_order_id,
        reason="pytest",
    )


@pytest.mark.asyncio
async def test_order_lifecycle_records_intent_before_submission(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    manager = OrderLifecycleManager(store)
    gateway = RecordingGateway(status="closed")

    order = await manager.submit_market_order(gateway, make_request())
    events = list(reversed(store.fetch_payloads("order_lifecycle", limit=10)))

    assert order.exchange_order_id == "ex_1"
    assert [event["payload"]["status"] for event in events] == ["intent_recorded", "submitting", "filled"]
    assert events[0]["payload"]["client_order_id"] == "client_1"
    store.close()


@pytest.mark.asyncio
async def test_order_lifecycle_suppresses_duplicate_client_order_id(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    manager = OrderLifecycleManager(store)
    gateway = RecordingGateway(status="closed")
    request = make_request("stable_client_id")

    first = await manager.submit_market_order(gateway, request)
    second = await manager.submit_market_order(gateway, request)
    latest = store.fetch_payloads("order_lifecycle", limit=1)[0]["payload"]

    assert second.exchange_order_id == first.exchange_order_id
    assert gateway.submits == 1
    assert latest["status"] == "duplicate_suppressed"
    store.close()


@pytest.mark.asyncio
async def test_order_lifecycle_unknown_after_submit_error_blocks_blind_retry(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    manager = OrderLifecycleManager(store)
    request = make_request("uncertain_client_id")

    with pytest.raises(OrderSubmissionUncertain):
        await manager.submit_market_order(RecordingGateway(fail=True), request)
    latest = store.fetch_payloads("order_lifecycle", limit=1)[0]["payload"]
    with pytest.raises(DuplicateClientOrderIdError):
        await manager.submit_market_order(RecordingGateway(), request)

    assert latest["status"] == "unknown"
    assert latest["recoverable"] is True
    assert latest["error_type"] == "TimeoutError"
    store.close()


@pytest.mark.asyncio
async def test_order_lifecycle_recovers_exchange_order_after_submit_timeout(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    manager = OrderLifecycleManager(store)
    request = make_request("recovered_client_id")
    recovered = OrderResult(
        symbol=request.symbol,
        side=request.side,
        amount=request.amount,
        status="open",
        dry_run=False,
        exchange_order_id="gate_123",
    )

    order = await manager.submit_market_order(RecordingGateway(fail=True, recovered=recovered), request)
    latest = store.fetch_payloads("order_lifecycle", limit=1)[0]["payload"]

    assert order.exchange_order_id == "gate_123"
    assert latest["status"] == "accepted"
    assert latest["reason"] == "recovered_after_submit_error"
    store.close()


@pytest.mark.asyncio
async def test_order_lifecycle_rejected_order_is_terminal(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    manager = OrderLifecycleManager(store)

    with pytest.raises(OrderRejected):
        await manager.submit_market_order(RecordingGateway(status="rejected"), make_request("reject_client_id"))
    latest = store.fetch_payloads("order_lifecycle", limit=1)[0]["payload"]

    assert latest["status"] == "rejected"
    assert latest["order_status"] == "rejected"
    store.close()


@pytest.mark.asyncio
async def test_order_lifecycle_refresh_updates_partial_fill(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    manager = OrderLifecycleManager(store)
    request = make_request("partial_client_id")
    accepted = await manager.submit_market_order(RecordingGateway(status="open"), request)
    recovered = accepted.model_copy(update={"status": "partially_filled"})

    updates = await manager.refresh_recent_orders(RecordingGateway(recovered=recovered), symbols=[request.symbol])
    latest = store.fetch_payloads("order_lifecycle", limit=1)[0]["payload"]

    assert len(updates) == 1
    assert latest["status"] == "partially_filled"
    assert latest["reason"] == "exchange_order_status_refreshed"
    store.close()


@pytest.mark.asyncio
async def test_order_lifecycle_cancel_failure_is_audited(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    manager = OrderLifecycleManager(store)

    with pytest.raises(TimeoutError):
        await manager.cancel_order(
            RecordingGateway(fail_cancel=True),
            symbol="ETH/USDT:USDT",
            order_id="stop_123",
            client_order_id="cancel_stop_123",
            trigger=True,
        )
    latest = store.fetch_payloads("order_lifecycle", limit=1)[0]["payload"]

    assert latest["status"] == "cancel_failed"
    assert latest["error_type"] == "TimeoutError"
    assert latest["reason"] == "cancel_order_failed:stop_123"
    store.close()


@pytest.mark.asyncio
async def test_order_lifecycle_close_position_records_reduce_only_intent(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    manager = OrderLifecycleManager(store)
    gateway = RecordingGateway(status="closed")

    order = await manager.close_position(
        gateway,
        "ETH/USDT:USDT",
        reason="software_fixed_atr_stop",
        client_order_id="close_client_id",
    )
    events = list(reversed(store.fetch_payloads("order_lifecycle", limit=10)))

    assert order is not None
    assert order.side == "sell"
    assert order.amount == pytest.approx(0.25)
    assert [event["payload"]["status"] for event in events] == ["intent_recorded", "submitting", "filled"]
    assert events[0]["payload"]["client_order_id"] == "close_client_id"
    assert events[0]["payload"]["reduce_only"] is True
    store.close()


@pytest.mark.asyncio
async def test_stop_loss_trigger_refresh_marks_filled(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    manager = OrderLifecycleManager(store)
    request = make_request("stop_triggered_client_id").model_copy(update={"side": "sell", "reduce_only": True})
    accepted = await manager.submit_stop_loss_order(RecordingGateway(status="open"), request, stop_price=3100.0)
    triggered = accepted.model_copy(update={"status": "closed"})

    updates = await manager.refresh_recent_orders(RecordingGateway(recovered=triggered), symbols=[request.symbol])
    latest = store.fetch_payloads("order_lifecycle", limit=1)[0]["payload"]

    assert len(updates) == 1
    assert latest["order_type"] == "stop_loss"
    assert latest["status"] == "filled"
    assert latest["exchange_order_id"] == accepted.exchange_order_id
    store.close()
