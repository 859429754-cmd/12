from __future__ import annotations

import logging
import uuid
from typing import Any

from ai_quant_trader.core.models import OrderRequest, OrderResult, Side
from ai_quant_trader.execution.lifecycle import OrderLifecycleManager
from ai_quant_trader.storage.sqlite import SQLiteStore
from ai_quant_trader.strategy.trend_state import TrendPositionState, TrendStateStore

logger = logging.getLogger(__name__)


class PositionStopError(RuntimeError):
    pass


class PositionStopManager:
    """Maintain one strategy-owned native stop for the exchange net position."""

    def __init__(
        self,
        store: SQLiteStore,
        lifecycle: OrderLifecycleManager,
        trend_state: TrendStateStore,
        *,
        account_slot: str,
        stop_role: str,
        legacy_stop_roles: set[str] | None = None,
    ) -> None:
        self.store = store
        self.lifecycle = lifecycle
        self.trend_state = trend_state
        self.account_slot = account_slot
        self.stop_role = stop_role
        self.legacy_stop_roles = legacy_stop_roles or set()

    async def replace_for_net_position(
        self,
        gateway: Any,
        *,
        symbol: str,
        state: TrendPositionState,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> OrderResult:
        qty = await self._net_position_qty(gateway, symbol, state.side)
        if qty <= 0:
            raise PositionStopError("net_position_missing_for_stop_replacement")

        stop_side = "sell" if state.side == Side.LONG.value else "buy"
        old_stop_ids = self.managed_stop_order_ids(symbol)
        request = OrderRequest(
            symbol=symbol,
            side=stop_side,
            amount=abs(float(qty)),
            reduce_only=True,
            client_order_id=f"aiq_net_stop_{uuid.uuid4().hex[:14]}",
            reason=reason,
            metadata={
                "role": self.stop_role,
                "account_slot": self.account_slot,
                "stop_scope": "net_position",
                "native_stop_price": float(state.stop_loss_price),
                "replaced_stop_order_ids": old_stop_ids,
                **(metadata or {}),
            },
        )
        order = await self.lifecycle.submit_stop_loss_order(gateway, request, state.stop_loss_price)
        self.store.insert(
            "orders",
            {**order.model_dump(mode="json"), "role": self.stop_role, "account_slot": self.account_slot},
            symbol,
        )
        if order.exchange_order_id:
            self.trend_state.set_native_stop_order_id(symbol, order.exchange_order_id)
        failed_cancellations: list[dict[str, str]] = []
        for order_id in old_stop_ids:
            if order.exchange_order_id and order_id == order.exchange_order_id:
                continue
            try:
                await self.lifecycle.cancel_order(
                    gateway,
                    symbol=symbol,
                    order_id=order_id,
                    client_order_id=f"aiq_stop_repl_cancel_{uuid.uuid4().hex[:8]}",
                    trigger=True,
                )
            except Exception as exc:  # noqa: BLE001
                failed_cancellations.append({"order_id": order_id, "error_type": type(exc).__name__})
                logger.warning(
                    "legacy_native_stop_cancel_failed_after_replacement",
                    extra={
                        "symbol": symbol,
                        "order_id": order_id,
                        "new_stop_order_id": order.exchange_order_id,
                        "account_slot": self.account_slot,
                        "reason": reason,
                        "error": type(exc).__name__,
                    },
                )
        if failed_cancellations:
            self.store.insert(
                "orders",
                {
                    "status": "legacy_stop_cancel_failed_after_replacement",
                    "symbol": symbol,
                    "role": self.stop_role,
                    "account_slot": self.account_slot,
                    "new_stop_order_id": order.exchange_order_id,
                    "failed_cancellations": failed_cancellations,
                    "reason": reason,
                },
                symbol,
            )
            raise PositionStopError("legacy_stop_cancel_failed_after_replacement")
        return order

    async def cancel_all_managed_stops(self, gateway: Any, symbol: str, *, reason: str) -> list[str]:
        cancelled: list[str] = []
        for order_id in self.managed_stop_order_ids(symbol):
            try:
                await self.lifecycle.cancel_order(
                    gateway,
                    symbol=symbol,
                    order_id=order_id,
                    client_order_id=f"aiq_stop_cancel_{uuid.uuid4().hex[:12]}",
                    trigger=True,
                )
                cancelled.append(order_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "managed_native_stop_cancel_failed",
                    extra={
                        "symbol": symbol,
                        "order_id": order_id,
                        "account_slot": self.account_slot,
                        "reason": reason,
                        "error": type(exc).__name__,
                    },
                )
        return cancelled

    def managed_stop_order_ids(self, symbol: str) -> list[str]:
        cancelled_ids = self._cancelled_stop_order_ids(symbol)
        ids: list[str] = []
        state = self.trend_state.get(symbol)
        if state and state.native_stop_order_id and state.native_stop_order_id not in cancelled_ids:
            ids.append(state.native_stop_order_id)

        managed_roles = {self.stop_role, *self.legacy_stop_roles}
        for row in self.store.fetch_payloads("orders", limit=1000, symbol=symbol):
            payload = row.get("payload") or {}
            if str(payload.get("account_slot") or self.account_slot) != self.account_slot:
                continue
            role = str(payload.get("role") or "")
            raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
            metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
            metadata_role = str(metadata.get("role") or "")
            if role not in managed_roles and metadata_role not in managed_roles:
                continue
            order_id = str(payload.get("exchange_order_id") or "")
            if order_id and order_id not in cancelled_ids:
                ids.append(order_id)
        return list(dict.fromkeys(ids))

    def _cancelled_stop_order_ids(self, symbol: str) -> set[str]:
        cancelled: set[str] = set()
        for row in self.store.fetch_payloads("order_lifecycle", limit=2000, symbol=symbol):
            payload = row.get("payload") or {}
            if str(payload.get("account_slot") or self.account_slot) != self.account_slot:
                continue
            if str(payload.get("order_type") or "") != "cancel":
                continue
            if str(payload.get("status") or "") != "cancelled":
                continue
            reason = str(payload.get("reason") or "")
            if reason.startswith("cancel_order:"):
                cancelled.add(reason.split(":", 1)[1])
        return cancelled

    async def _net_position_qty(self, gateway: Any, symbol: str, side: str) -> float:
        positions = await gateway.fetch_positions([symbol])
        for position in positions:
            position_side = position.side.value if isinstance(position.side, Side) else str(position.side)
            if position.symbol == symbol and position_side == side and abs(float(position.qty)) > 0:
                return abs(float(position.qty))
        return 0.0
