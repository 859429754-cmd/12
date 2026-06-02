from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ai_quant_trader.core.models import OrderLifecycleEvent, OrderLifecycleStatus, OrderRequest, OrderResult, Side
from ai_quant_trader.storage.sqlite import SQLiteStore

logger = logging.getLogger(__name__)


class OrderLifecycleError(RuntimeError):
    pass


class DuplicateClientOrderIdError(OrderLifecycleError):
    pass


class OrderSubmissionUncertain(OrderLifecycleError):
    pass


class OrderRejected(OrderLifecycleError):
    pass


class OrderLifecycleManager:
    """Persist order intent before exchange submission and prevent blind retries."""

    def __init__(self, store: SQLiteStore, *, gateway_mode: str = "unknown") -> None:
        self.store = store
        self.gateway_mode = gateway_mode

    async def submit_market_order(
        self,
        gateway: Any,
        request: OrderRequest,
        *,
        gateway_mode: str | None = None,
    ) -> OrderResult:
        return await self._submit_order(
            gateway,
            request,
            "market",
            lambda: gateway.create_market_order(request),
            gateway_mode=gateway_mode,
        )

    async def submit_stop_loss_order(
        self,
        gateway: Any,
        request: OrderRequest,
        stop_price: float,
        *,
        price_type: int = 1,
        gateway_mode: str | None = None,
    ) -> OrderResult:
        return await self._submit_order(
            gateway,
            request,
            "stop_loss",
            lambda: gateway.create_stop_loss_order(request, stop_price, price_type),
            gateway_mode=gateway_mode,
        )

    async def close_position(
        self,
        gateway: Any,
        symbol: str,
        *,
        reason: str,
        client_order_id: str | None = None,
        gateway_mode: str | None = None,
    ) -> OrderResult | None:
        positions = await gateway.fetch_positions([symbol])
        position = next((item for item in positions if item.symbol == symbol), None)
        if position is None or position.side == Side.FLAT or abs(position.qty) <= 0:
            return None
        request = OrderRequest(
            symbol=symbol,
            side="sell" if position.side == Side.LONG else "buy",
            amount=abs(position.qty),
            reduce_only=True,
            client_order_id=client_order_id or f"aiq_close_{uuid.uuid4().hex[:18]}",
            reason=reason,
        )
        return await self.submit_market_order(gateway, request, gateway_mode=gateway_mode)

    async def cancel_order(
        self,
        gateway: Any,
        *,
        symbol: str,
        order_id: str,
        client_order_id: str,
        trigger: bool = False,
        gateway_mode: str | None = None,
    ) -> bool:
        mode = gateway_mode or self._gateway_mode(gateway)
        self._record(
            client_order_id=client_order_id,
            symbol=symbol,
            status=OrderLifecycleStatus.CANCEL_PENDING,
            order_type="cancel",
            gateway_mode=mode,
            reason=f"cancel_order:{order_id}",
        )
        try:
            ok = bool(await gateway.cancel_order(symbol, order_id, trigger=trigger))
        except Exception as exc:  # noqa: BLE001
            self._record(
                client_order_id=client_order_id,
                symbol=symbol,
                status=OrderLifecycleStatus.CANCEL_FAILED,
                order_type="cancel",
                gateway_mode=mode,
                reason=f"cancel_order_failed:{order_id}",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            raise
        self._record(
            client_order_id=client_order_id,
            symbol=symbol,
            status=OrderLifecycleStatus.CANCELLED if ok else OrderLifecycleStatus.CANCEL_FAILED,
            order_type="cancel",
            gateway_mode=mode,
            reason=f"cancel_order:{order_id}",
        )
        return ok

    async def refresh_recent_orders(
        self,
        gateway: Any,
        *,
        symbols: list[str] | None = None,
        limit: int = 100,
        gateway_mode: str | None = None,
    ) -> list[OrderLifecycleEvent]:
        mode = gateway_mode or self._gateway_mode(gateway)
        latest_by_client_id: dict[str, dict[str, Any]] = {}
        for row in self.store.fetch_payloads("order_lifecycle", limit=limit):
            payload = row.get("payload") or {}
            client_order_id = str(payload.get("client_order_id") or "")
            if not client_order_id or client_order_id in latest_by_client_id:
                continue
            if symbols and payload.get("symbol") not in set(symbols):
                continue
            latest_by_client_id[client_order_id] = payload

        updates: list[OrderLifecycleEvent] = []
        for payload in latest_by_client_id.values():
            if str(payload.get("status") or "") in {"filled", "cancelled", "rejected", "blocked", "duplicate_suppressed"}:
                continue
            exchange_order_id = str(payload.get("exchange_order_id") or "")
            if not exchange_order_id:
                continue
            refreshed = await self._fetch_order_status(gateway, str(payload.get("symbol")), exchange_order_id)
            if refreshed is None:
                continue
            status = self._status_from_order(refreshed)
            if status.value == payload.get("status") and refreshed.status == payload.get("order_status"):
                continue
            event = OrderLifecycleEvent(
                client_order_id=str(payload.get("client_order_id")),
                symbol=refreshed.symbol,
                status=status,
                order_type=str(payload.get("order_type") or "market"),  # type: ignore[arg-type]
                side=str(payload.get("side") or refreshed.side),
                amount=float(payload.get("amount") or refreshed.amount or 0.0),
                reduce_only=bool(payload.get("reduce_only")),
                gateway_mode=mode,
                reason="exchange_order_status_refreshed",
                exchange_order_id=refreshed.exchange_order_id,
                order_status=refreshed.status,
                order=refreshed.model_dump(mode="json"),
            )
            self.store.insert("order_lifecycle", event, event.symbol)
            updates.append(event)
        return updates

    async def _submit_order(
        self,
        gateway: Any,
        request: OrderRequest,
        order_type: str,
        submit: Callable[[], Awaitable[OrderResult]],
        *,
        gateway_mode: str | None = None,
    ) -> OrderResult:
        mode = gateway_mode or self._gateway_mode(gateway)
        existing = self.latest_event(request.client_order_id, symbol=request.symbol)
        if existing is not None:
            return await self._handle_duplicate_or_recover(gateway, request, order_type, existing, mode)

        self._record_request(OrderLifecycleStatus.INTENT_RECORDED, request, order_type, mode)
        self._record_request(OrderLifecycleStatus.SUBMITTING, request, order_type, mode)
        try:
            order = await submit()
        except Exception as exc:  # noqa: BLE001
            recovered = await self._recover_order_from_gateway(gateway, request)
            if recovered is not None:
                self._record_order(self._status_from_order(recovered), request, order_type, mode, recovered, reason="recovered_after_submit_error")
                return recovered
            self._record_request(
                OrderLifecycleStatus.UNKNOWN,
                request,
                order_type,
                mode,
                reason="exchange_submit_exception_state_unknown",
                error_type=type(exc).__name__,
                error_message=str(exc),
                recoverable=True,
            )
            logger.error(
                "order_submit_state_unknown",
                extra={"symbol": request.symbol, "client_order_id": request.client_order_id, "gateway_mode": mode, "error_type": type(exc).__name__},
            )
            raise OrderSubmissionUncertain(f"order_submit_state_unknown:{request.client_order_id}") from exc

        status = self._status_from_order(order)
        self._record_order(status, request, order_type, mode, order)
        if status == OrderLifecycleStatus.REJECTED:
            raise OrderRejected(f"order_rejected:{request.client_order_id}:{order.status}")
        return order

    async def _handle_duplicate_or_recover(
        self,
        gateway: Any,
        request: OrderRequest,
        order_type: str,
        existing: dict[str, Any],
        gateway_mode: str,
    ) -> OrderResult:
        payload = existing.get("payload") or {}
        existing_order = payload.get("order")
        if existing_order:
            order = OrderResult.model_validate(existing_order)
            status = self._status_from_order(order)
            self._record_order(
                OrderLifecycleStatus.DUPLICATE_SUPPRESSED,
                request,
                order_type,
                gateway_mode,
                order,
                reason="duplicate_client_order_id_returned_existing_order",
            )
            if status == OrderLifecycleStatus.REJECTED:
                raise OrderRejected(f"order_rejected:{request.client_order_id}:{order.status}")
            return order

        recovered = await self._recover_order_from_gateway(gateway, request)
        if recovered is not None:
            status = self._status_from_order(recovered)
            self._record_order(
                status,
                request,
                order_type,
                gateway_mode,
                recovered,
                reason="duplicate_client_order_id_recovered_from_exchange",
            )
            if status == OrderLifecycleStatus.REJECTED:
                raise OrderRejected(f"order_rejected:{request.client_order_id}:{recovered.status}")
            return recovered

        status = str(payload.get("status") or "unknown")
        raise DuplicateClientOrderIdError(f"client_order_id_unresolved_or_reused:{request.client_order_id}:{status}")

    async def _recover_order_from_gateway(self, gateway: Any, request: OrderRequest) -> OrderResult | None:
        finder = getattr(gateway, "find_order_by_client_order_id", None)
        if finder is None:
            return None
        found = await finder(request.symbol, request.client_order_id)
        if found is None:
            return None
        if isinstance(found, OrderResult):
            return found
        return OrderResult.model_validate(found)

    async def _fetch_order_status(self, gateway: Any, symbol: str, exchange_order_id: str) -> OrderResult | None:
        fetcher = getattr(gateway, "fetch_order_by_exchange_id", None)
        if fetcher is None:
            return None
        found = await fetcher(symbol, exchange_order_id)
        if found is None:
            return None
        if isinstance(found, OrderResult):
            return found
        return OrderResult.model_validate(found)

    def latest_event(self, client_order_id: str, *, symbol: str | None = None) -> dict[str, Any] | None:
        return self.store.fetch_latest_payload_by_value(
            "order_lifecycle",
            "client_order_id",
            client_order_id,
            symbol=symbol,
        )

    def _record_request(
        self,
        status: OrderLifecycleStatus,
        request: OrderRequest,
        order_type: str,
        gateway_mode: str,
        *,
        reason: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        recoverable: bool = False,
    ) -> int:
        return self._record(
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            status=status,
            order_type=order_type,
            side=request.side,
            amount=float(request.amount),
            reduce_only=bool(request.reduce_only),
            gateway_mode=gateway_mode,
            reason=reason or request.reason,
            error_type=error_type,
            error_message=error_message,
            recoverable=recoverable,
        )

    def _record_order(
        self,
        status: OrderLifecycleStatus,
        request: OrderRequest,
        order_type: str,
        gateway_mode: str,
        order: OrderResult,
        *,
        reason: str | None = None,
    ) -> int:
        return self._record(
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            status=status,
            order_type=order_type,
            side=request.side,
            amount=float(request.amount),
            reduce_only=bool(request.reduce_only),
            gateway_mode=gateway_mode,
            reason=reason or request.reason,
            exchange_order_id=order.exchange_order_id,
            order_status=order.status,
            order=order.model_dump(mode="json"),
        )

    def _record(
        self,
        *,
        client_order_id: str,
        symbol: str,
        status: OrderLifecycleStatus,
        order_type: str,
        gateway_mode: str,
        reason: str = "",
        side: str | None = None,
        amount: float | None = None,
        reduce_only: bool = False,
        exchange_order_id: str | None = None,
        order_status: str | None = None,
        order: dict[str, Any] | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        recoverable: bool = False,
    ) -> int:
        event = OrderLifecycleEvent(
            client_order_id=client_order_id,
            symbol=symbol,
            status=status,
            order_type=order_type,  # type: ignore[arg-type]
            side=side,
            amount=amount,
            reduce_only=reduce_only,
            gateway_mode=gateway_mode,
            reason=reason,
            exchange_order_id=exchange_order_id,
            order_status=order_status,
            order=order,
            error_type=error_type,
            error_message=error_message,
            recoverable=recoverable,
        )
        return self.store.insert("order_lifecycle", event, symbol)

    def _gateway_mode(self, gateway: Any) -> str:
        return str(getattr(gateway, "mode", self.gateway_mode) or self.gateway_mode)

    def _status_from_order(self, order: OrderResult) -> OrderLifecycleStatus:
        status = str(order.status or "").lower()
        if "partial" in status:
            return OrderLifecycleStatus.PARTIALLY_FILLED
        if status in {"closed", "filled", "done"} or "filled" in status:
            return OrderLifecycleStatus.FILLED
        if status in {"rejected", "reject", "failed", "expired"} or "reject" in status or "failed" in status:
            return OrderLifecycleStatus.REJECTED
        if status in {"canceled", "cancelled"} or "cancel" in status:
            return OrderLifecycleStatus.CANCELLED
        if "submitted" in status:
            return OrderLifecycleStatus.SUBMITTED
        return OrderLifecycleStatus.ACCEPTED
