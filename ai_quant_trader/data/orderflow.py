from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import ccxt.async_support as ccxt
import requests

from ai_quant_trader.core.models import OrderflowSummary

logger = logging.getLogger(__name__)


class MultiExchangeOrderflowClient:
    """Binance/OKX/Bybit 公开订单流采集。"""

    def __init__(self, exchanges: list[str], live_public_data: bool = True):
        self.exchange_names = exchanges
        self.live_public_data = live_public_data

    async def fetch_summaries(self, symbol: str) -> list[OrderflowSummary]:
        if not self.live_public_data:
            return [self._synthetic_summary(symbol, name, quality=0.75) for name in self.exchange_names]

        summaries: list[OrderflowSummary] = []
        for name in self.exchange_names:
            if name == "okx":
                try:
                    summaries.append(await self._fetch_okx_summary(symbol))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("订单流采集失败 %s %s: %s", name, symbol, exc)
                    summaries.append(self._unavailable_summary(symbol, name, quality=0.25))
                continue

            exchange = self._new_exchange(name)
            if exchange is None:
                continue
            try:
                mapped = self._map_symbol(name, symbol)
                order_book = await exchange.fetch_order_book(mapped, limit=50)
                trades = await exchange.fetch_trades(mapped, limit=100)
                summaries.append(self._summarize(symbol, name, order_book, trades))
            except Exception as exc:  # noqa: BLE001
                logger.warning("订单流采集失败 %s %s: %s", name, symbol, exc)
                summaries.append(self._unavailable_summary(symbol, name, quality=0.25))
            finally:
                try:
                    await exchange.close()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("关闭订单流客户端失败 %s: %s", name, exc)
        return summaries

    async def close(self) -> None:
        return None

    def _new_exchange(self, name: str):
        if name == "binance":
            return ccxt.binanceusdm({"enableRateLimit": True})
        if name == "okx":
            return ccxt.okx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
        if name == "bybit":
            return ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap"}})
        return None

    def _map_symbol(self, exchange: str, gate_symbol: str) -> str:
        base = gate_symbol.split("/")[0]
        if exchange in {"binance", "bybit", "okx"}:
            return f"{base}/USDT:USDT"
        return gate_symbol

    async def _fetch_okx_summary(self, symbol: str) -> OrderflowSummary:
        # ccxt.okx load_markets has returned None market ids in production, causing
        # a TypeError inside ccxt sorting. OKX orderflow is public data, so use the
        # native REST endpoints directly and keep private execution isolated.
        inst_id = self._okx_inst_id(symbol)
        order_book_payload, trades_payload = await asyncio.gather(
            asyncio.to_thread(
                self._requests_json,
                "https://www.okx.com/api/v5/market/books",
                {"instId": inst_id, "sz": 50},
            ),
            asyncio.to_thread(
                self._requests_json,
                "https://www.okx.com/api/v5/market/trades",
                {"instId": inst_id, "limit": 100},
            ),
        )
        order_book_row = self._first_data_row(order_book_payload)
        trade_rows = trades_payload.get("data") if isinstance(trades_payload, dict) else []
        if not isinstance(trade_rows, list):
            trade_rows = []
        order_book = {
            "bids": order_book_row.get("bids") or [],
            "asks": order_book_row.get("asks") or [],
        }
        trades = [
            {
                "amount": row.get("sz"),
                "price": row.get("px"),
                "side": row.get("side"),
            }
            for row in trade_rows
            if isinstance(row, dict)
        ]
        return self._summarize(symbol, "okx", order_book, trades)

    def _okx_inst_id(self, gate_symbol: str) -> str:
        base = gate_symbol.split("/")[0].split(":")[0]
        quote = "USDT"
        if "/" in gate_symbol:
            quote = gate_symbol.split("/", 1)[1].split(":")[0]
        return f"{base}-{quote}-SWAP"

    def _first_data_row(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise RuntimeError("empty_okx_orderflow_payload")
        return data[0]

    def _requests_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        response = requests.get(url, params=params, timeout=10, headers={"User-Agent": "Mozilla/5.0 ai-quant-trader"})
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("invalid_orderflow_json")
        return payload

    def _summarize(self, symbol: str, exchange: str, order_book: dict[str, Any], trades: list[dict]) -> OrderflowSummary:
        bids = order_book.get("bids") or []
        asks = order_book.get("asks") or []
        bid_depth = sum(self._level_notional(level) for level in bids[:20])
        ask_depth = sum(self._level_notional(level) for level in asks[:20])
        depth = bid_depth + ask_depth
        imbalance = (bid_depth - ask_depth) / depth if depth > 0 else 0.0
        best_bid = self._level_price(bids[0]) if bids else 0.0
        best_ask = self._level_price(asks[0]) if asks else 0.0
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else 0.0
        spread_bps = ((best_ask - best_bid) / mid * 10000) if mid else 0.0

        buy_volume = 0.0
        sell_volume = 0.0
        large_trade_events = 0
        for trade in trades:
            amount = float(trade.get("amount") or 0.0)
            price = float(trade.get("price") or mid or 0.0)
            notional = amount * price
            if notional > 250_000:
                large_trade_events += 1
            if trade.get("side") == "buy":
                buy_volume += amount
            else:
                sell_volume += amount
        ratio = buy_volume / max(sell_volume, 1e-9)
        cvd_delta = buy_volume - sell_volume
        return OrderflowSummary(
            symbol=symbol,
            exchange=exchange,
            bid_ask_imbalance=max(-1, min(1, imbalance)),
            active_buy_sell_ratio=max(0, ratio),
            cvd_delta=cvd_delta,
            spread_bps=max(0, spread_bps),
            depth_usd=max(0, depth),
            large_trade_events=large_trade_events,
            liquidity_shift=max(-1, min(1, imbalance * 0.5)),
            data_quality=1.0 if depth > 0 else 0.2,
        )

    def _level_price(self, level: list | tuple) -> float:
        if len(level) < 1 or level[0] in {None, ""}:
            return 0.0
        return float(level[0])

    def _level_amount(self, level: list | tuple) -> float:
        if len(level) < 2 or level[1] in {None, ""}:
            return 0.0
        return float(level[1])

    def _level_notional(self, level: list | tuple) -> float:
        return self._level_price(level) * self._level_amount(level)

    def _synthetic_summary(self, symbol: str, exchange: str, quality: float) -> OrderflowSummary:
        return OrderflowSummary(
            symbol=symbol,
            exchange=exchange,
            bid_ask_imbalance=random.uniform(-0.08, 0.08),
            active_buy_sell_ratio=random.uniform(0.9, 1.1),
            cvd_delta=random.uniform(-100, 100),
            spread_bps=random.uniform(1, 8),
            depth_usd=random.uniform(1_000_000, 8_000_000),
            liquidity_shift=random.uniform(-0.05, 0.05),
            data_quality=quality,
        )

    def _unavailable_summary(self, symbol: str, exchange: str, quality: float) -> OrderflowSummary:
        return OrderflowSummary(
            symbol=symbol,
            exchange=exchange,
            bid_ask_imbalance=0.0,
            active_buy_sell_ratio=1.0,
            cvd_delta=0.0,
            spread_bps=0.0,
            depth_usd=0.0,
            large_trade_events=0,
            liquidity_shift=0.0,
            data_quality=quality,
        )
