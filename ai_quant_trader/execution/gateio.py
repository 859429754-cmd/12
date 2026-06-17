from __future__ import annotations

import logging
import os
import uuid
import math
from typing import Any

import ccxt.async_support as ccxt

from ai_quant_trader.core.models import OrderRequest, OrderResult, PositionSnapshot, Side

logger = logging.getLogger(__name__)


class GateExecutionClient:
    """Gate.io USDT 永续执行器。

    dry_run=true 时只记录模拟订单；真实下单必须显式配置 dry_run=false 并提供密钥。
    """

    def __init__(
        self,
        dry_run: bool = True,
        *,
        api_key_env: str = "GATEIO_API_KEY",
        api_secret_env: str = "GATEIO_API_SECRET",
        account_slot: str = "default",
    ):
        self.dry_run = dry_run
        self.api_key_env = api_key_env
        self.api_secret_env = api_secret_env
        self.account_slot = account_slot
        self.exchange = self._new_exchange()

    def _new_exchange(self):
        return ccxt.gateio(
            {
                "apiKey": os.getenv(self.api_key_env),
                "secret": os.getenv(self.api_secret_env),
                "enableRateLimit": True,
                "options": {"defaultType": "swap", "defaultSettle": "USDT"},
            }
        )

    async def reload_from_env(self) -> None:
        await self.close()
        self.exchange = self._new_exchange()

    async def check_connectivity(self) -> bool:
        if self.dry_run:
            return True
        await self.exchange.fetch_balance()
        return True

    async def fetch_balance_summary(self) -> dict[str, Any]:
        if not os.getenv(self.api_key_env) or not os.getenv(self.api_secret_env):
            return {
                "ok": False,
                "message": "Gate API is not configured for this account slot.",
                "account_slot": self.account_slot,
            }
        balance = await self.exchange.fetch_balance()
        total = balance.get("total", {}) or {}
        free = balance.get("free", {}) or {}
        used = balance.get("used", {}) or {}
        if "USDT" not in total:
            raise RuntimeError("gate_balance_missing_usdt_total")
        return {
            "ok": True,
            "account_slot": self.account_slot,
            "usdt_total": float(total["USDT"]),
            "usdt_free": float(free.get("USDT") or 0.0),
            "usdt_used": float(used.get("USDT") or 0.0),
            "raw": balance,
        }

    async def fetch_positions(self, symbols: list[str]) -> list[PositionSnapshot]:
        if self.dry_run:
            return [PositionSnapshot(symbol=symbol, side=Side.FLAT, qty=0.0, mark_price=0.0) for symbol in symbols]
        await self.exchange.load_markets()
        raw_positions = await self.exchange.fetch_positions(symbols)
        by_symbol: dict[str, PositionSnapshot] = {}
        for item in raw_positions:
            symbol = item.get("symbol")
            if symbol not in symbols:
                continue
            info = item.get("info", {}) or {}
            contracts = float(item.get("contracts") or info.get("size") or 0.0)
            contract_size = self._market_contract_size(symbol)
            qty = abs(contracts) * contract_size
            side_raw = str(item.get("side") or info.get("side") or "").lower()
            if contracts == 0:
                side = Side.FLAT
            elif side_raw in {"short", "sell"} or contracts < 0:
                side = Side.SHORT
            else:
                side = Side.LONG
            mark_price = float(item.get("markPrice") or item.get("lastPrice") or item.get("entryPrice") or 0.0)
            snapshot = PositionSnapshot(
                symbol=symbol,
                side=side,
                qty=qty,
                entry_price=float(item.get("entryPrice") or 0.0),
                mark_price=mark_price,
                unrealized_pnl=float(item.get("unrealizedPnl") or 0.0),
            )
            existing = by_symbol.get(symbol)
            by_symbol[symbol] = self._merge_position_snapshot(existing, snapshot) if existing else snapshot
        output: list[PositionSnapshot] = []
        for symbol in symbols:
            output.append(by_symbol.get(symbol) or PositionSnapshot(symbol=symbol, side=Side.FLAT, qty=0.0, mark_price=0.0))
        return output

    async def fetch_open_orders(self, symbols: list[str]) -> list[dict[str, Any]]:
        if self.dry_run:
            return []
        await self.exchange.load_markets()
        output: list[dict[str, Any]] = []
        for symbol in symbols:
            try:
                output.extend(await self.exchange.fetch_open_orders(symbol))
            except Exception as exc:  # noqa: BLE001
                logger.warning("gate_open_orders_fetch_failed", extra={"symbol": symbol, "error": type(exc).__name__})
                raise
        return output

    async def find_order_by_client_order_id(self, symbol: str, client_order_id: str) -> OrderResult | None:
        if self.dry_run:
            return None
        await self.exchange.load_markets()
        gate_text = self._gate_text(client_order_id)
        for fetcher_name in ("fetch_open_orders", "fetch_closed_orders"):
            fetcher = getattr(self.exchange, fetcher_name, None)
            if not callable(fetcher):
                continue
            try:
                orders = await fetcher(symbol)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "gate_order_recovery_fetch_failed",
                    extra={"symbol": symbol, "fetcher": fetcher_name, "error": type(exc).__name__},
                )
                continue
            for order in orders or []:
                if self._order_matches_client_id(order, client_order_id, gate_text):
                    return self._order_result_from_exchange_order(symbol, order)
        return None

    async def fetch_order_by_exchange_id(self, symbol: str, exchange_order_id: str) -> OrderResult | None:
        if self.dry_run or not exchange_order_id:
            return None
        await self.exchange.load_markets()
        try:
            order = await self.exchange.fetch_order(exchange_order_id, symbol)
        except ccxt.OrderNotFound:
            logger.warning(
                "gate_order_status_not_found",
                extra={"symbol": symbol, "order_id": exchange_order_id},
            )
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "gate_order_status_fetch_failed",
                extra={"symbol": symbol, "order_id": exchange_order_id, "error": type(exc).__name__},
            )
            raise
        return self._order_result_from_exchange_order(symbol, order) if order else None

    def _merge_position_snapshot(self, existing: PositionSnapshot, incoming: PositionSnapshot) -> PositionSnapshot:
        if existing.side == Side.FLAT:
            return incoming if incoming.side != Side.FLAT else existing
        if incoming.side == Side.FLAT:
            return existing
        if existing.side != incoming.side:
            raise RuntimeError(f"hedged_position_not_supported:{incoming.symbol}")
        qty = abs(existing.qty) + abs(incoming.qty)
        entry_price = (
            (existing.entry_price * abs(existing.qty) + incoming.entry_price * abs(incoming.qty)) / qty
            if qty > 0
            else 0.0
        )
        return PositionSnapshot(
            symbol=existing.symbol,
            side=existing.side,
            qty=qty,
            entry_price=entry_price,
            mark_price=incoming.mark_price or existing.mark_price,
            unrealized_pnl=existing.unrealized_pnl + incoming.unrealized_pnl,
        )

    async def minimum_order_amount(self, symbol: str, price: float | None = None) -> float:
        if self.dry_run:
            reference_price = max(float(price or 0.0), 1e-9)
            return max(5.0 / reference_price, 0.0001)
        await self.exchange.load_markets()
        market = self.exchange.market(symbol)
        if market.get("contract"):
            precision_amount = (market.get("precision") or {}).get("amount")
            minimum_contracts = 1.0 if precision_amount in {0, 1, 1.0, None} else float(precision_amount)
            amount_min = (((market.get("limits") or {}).get("amount") or {}).get("min")) or 0.0
            return max(float(amount_min or 0.0), minimum_contracts)
        limits = market.get("limits", {}) or {}
        amount_min = ((limits.get("amount") or {}).get("min")) or 0.0
        cost_min = ((limits.get("cost") or {}).get("min")) or 0.0
        if amount_min:
            return float(amount_min)
        if cost_min and price:
            return float(cost_min) / max(float(price), 1e-9)
        return 0.0001

    async def minimum_order_check(self, symbol: str, price: float | None = None) -> dict[str, Any]:
        amount = await self.minimum_order_amount(symbol, price)
        contract_size = await self._contract_size(symbol)
        estimated_notional = amount * contract_size * float(price or 0.0)
        return {
            "ok": amount > 0,
            "symbol": symbol,
            "amount": amount,
            "amount_unit": "contracts" if contract_size != 1.0 else "base",
            "contract_size": contract_size,
            "reference_price": float(price or 0.0),
            "estimated_notional": estimated_notional,
            "dry_run": self.dry_run,
        }

    async def create_market_order(self, request: OrderRequest) -> OrderResult:
        exchange_amount = await self._normalize_order_amount(request.symbol, request.amount, request.reduce_only)
        if self.dry_run:
            logger.info("DRY-RUN订单: %s", request)
            return OrderResult(
                symbol=request.symbol,
                side=request.side,
                amount=exchange_amount,
                status="dry_run_created",
                dry_run=True,
                exchange_order_id=f"dry_{uuid.uuid4().hex[:12]}",
                raw={**request.model_dump(mode="json"), "exchange_amount": exchange_amount},
            )
        params: dict[str, Any] = {"text": self._gate_text(request.client_order_id)}
        if request.reduce_only:
            params["reduceOnly"] = True
            params["reduce_only"] = True
        order = await self.exchange.create_order(request.symbol, "market", request.side, exchange_amount, None, params)
        return OrderResult(
            symbol=request.symbol,
            side=request.side,
            amount=exchange_amount,
            price=order.get("average") or order.get("price"),
            status=order.get("status", "submitted"),
            dry_run=False,
            exchange_order_id=order.get("id"),
            raw=order,
        )

    async def create_stop_loss_order(
        self,
        request: OrderRequest,
        stop_price: float,
        price_type: int = 1,
    ) -> OrderResult:
        if not request.reduce_only:
            raise ValueError("native_stop_loss_order_must_be_reduce_only")
        exchange_amount = await self._normalize_order_amount(request.symbol, request.amount, request.reduce_only)
        stop_price = float(stop_price)
        if stop_price <= 0:
            raise ValueError("native_stop_loss_price_must_be_positive")
        if self.dry_run:
            logger.info("DRY-RUN native stop order: %s stop=%s", request, stop_price)
            return OrderResult(
                symbol=request.symbol,
                side=request.side,
                amount=exchange_amount,
                price=stop_price,
                status="dry_run_stop_created",
                dry_run=True,
                exchange_order_id=f"dry_stop_{uuid.uuid4().hex[:12]}",
                raw={
                    **request.model_dump(mode="json"),
                    "exchange_amount": exchange_amount,
                    "stop_loss_price": stop_price,
                    "price_type": int(price_type),
                },
            )
        params: dict[str, Any] = {
            "text": self._gate_text(request.client_order_id),
            "stopLossPrice": stop_price,
            "reduceOnly": True,
            "price_type": int(price_type),
        }
        order = await self.exchange.create_order(request.symbol, "market", request.side, exchange_amount, None, params)
        return OrderResult(
            symbol=request.symbol,
            side=request.side,
            amount=exchange_amount,
            price=stop_price,
            status=order.get("status", "stop_submitted"),
            dry_run=False,
            exchange_order_id=order.get("id"),
            raw=order,
        )

    async def cancel_order(self, symbol: str, order_id: str, *, trigger: bool = False) -> bool:
        if not order_id:
            return False
        if self.dry_run:
            logger.info("DRY-RUN cancel order: symbol=%s order_id=%s trigger=%s", symbol, order_id, trigger)
            return True
        await self.exchange.cancel_order(order_id, symbol, {"trigger": trigger})
        return True

    async def close_position(self, symbol: str, reason: str = "manual_close") -> OrderResult | None:
        positions = await self.fetch_positions([symbol])
        position = next((item for item in positions if item.symbol == symbol), None)
        if not position or position.side == Side.FLAT or abs(position.qty) <= 0:
            return None
        side = "sell" if position.side == Side.LONG else "buy"
        return await self.create_market_order(
            OrderRequest(
                symbol=symbol,
                side=side,
                amount=abs(position.qty),
                reduce_only=True,
                client_order_id=f"aiq_close_{uuid.uuid4().hex[:18]}",
                reason=reason,
            )
        )

    async def close_positions(self, symbols: list[str], reason: str = "panic_close") -> list[OrderResult]:
        results: list[OrderResult] = []
        positions = await self.fetch_positions(symbols)
        for position in positions:
            if position.side == Side.FLAT or abs(position.qty) <= 0:
                continue
            side = "sell" if position.side == Side.LONG else "buy"
            order = await self.create_market_order(
                OrderRequest(
                    symbol=position.symbol,
                    side=side,
                    amount=abs(position.qty),
                    reduce_only=True,
                    client_order_id=f"aiq_close_{uuid.uuid4().hex[:18]}",
                    reason=reason,
                )
            )
            results.append(order)
        return results

    async def close(self) -> None:
        try:
            await self.exchange.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("关闭Gate执行客户端时出现非致命异常: %s", exc)

    async def _contract_size(self, symbol: str) -> float:
        if self.dry_run:
            return 1.0
        await self.exchange.load_markets()
        market = self.exchange.market(symbol)
        return float(market.get("contractSize") or 1.0) if market.get("contract") else 1.0

    def _market_contract_size(self, symbol: str) -> float:
        try:
            market = self.exchange.market(symbol)
        except Exception:  # noqa: BLE001
            return 1.0
        return float(market.get("contractSize") or 1.0) if market.get("contract") else 1.0

    def _order_matches_client_id(self, order: dict[str, Any], client_order_id: str, gate_text: str) -> bool:
        info = order.get("info") or {}
        candidates = {
            str(order.get("clientOrderId") or ""),
            str(order.get("client_order_id") or ""),
            str(order.get("text") or ""),
            str(info.get("clientOrderId") or ""),
            str(info.get("client_order_id") or ""),
            str(info.get("text") or ""),
        }
        return client_order_id in candidates or gate_text in candidates

    def _order_result_from_exchange_order(self, symbol: str, order: dict[str, Any]) -> OrderResult:
        info = order.get("info") or {}
        amount = float(order.get("amount") or order.get("filled") or info.get("size") or 0.0)
        return OrderResult(
            symbol=str(order.get("symbol") or symbol),
            side=str(order.get("side") or info.get("side") or ""),
            amount=amount,
            price=order.get("average") or order.get("price"),
            status=str(order.get("status") or info.get("status") or "submitted"),
            dry_run=False,
            exchange_order_id=str(order.get("id") or info.get("id") or "") or None,
            raw=order,
        )

    async def _normalize_order_amount(self, symbol: str, amount: float, reduce_only: bool) -> float:
        if self.dry_run:
            return float(amount)
        await self.exchange.load_markets()
        market = self.exchange.market(symbol)
        raw_amount = float(amount)
        if not market.get("contract"):
            return raw_amount
        contract_size = float(market.get("contractSize") or 1.0)
        contracts = raw_amount / max(contract_size, 1e-12)
        if not reduce_only:
            min_contracts = await self.minimum_order_amount(symbol)
            contracts = max(contracts, min_contracts)
        precision = (market.get("precision") or {}).get("amount")
        if precision in {0, 1, 1.0, None}:
            contracts = math.ceil(contracts)
        return float(contracts)

    def _gate_text(self, client_order_id: str) -> str:
        """Gate futures order text must start with ``t-``."""
        text = str(client_order_id or uuid.uuid4().hex[:18])
        text = text if text.startswith("t-") else f"t-{text}"
        return text[:28]
