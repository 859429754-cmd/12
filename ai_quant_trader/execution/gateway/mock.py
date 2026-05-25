from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from ai_quant_trader.core.models import OrderRequest, OrderResult, PositionSnapshot, Side, utc_now

logger = logging.getLogger(__name__)


class MockExchangeGateway:
    """本地模拟交易网关。

    该网关不发任何交易所网络请求，只在本地 JSON 文件维护虚拟余额、持仓和订单。
    它用于控制台验证、最小仓测试和回归测试，不能被误认为真实成交。
    """

    mode = "mock"

    def __init__(self, state_path: str = "data/mock_exchange_state.json") -> None:
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

    async def check_connectivity(self) -> bool:
        return True

    async def fetch_balance_summary(self) -> dict[str, Any]:
        state = self._load_state()
        balance = state["balance"]
        return {
            "ok": True,
            "mode": self.mode,
            "dry_run": True,
            "message": "当前为本地模拟交易网关，未读取真实 Gate.io 余额。",
            "usdt_total": float(balance["USDT"]["total"]),
            "usdt_free": float(balance["USDT"]["free"]),
            "usdt_used": float(balance["USDT"]["used"]),
        }

    async def fetch_positions(self, symbols: list[str]) -> list[PositionSnapshot]:
        state = self._load_state()
        positions = state.get("positions", {})
        output: list[PositionSnapshot] = []
        for symbol in symbols:
            raw = positions.get(symbol) or {}
            qty = abs(float(raw.get("qty") or 0.0))
            raw_side = str(raw.get("side") or "flat")
            side = Side(raw_side) if raw_side in {"long", "short", "flat"} else Side.FLAT
            if qty <= 0:
                side = Side.FLAT
            output.append(
                PositionSnapshot(
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    entry_price=float(raw.get("entry_price") or 0.0),
                    mark_price=float(raw.get("mark_price") or 0.0),
                    unrealized_pnl=0.0,
                )
            )
        return output

    async def fetch_open_orders(self, symbols: list[str]) -> list[dict[str, Any]]:
        state = self._load_state()
        output = []
        for order in state.get("orders", []):
            if order.get("symbol") in symbols and str(order.get("status") or "").endswith("created"):
                output.append(order)
        return output

    async def find_order_by_client_order_id(self, symbol: str, client_order_id: str) -> OrderResult | None:
        state = self._load_state()
        for order in reversed(state.get("orders", [])):
            if order.get("symbol") == symbol and (order.get("raw") or {}).get("client_order_id") == client_order_id:
                return OrderResult.model_validate(order)
        return None

    async def fetch_order_by_exchange_id(self, symbol: str, exchange_order_id: str) -> OrderResult | None:
        state = self._load_state()
        for order in reversed(state.get("orders", [])):
            if order.get("symbol") == symbol and order.get("exchange_order_id") == exchange_order_id:
                return OrderResult.model_validate(order)
        return None

    async def minimum_order_amount(self, symbol: str, price: float | None = None) -> float:
        reference_price = max(float(price or 0.0), 1e-9)
        return max(5.0 / reference_price, 0.0001)

    async def minimum_order_check(self, symbol: str, price: float | None = None) -> dict[str, Any]:
        amount = await self.minimum_order_amount(symbol, price)
        reference_price = float(price or 0.0)
        return {
            "ok": amount > 0,
            "symbol": symbol,
            "amount": amount,
            "amount_unit": "base",
            "contract_size": 1.0,
            "reference_price": reference_price,
            "estimated_notional": amount * reference_price,
            "mode": self.mode,
            "dry_run": True,
        }

    async def contract_size(self, symbol: str) -> float:
        return 1.0

    async def create_market_order(self, request: OrderRequest) -> OrderResult:
        state = self._load_state()
        for existing in state.setdefault("orders", []):
            if (existing.get("raw") or {}).get("client_order_id") == request.client_order_id:
                return OrderResult.model_validate(existing)
        order_id = f"mock_{uuid.uuid4().hex[:12]}"
        amount = float(request.amount)
        price = 0.0
        self._apply_position_update(state, request, amount, price)
        result = OrderResult(
            symbol=request.symbol,
            side=request.side,
            amount=amount,
            price=None,
            status="mock_created",
            dry_run=True,
            exchange_order_id=order_id,
            raw={**request.model_dump(mode="json"), "mode": self.mode},
        )
        state.setdefault("orders", []).append(result.model_dump(mode="json"))
        self._save_state(state)
        logger.info("mock_order_created", extra={"symbol": request.symbol, "side": request.side, "amount": amount, "order_id": order_id})
        return result

    async def create_stop_loss_order(
        self,
        request: OrderRequest,
        stop_price: float,
        price_type: int = 1,
    ) -> OrderResult:
        if not request.reduce_only:
            raise ValueError("native_stop_loss_order_must_be_reduce_only")
        state = self._load_state()
        for existing in state.setdefault("orders", []):
            if (existing.get("raw") or {}).get("client_order_id") == request.client_order_id:
                return OrderResult.model_validate(existing)
        order_id = f"mock_stop_{uuid.uuid4().hex[:12]}"
        result = OrderResult(
            symbol=request.symbol,
            side=request.side,
            amount=float(request.amount),
            price=float(stop_price),
            status="mock_stop_created",
            dry_run=True,
            exchange_order_id=order_id,
            raw={
                **request.model_dump(mode="json"),
                "mode": self.mode,
                "stop_loss_price": float(stop_price),
                "price_type": int(price_type),
                "trigger": True,
            },
        )
        state.setdefault("orders", []).append(result.model_dump(mode="json"))
        self._save_state(state)
        logger.info("mock_stop_order_created", extra={"symbol": request.symbol, "side": request.side, "order_id": order_id})
        return result

    async def cancel_order(self, symbol: str, order_id: str, *, trigger: bool = False) -> bool:
        state = self._load_state()
        state.setdefault("cancelled_orders", []).append({"symbol": symbol, "order_id": order_id, "trigger": trigger})
        self._save_state(state)
        logger.info("mock_order_cancelled", extra={"symbol": symbol, "order_id": order_id, "trigger": trigger})
        return True

    async def close_position(self, symbol: str, reason: str = "manual_close") -> OrderResult | None:
        position = (await self.fetch_positions([symbol]))[0]
        if position.side == Side.FLAT or position.qty <= 0:
            return None
        return await self.create_market_order(
            OrderRequest(
                symbol=symbol,
                side="sell" if position.side == Side.LONG else "buy",
                amount=position.qty,
                reduce_only=True,
                client_order_id=f"aiq_mock_close_{uuid.uuid4().hex[:12]}",
                reason=reason,
            )
        )

    async def close_positions(self, symbols: list[str], reason: str = "panic_close") -> list[OrderResult]:
        results: list[OrderResult] = []
        for symbol in symbols:
            order = await self.close_position(symbol, reason)
            if order:
                results.append(order)
        return results

    async def close(self) -> None:
        return None

    def _default_state(self) -> dict[str, Any]:
        return {
            "created_at": utc_now().isoformat(),
            "balance": {"USDT": {"total": 10_000.0, "free": 10_000.0, "used": 0.0}},
            "positions": {},
            "orders": [],
        }

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            state = self._default_state()
            self._save_state(state)
            return state
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            state = json.loads(raw)
            if not isinstance(state, dict):
                raise ValueError("mock state root is not object")
            state.setdefault("balance", {"USDT": {"total": 10_000.0, "free": 10_000.0, "used": 0.0}})
            state.setdefault("positions", {})
            state.setdefault("orders", [])
            return state
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("mock_state_recovered", extra={"path": str(self.state_path), "error": str(exc)})
            state = self._default_state()
            self._save_state(state)
            return state

    def _save_state(self, state: dict[str, Any]) -> None:
        tmp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        try:
            tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_path, self.state_path)
        except OSError as exc:
            logger.error("mock_state_write_failed", extra={"path": str(self.state_path), "error": str(exc)})
            raise

    def _apply_position_update(self, state: dict[str, Any], request: OrderRequest, amount: float, price: float) -> None:
        positions = state.setdefault("positions", {})
        current = positions.get(request.symbol) or {"side": "flat", "qty": 0.0, "entry_price": 0.0, "mark_price": 0.0}
        qty = float(current.get("qty") or 0.0)
        side = str(current.get("side") or "flat")

        if request.reduce_only:
            new_qty = max(0.0, qty - amount)
            positions[request.symbol] = {
                "side": "flat" if new_qty <= 0 else side,
                "qty": new_qty,
                "entry_price": float(current.get("entry_price") or 0.0),
                "mark_price": price or float(current.get("mark_price") or 0.0),
            }
            return

        target_side = "long" if request.side == "buy" else "short"
        if side == target_side:
            new_qty = qty + amount
        else:
            new_qty = amount
        positions[request.symbol] = {
            "side": target_side,
            "qty": new_qty,
            "entry_price": price or float(current.get("entry_price") or 0.0),
            "mark_price": price or float(current.get("mark_price") or 0.0),
        }
