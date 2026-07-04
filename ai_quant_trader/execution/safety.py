from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from ai_quant_trader.core.models import ExchangeConnectionStatus, ExchangeSafetyState, PositionSnapshot, ReconciliationReport, Side
from ai_quant_trader.strategy.trend_state import TrendPositionState, TrendStateStore


class ExchangeSafetyMonitor:
    """Fail-closed safety gate for live exchange state.

    Existing positions are not auto-closed here. When private exchange state is
    stale or inconsistent, the trading loop must stop opening new positions and
    surface a manual Gate-side handling instruction.
    """

    def __init__(self, stale_after_seconds: int = 300):
        self.stale_after_seconds = stale_after_seconds
        self._state = ExchangeSafetyState(
            status=ExchangeConnectionStatus.RECONCILIATION_REQUIRED,
            can_open_new_entries=False,
            reason="exchange_reconciliation_not_run",
            stale_after_seconds=stale_after_seconds,
        )

    @property
    def state(self) -> ExchangeSafetyState:
        if self._state.last_success_at is None:
            return self._state
        if datetime.now(UTC) - self._state.last_success_at > timedelta(seconds=self.stale_after_seconds):
            return self._state.model_copy(
                update={
                    "status": ExchangeConnectionStatus.DEGRADED_READONLY,
                    "can_open_new_entries": False,
                    "reason": "exchange_private_state_stale_over_5m",
                    "checked_at": datetime.now(UTC),
                }
            )
        return self._state

    def mark_success(self, reason: str = "exchange_private_state_verified") -> ExchangeSafetyState:
        now = datetime.now(UTC)
        self._state = ExchangeSafetyState(
            status=ExchangeConnectionStatus.OK,
            can_open_new_entries=True,
            reason=reason,
            last_success_at=now,
            checked_at=now,
            stale_after_seconds=self.stale_after_seconds,
        )
        return self._state

    def mark_failure(self, reason: str, failures: list[str] | None = None) -> ExchangeSafetyState:
        last_success = self._state.last_success_at
        status = ExchangeConnectionStatus.DEGRADED_READONLY
        if last_success is None:
            status = ExchangeConnectionStatus.RECONCILIATION_REQUIRED
        elif datetime.now(UTC) - last_success > timedelta(seconds=self.stale_after_seconds):
            status = ExchangeConnectionStatus.DEGRADED_READONLY
        self._state = ExchangeSafetyState(
            status=status,
            can_open_new_entries=False,
            reason=reason,
            last_success_at=last_success,
            checked_at=datetime.now(UTC),
            stale_after_seconds=self.stale_after_seconds,
            failures=failures or [reason],
        )
        return self._state

    async def reconcile(
        self,
        gateway: Any,
        symbols: list[str],
        trend_state: TrendStateStore,
        *,
        live: bool,
    ) -> ReconciliationReport:
        issues: list[str] = []
        balance_ok = False
        positions_ok = False
        open_orders_ok = False
        native_stops_ok = True
        local_state_ok = True
        positions: list[PositionSnapshot] = []

        try:
            balance = await gateway.fetch_balance_summary()
            balance_ok = bool(balance.get("ok", True))
            if live and not balance_ok:
                issues.append("exchange_balance_unavailable")
        except Exception as exc:  # noqa: BLE001
            issues.append(f"exchange_balance_error:{type(exc).__name__}")

        try:
            positions = await gateway.fetch_positions(symbols)
            positions_ok = True
        except Exception as exc:  # noqa: BLE001
            issues.append(f"exchange_positions_error:{type(exc).__name__}")

        try:
            open_orders = await gateway.fetch_open_orders(symbols)
            open_orders_ok = True
        except AttributeError:
            open_orders = []
            open_orders_ok = not live
            if live:
                issues.append("exchange_open_orders_not_supported")
        except Exception as exc:  # noqa: BLE001
            open_orders = []
            issues.append(f"exchange_open_orders_error:{type(exc).__name__}")

        if live and positions_ok:
            for position in positions:
                local = trend_state.get(position.symbol)
                if position.side.value != "flat" and abs(position.qty) > 0:
                    if local is None:
                        issues.append(f"orphan_position_without_local_trend_state:{position.symbol}")
                        local_state_ok = False
                    elif not local.native_stop_order_id:
                        issues.append(f"native_stop_state_missing:{position.symbol}")
                        native_stops_ok = False
                    else:
                        stop_issues = await self._verify_native_stop_order(gateway, position, local, local.native_stop_order_id)
                        if stop_issues:
                            issues.extend(stop_issues)
                            native_stops_ok = False
                elif local is not None:
                    issues.append(f"local_trend_state_without_exchange_position:{position.symbol}")
                    local_state_ok = False

        if live and open_orders_ok:
            for order in open_orders:
                symbol = str(order.get("symbol") or "")
                reduce_only = bool(order.get("reduceOnly") or order.get("reduce_only") or (order.get("info") or {}).get("reduce_only"))
                if symbol in symbols and not reduce_only:
                    issues.append(f"open_non_reduce_order_requires_review:{symbol}")
                    open_orders_ok = False

        status = ExchangeConnectionStatus.OK if not issues else ExchangeConnectionStatus.RECONCILIATION_REQUIRED
        report = ReconciliationReport(
            status=status,
            symbol_count=len(symbols),
            balance_ok=balance_ok,
            positions_ok=positions_ok,
            open_orders_ok=open_orders_ok,
            native_stops_ok=native_stops_ok,
            local_state_ok=local_state_ok,
            issues=issues,
        )
        if status == ExchangeConnectionStatus.OK:
            self.mark_success("exchange_reconciliation_ok")
        else:
            self._state = ExchangeSafetyState(
                status=status,
                can_open_new_entries=False,
                reason="exchange_reconciliation_required",
                last_success_at=self._state.last_success_at,
                checked_at=datetime.now(UTC),
                stale_after_seconds=self.stale_after_seconds,
                failures=issues,
            )
        return report

    async def _verify_native_stop_order(
        self,
        gateway: Any,
        position: PositionSnapshot,
        local: TrendPositionState,
        native_stop_order_id: str,
    ) -> list[str]:
        fetcher = getattr(gateway, "fetch_order_by_exchange_id", None)
        if not callable(fetcher):
            return [f"native_stop_order_verification_not_supported:{position.symbol}"]
        try:
            order = await fetcher(position.symbol, native_stop_order_id)
        except Exception as exc:  # noqa: BLE001
            return [f"native_stop_order_verify_error:{position.symbol}:{type(exc).__name__}"]
        if order is None:
            return [f"native_stop_order_not_found_on_exchange:{position.symbol}"]
        issues: list[str] = []
        raw = getattr(order, "raw", {}) or {}
        reduce_only = bool(raw.get("reduce_only") or raw.get("reduceOnly") or raw.get("reduce_only_order") or raw.get("close"))
        if not reduce_only:
            issues.append(f"native_stop_order_not_reduce_only:{position.symbol}")
        expected_side = "sell" if position.side == Side.LONG else "buy"
        actual_side = str(getattr(order, "side", "") or raw.get("side") or "").lower()
        if actual_side and actual_side != expected_side:
            issues.append(f"native_stop_order_side_mismatch:{position.symbol}:{actual_side}:{expected_side}")
        order_amount = self._float_or_none(getattr(order, "amount", None) or raw.get("amount") or raw.get("size"))
        required_amount = abs(float(position.qty))
        amount_tolerance = max(required_amount * 0.001, 1e-9)
        if order_amount is None or order_amount <= 0:
            issues.append(f"native_stop_order_amount_missing:{position.symbol}")
        elif order_amount + amount_tolerance < required_amount:
            issues.append(
                f"native_stop_order_amount_under_covers_position:{position.symbol}:{order_amount:.12g}:{required_amount:.12g}"
            )
        expected_trigger_price = float(local.stop_loss_price)
        actual_trigger_price = self._native_stop_trigger_price(order)
        price_tolerance = max(abs(expected_trigger_price) * 0.0005, 0.1)
        if actual_trigger_price is None or actual_trigger_price <= 0:
            issues.append(f"native_stop_order_trigger_price_missing:{position.symbol}")
        elif abs(actual_trigger_price - expected_trigger_price) > price_tolerance:
            issues.append(
                f"native_stop_order_trigger_price_mismatch:{position.symbol}:{actual_trigger_price:.12g}:{expected_trigger_price:.12g}"
            )
        return issues

    def _native_stop_trigger_price(self, order: Any) -> float | None:
        raw = getattr(order, "raw", {}) or {}
        info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
        candidates = [
            raw.get("stop_loss_price"),
            raw.get("stopLossPrice"),
            raw.get("stop_price"),
            raw.get("stopPrice"),
            raw.get("trigger_price"),
            raw.get("triggerPrice"),
            raw.get("trigger"),
            info.get("stop_loss_price"),
            info.get("stopLossPrice"),
            info.get("stop_price"),
            info.get("stopPrice"),
            info.get("trigger_price"),
            info.get("triggerPrice"),
            info.get("trigger"),
            getattr(order, "price", None),
        ]
        for value in candidates:
            parsed = self._float_or_none(value)
            if parsed is not None and parsed > 0:
                return parsed
        return None

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
