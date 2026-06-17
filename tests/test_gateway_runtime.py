from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_quant_trader.api.server import create_app
from ai_quant_trader.app import TradingApp
from ai_quant_trader.core.models import OrderRequest, OrderResult, PositionSnapshot, Side, SignalAction, StrategySignal
from ai_quant_trader.execution import gateio as gateio_module
from ai_quant_trader.execution.gateio import GateExecutionClient
from ai_quant_trader.execution.gateway.factory import create_exchange_gateway
from ai_quant_trader.execution.gateway.gate_real import GateRealGateway
from ai_quant_trader.execution.gateway.mock import MockExchangeGateway
from ai_quant_trader.strategy.trend_state import TrendStateStore
from tests.test_console_api import write_config


@pytest.mark.asyncio
async def test_mock_gateway_persists_order_and_position(tmp_path: Path) -> None:
    gateway = MockExchangeGateway(str(tmp_path / "mock_state.json"))

    balance = await gateway.fetch_balance_summary()
    assert balance["dry_run"] is True
    assert balance["usdt_total"] == 10000.0

    order = await gateway.create_market_order(
        OrderRequest(
            symbol="ETH/USDT:USDT",
            side="buy",
            amount=0.01,
            client_order_id="test_order_1",
            reason="pytest",
        )
    )
    assert order.dry_run is True
    assert order.status == "mock_created"
    assert order.exchange_order_id is not None

    position = (await gateway.fetch_positions(["ETH/USDT:USDT"]))[0]
    assert position.side == Side.LONG
    assert position.qty == pytest.approx(0.01)

    closed = await gateway.close_positions(["ETH/USDT:USDT"], reason="pytest_close")
    assert len(closed) == 1
    position_after_close = (await gateway.fetch_positions(["ETH/USDT:USDT"]))[0]
    assert position_after_close.side == Side.FLAT
    assert position_after_close.qty == 0.0


@pytest.mark.asyncio
async def test_mock_gateway_client_order_id_is_idempotent(tmp_path: Path) -> None:
    gateway = MockExchangeGateway(str(tmp_path / "mock_state.json"))
    request = OrderRequest(
        symbol="ETH/USDT:USDT",
        side="buy",
        amount=0.01,
        client_order_id="stable_client_order_id",
        reason="pytest",
    )

    first = await gateway.create_market_order(request)
    second = await gateway.create_market_order(request)
    orders = gateway._load_state()["orders"]  # noqa: SLF001

    assert second.exchange_order_id == first.exchange_order_id
    assert len([order for order in orders if (order.get("raw") or {}).get("client_order_id") == "stable_client_order_id"]) == 1


@pytest.mark.asyncio
async def test_gate_contract_positions_are_normalized_to_base_qty() -> None:
    class FakeGateExchange:
        async def load_markets(self):
            return None

        async def fetch_positions(self, symbols):
            return [
                {
                    "symbol": "ETH/USDT:USDT",
                    "contracts": 3,
                    "side": "long",
                    "markPrice": 2000.0,
                    "entryPrice": 1900.0,
                }
            ]

        def market(self, symbol):
            return {
                "symbol": symbol,
                "contract": True,
                "contractSize": 0.01,
                "precision": {"amount": 0},
                "limits": {"amount": {"min": 1}},
            }

        async def close(self):
            return None

    client = GateExecutionClient(dry_run=False)
    await client.exchange.close()
    client.exchange = FakeGateExchange()

    position = (await client.fetch_positions(["ETH/USDT:USDT"]))[0]

    assert position.qty == pytest.approx(0.03)
    assert position.notional == pytest.approx(60.0)
    assert await client._normalize_order_amount("ETH/USDT:USDT", 0.025, reduce_only=False) == pytest.approx(3.0)
    assert await client._normalize_order_amount("ETH/USDT:USDT", 0.025, reduce_only=True) == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_gate_balance_missing_usdt_total_does_not_return_fake_zero(monkeypatch) -> None:
    class FakeGateExchange:
        async def fetch_balance(self):
            return {"total": {}, "free": {}, "used": {}}

        async def close(self):
            return None

    monkeypatch.setenv("GATEIO_API_KEY", "test_key")
    monkeypatch.setenv("GATEIO_API_SECRET", "test_secret")
    client = GateExecutionClient(dry_run=False)
    await client.exchange.close()
    client.exchange = FakeGateExchange()

    with pytest.raises(RuntimeError, match="gate_balance_missing_usdt_total"):
        await client.fetch_balance_summary()


@pytest.mark.asyncio
async def test_gate_duplicate_flat_positions_are_deduplicated() -> None:
    class FakeGateExchange:
        async def load_markets(self):
            return None

        async def fetch_positions(self, symbols):
            return [
                {"symbol": "ETH/USDT:USDT", "contracts": 0, "side": "long", "markPrice": 2000.0},
                {"symbol": "ETH/USDT:USDT", "contracts": 0, "side": "short", "markPrice": 2000.0},
            ]

        def market(self, symbol):
            return {"symbol": symbol, "contract": True, "contractSize": 0.01}

        async def close(self):
            return None

    client = GateExecutionClient(dry_run=False)
    await client.exchange.close()
    client.exchange = FakeGateExchange()

    positions = await client.fetch_positions(["ETH/USDT:USDT"])

    assert len(positions) == 1
    assert positions[0].side == Side.FLAT
    assert positions[0].qty == 0.0


@pytest.mark.asyncio
async def test_gate_hedged_position_blocks_unsupported_live_mode() -> None:
    class FakeGateExchange:
        async def load_markets(self):
            return None

        async def fetch_positions(self, symbols):
            return [
                {"symbol": "ETH/USDT:USDT", "contracts": 1, "side": "long", "markPrice": 2000.0},
                {"symbol": "ETH/USDT:USDT", "contracts": 1, "side": "short", "markPrice": 2000.0},
            ]

        def market(self, symbol):
            return {"symbol": symbol, "contract": True, "contractSize": 0.01}

        async def close(self):
            return None

    client = GateExecutionClient(dry_run=False)
    await client.exchange.close()
    client.exchange = FakeGateExchange()

    with pytest.raises(RuntimeError, match="hedged_position_not_supported"):
        await client.fetch_positions(["ETH/USDT:USDT"])


@pytest.mark.asyncio
async def test_gate_order_not_found_status_returns_none() -> None:
    class FakeGateExchange:
        async def load_markets(self):
            return None

        async def fetch_order(self, exchange_order_id, symbol):
            raise gateio_module.ccxt.OrderNotFound("missing")

        async def close(self):
            return None

    client = GateExecutionClient(dry_run=False)
    await client.exchange.close()
    client.exchange = FakeGateExchange()

    found = await client.fetch_order_by_exchange_id("ETH/USDT:USDT", "missing_order")

    assert found is None


@pytest.mark.asyncio
async def test_gate_native_stop_loss_uses_trigger_order_params() -> None:
    class FakeGateExchange:
        def __init__(self):
            self.created = None

        async def load_markets(self):
            return None

        def market(self, symbol):
            return {
                "symbol": symbol,
                "contract": True,
                "contractSize": 0.01,
                "precision": {"amount": 0},
                "limits": {"amount": {"min": 1}},
            }

        async def create_order(self, symbol, order_type, side, amount, price, params):
            self.created = {
                "symbol": symbol,
                "order_type": order_type,
                "side": side,
                "amount": amount,
                "price": price,
                "params": params,
            }
            return {"id": "stop_123", "status": "open", "average": None, "price": None}

        async def close(self):
            return None

    client = GateExecutionClient(dry_run=False)
    await client.exchange.close()
    fake = FakeGateExchange()
    client.exchange = fake

    order = await client.create_stop_loss_order(
        OrderRequest(
            symbol="ETH/USDT:USDT",
            side="sell",
            amount=0.025,
            reduce_only=True,
            client_order_id="native_stop_test",
            reason="pytest",
        ),
        1980.5,
    )

    assert order.exchange_order_id == "stop_123"
    assert fake.created["symbol"] == "ETH/USDT:USDT"
    assert fake.created["order_type"] == "market"
    assert fake.created["side"] == "sell"
    assert fake.created["amount"] == pytest.approx(3.0)
    assert fake.created["price"] is None
    assert fake.created["params"]["stopLossPrice"] == pytest.approx(1980.5)
    assert fake.created["params"]["reduceOnly"] is True
    assert fake.created["params"]["price_type"] == 1
    assert fake.created["params"]["text"].startswith("t-")


@pytest.mark.asyncio
async def test_gate_cancel_trigger_order_uses_trigger_param() -> None:
    class FakeGateExchange:
        def __init__(self):
            self.cancelled = None

        async def cancel_order(self, order_id, symbol, params):
            self.cancelled = {"order_id": order_id, "symbol": symbol, "params": params}
            return {"id": order_id}

        async def close(self):
            return None

    client = GateExecutionClient(dry_run=False)
    await client.exchange.close()
    fake = FakeGateExchange()
    client.exchange = fake

    assert await client.cancel_order("ETH/USDT:USDT", "stop_123", trigger=True) is True
    assert fake.cancelled == {
        "order_id": "stop_123",
        "symbol": "ETH/USDT:USDT",
        "params": {"trigger": True},
    }


def test_trend_state_records_native_stop_order_id(tmp_path: Path) -> None:
    store = TrendStateStore(str(tmp_path / "state_trend.json"))

    state = store.record_entry("ETH/USDT:USDT", Side.LONG, 2200.0, 25.0, 2.0)
    assert state.native_stop_order_id is None

    updated = store.set_native_stop_order_id("ETH/USDT:USDT", "stop_123")

    assert updated is not None
    assert updated.native_stop_order_id == "stop_123"
    assert store.get("ETH/USDT:USDT").native_stop_order_id == "stop_123"


def test_runtime_mode_requires_configured_trend_account_for_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ["GATEIO_API_KEY", "GATEIO_API_SECRET", "GATEIO_TREND_API_KEY", "GATEIO_TREND_API_SECRET"]:
        monkeypatch.setenv(key, "")
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    client = TestClient(create_app(str(config_path)))

    missing_account = client.post("/api/control/runtime-mode", json={"operator_id": "tester", "dry_run": False})
    assert missing_account.status_code == 403

    monkeypatch.setenv("GATEIO_TREND_API_KEY", "trend_key")
    monkeypatch.setenv("GATEIO_TREND_API_SECRET", "trend_secret")

    ok = client.post("/api/control/runtime-mode", json={"operator_id": "tester", "dry_run": False})
    assert ok.status_code == 200
    live_status = client.get("/api/status").json()
    assert live_status["dry_run"] is False
    assert live_status["execution_mode"] == "live"

    back_to_mock = client.post("/api/control/runtime-mode", json={"operator_id": "tester", "dry_run": True})
    assert back_to_mock.status_code == 200
    mock_status = client.get("/api/status").json()
    assert mock_status["dry_run"] is True
    assert mock_status["execution_mode"] == "mock"


def test_live_gateway_can_bind_trend_account_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEIO_TREND_API_KEY", "trend_key")
    monkeypatch.setenv("GATEIO_TREND_API_SECRET", "trend_secret")

    gateway = create_exchange_gateway("live", account_slot="trend")

    assert isinstance(gateway, GateRealGateway)
    assert gateway.account_slot == "trend"
    assert gateway.api_key_env == "GATEIO_TREND_API_KEY"
    assert gateway.api_secret_env == "GATEIO_TREND_API_SECRET"


def test_live_gateway_uses_legacy_gate_key_for_trend_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEIO_TREND_API_KEY", raising=False)
    monkeypatch.delenv("GATEIO_TREND_API_SECRET", raising=False)
    monkeypatch.setenv("GATEIO_API_KEY", "legacy_key")
    monkeypatch.setenv("GATEIO_API_SECRET", "legacy_secret")

    gateway = create_exchange_gateway("live", account_slot="trend")

    assert isinstance(gateway, GateRealGateway)
    assert gateway.account_slot == "trend"
    assert gateway.api_key_env == "GATEIO_API_KEY"
    assert gateway.api_secret_env == "GATEIO_API_SECRET"


def test_live_gateway_can_bind_follower_account_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEIO_FOLLOWER_API_KEY", "follower_key")
    monkeypatch.setenv("GATEIO_FOLLOWER_API_SECRET", "follower_secret")

    gateway = create_exchange_gateway("live", account_slot="follower")

    assert isinstance(gateway, GateRealGateway)
    assert gateway.account_slot == "follower"
    assert gateway.api_key_env == "GATEIO_FOLLOWER_API_KEY"
    assert gateway.api_secret_env == "GATEIO_FOLLOWER_API_SECRET"


def test_live_gateway_uses_range_key_for_range_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEIO_FOLLOWER_API_KEY", raising=False)
    monkeypatch.delenv("GATEIO_FOLLOWER_API_SECRET", raising=False)
    monkeypatch.setenv("GATEIO_RANGE_API_KEY", "range_key")
    monkeypatch.setenv("GATEIO_RANGE_API_SECRET", "range_secret")

    gateway = create_exchange_gateway("live", account_slot="range")

    assert isinstance(gateway, GateRealGateway)
    assert gateway.account_slot == "range"
    assert gateway.api_key_env == "GATEIO_RANGE_API_KEY"
    assert gateway.api_secret_env == "GATEIO_RANGE_API_SECRET"


def test_live_gateway_rejects_unknown_account_slot() -> None:
    with pytest.raises(ValueError, match="unsupported_gate_account_slot"):
        create_exchange_gateway("live", account_slot="unknown")


@pytest.mark.asyncio
async def test_live_position_fetch_failure_blocks_trading_cycle(tmp_path: Path) -> None:
    class FailingGateway:
        async def fetch_positions(self, symbols):
            raise RuntimeError("exchange_down")

        async def close(self):
            return None

    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    app = TradingApp(str(config_path))
    app.config.runtime.execution_mode = "live"
    app.config.runtime.dry_run = False
    app.execution = FailingGateway()

    with pytest.raises(RuntimeError, match="live_position_fetch_failed_blocking_trading_cycle"):
        await app._fetch_positions(["ETH/USDT:USDT"])

    await app.close()


@pytest.mark.asyncio
async def test_live_effective_equity_uses_exchange_balance(tmp_path: Path) -> None:
    class BalanceGateway:
        async def fetch_balance_summary(self):
            return {"ok": True, "usdt_total": 385.5, "usdt_free": 300.0}

        async def close(self):
            return None

    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    app = TradingApp(str(config_path))
    app.config.runtime.execution_mode = "live"
    app.config.runtime.dry_run = False
    app.execution = BalanceGateway()

    assert await app._effective_equity(10_000.0) == pytest.approx(385.5)

    await app.close()


@pytest.mark.asyncio
async def test_live_effective_equity_failure_blocks_trading_cycle(tmp_path: Path) -> None:
    class BalanceGateway:
        async def fetch_balance_summary(self):
            raise RuntimeError("balance_down")

        async def close(self):
            return None

    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    app = TradingApp(str(config_path))
    app.config.runtime.execution_mode = "live"
    app.config.runtime.dry_run = False
    app.execution = BalanceGateway()

    with pytest.raises(RuntimeError, match="live_balance_fetch_failed_blocking_trading_cycle"):
        await app._effective_equity(10_000.0)

    await app.close()


def test_local_trend_state_blocks_same_direction_duplicate_entry(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    app = TradingApp(str(config_path))
    app.trend_state = app.trend_state.__class__(str(tmp_path / "state_trend.json"))
    app.trend_state.record_entry("ETH/USDT:USDT", Side.LONG, 2200.0, 25.0, 2.0)

    assert app._same_direction_trend_state_exists(
        "ETH/USDT:USDT",
        StrategySignal(symbol="ETH/USDT:USDT", timeframe="1h", action=SignalAction.LONG, current_price=2210.0),
    )
    assert not app._same_direction_trend_state_exists(
        "ETH/USDT:USDT",
        StrategySignal(symbol="ETH/USDT:USDT", timeframe="1h", action=SignalAction.SHORT, current_price=2190.0),
    )


@pytest.mark.asyncio
async def test_hourly_trading_loop_runs_startup_cycle_before_sleep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    app = TradingApp(str(config_path))
    calls: list[str] = []

    async def fake_cycle(*, equity: float, live_news: bool, heartbeat_reason: str) -> None:
        calls.append(heartbeat_reason)
        raise asyncio.CancelledError()

    monkeypatch.setattr(app, "_run_trading_cycle_with_heartbeat", fake_cycle)

    with pytest.raises(asyncio.CancelledError):
        await app._hourly_trading_loop(equity=1000.0, live_news=False)

    assert calls == ["trading_startup_cycle_ok"]
    await app.close()


@pytest.mark.asyncio
async def test_trading_app_places_native_stop_after_entry_state(tmp_path: Path) -> None:
    class RecordingGateway:
        def __init__(self):
            self.stop_request = None

        async def create_stop_loss_order(self, request, stop_price, price_type=1):
            self.stop_request = {"request": request, "stop_price": stop_price, "price_type": price_type}
            return OrderResult(
                symbol=request.symbol,
                side=request.side,
                amount=request.amount,
                price=stop_price,
                status="mock_stop_created",
                dry_run=True,
                exchange_order_id="stop_123",
            )

        async def close(self):
            return None

    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    app = TradingApp(str(config_path))
    app.trend_state = TrendStateStore(str(tmp_path / "state_trend.json"))
    app.execution = RecordingGateway()
    state = app.trend_state.record_entry("ETH/USDT:USDT", Side.LONG, 2200.0, 25.0, 2.0)
    signal = StrategySignal(symbol="ETH/USDT:USDT", timeframe="1h", action=SignalAction.LONG, current_price=2200.0)

    await app._place_native_stop_loss("ETH/USDT:USDT", signal, state, amount=0.02)

    assert app.execution.stop_request["request"].side == "sell"
    assert app.execution.stop_request["request"].reduce_only is True
    assert app.execution.stop_request["stop_price"] == pytest.approx(2150.0)
    assert app.trend_state.get("ETH/USDT:USDT").native_stop_order_id == "stop_123"
    await app.close()


@pytest.mark.asyncio
async def test_trading_app_emergency_closes_when_native_stop_submit_fails(tmp_path: Path) -> None:
    class FailingStopGateway:
        def __init__(self):
            self.closed = False

        async def create_stop_loss_order(self, request, stop_price, price_type=1):
            raise RuntimeError("stop_api_down")

        async def fetch_positions(self, symbols):
            return [PositionSnapshot(symbol=symbols[0], side=Side.LONG, qty=0.02, mark_price=2200.0)]

        async def create_market_order(self, request):
            self.closed = True
            return OrderResult(
                symbol=request.symbol,
                side=request.side,
                amount=request.amount,
                status="mock_closed",
                dry_run=True,
                exchange_order_id="close_123",
                raw=request.model_dump(mode="json"),
            )

        async def close(self):
            return None

    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    app = TradingApp(str(config_path))
    app.trend_state = TrendStateStore(str(tmp_path / "state_trend.json"))
    app.execution = FailingStopGateway()
    state = app.trend_state.record_entry("ETH/USDT:USDT", Side.LONG, 2200.0, 25.0, 2.0)
    signal = StrategySignal(symbol="ETH/USDT:USDT", timeframe="1h", action=SignalAction.LONG, current_price=2200.0)

    with pytest.raises(RuntimeError, match="native_stop_loss_submit_failed"):
        await app._place_native_stop_loss("ETH/USDT:USDT", signal, state, amount=0.02)

    assert app.execution.closed is True
    assert app.trend_state.get("ETH/USDT:USDT") is None
    await app.close()


@pytest.mark.asyncio
async def test_live_native_stop_unknown_requires_manual_gate_without_auto_close(tmp_path: Path) -> None:
    class UnknownStopGateway:
        mode = "live"

        def __init__(self):
            self.closed = False

        async def create_stop_loss_order(self, request, stop_price, price_type=1):
            raise TimeoutError("stop_submit_timeout")

        async def close_position(self, symbol, reason="manual_close"):
            self.closed = True
            return OrderResult(
                symbol=symbol,
                side="sell",
                amount=0.02,
                status="mock_closed",
                dry_run=False,
                exchange_order_id="close_123",
            )

        async def close(self):
            return None

    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    app = TradingApp(str(config_path))
    app.config.runtime.execution_mode = "live"
    app.config.runtime.dry_run = False
    app.trend_state = TrendStateStore(str(tmp_path / "state_trend.json"))
    app.execution = UnknownStopGateway()
    state = app.trend_state.record_entry("ETH/USDT:USDT", Side.LONG, 2200.0, 25.0, 2.0)
    signal = StrategySignal(symbol="ETH/USDT:USDT", timeframe="1h", action=SignalAction.LONG, current_price=2200.0)

    with pytest.raises(RuntimeError, match="native_stop_loss_state_unknown_manual_gate_required"):
        await app._place_native_stop_loss("ETH/USDT:USDT", signal, state, amount=0.02)

    assert app.execution.closed is False
    latest_health = app.store.fetch_latest("exchange_health")["payload"]
    assert latest_health["can_open_new_entries"] is False
    assert latest_health["reason"].startswith("native_stop_submit_")
    await app.close()


@pytest.mark.asyncio
async def test_native_stop_cancel_failure_is_audited_without_clearing_state(tmp_path: Path) -> None:
    class FailingCancelGateway:
        mode = "live"

        async def cancel_order(self, symbol, order_id, *, trigger=False):
            raise TimeoutError("cancel_timeout")

        async def close(self):
            return None

    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    app = TradingApp(str(config_path))
    app.config.runtime.execution_mode = "live"
    app.trend_state = TrendStateStore(str(tmp_path / "state_trend.json"))
    state = app.trend_state.record_entry("ETH/USDT:USDT", Side.LONG, 2200.0, 25.0, 2.0)
    app.trend_state.set_native_stop_order_id("ETH/USDT:USDT", "stop_123")
    app.execution = FailingCancelGateway()

    await app._cancel_native_stop_order("ETH/USDT:USDT")
    latest = app.store.fetch_latest("order_lifecycle")["payload"]

    assert state is not None
    assert latest["status"] == "cancel_failed"
    assert latest["error_type"] == "TimeoutError"
    assert app.trend_state.get("ETH/USDT:USDT").native_stop_order_id == "stop_123"
    await app.close()


@pytest.mark.asyncio
async def test_order_status_worker_refreshes_live_reconciliation(tmp_path: Path) -> None:
    class HealthyLiveGateway:
        mode = "live"

        async def fetch_balance_summary(self):
            return {"ok": True, "usdt_total": 1000.0}

        async def fetch_positions(self, symbols):
            return [PositionSnapshot(symbol=symbol, side=Side.FLAT, qty=0.0, mark_price=0.0) for symbol in symbols]

        async def fetch_open_orders(self, symbols):
            return []

        async def close(self):
            return None

    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    app = TradingApp(str(config_path))
    app.config.runtime.execution_mode = "live"
    app.config.runtime.dry_run = False
    app.execution = HealthyLiveGateway()

    state = await app._refresh_reconciliation_and_order_status_once(["ETH/USDT:USDT"])  # noqa: SLF001

    reconciliation = app.store.fetch_latest("reconciliation_runs")["payload"]
    exchange_health = app.store.fetch_latest("exchange_health")["payload"]
    assert reconciliation["status"] == "ok"
    assert exchange_health["reason"] == "exchange_reconciliation_ok"
    assert state.can_open_new_entries is True
    await app.close()


@pytest.mark.asyncio
async def test_trading_app_close_closes_follower_execution(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    app = TradingApp(str(config_path))
    closed: list[str] = []

    class Closeable:
        def __init__(self, name: str) -> None:
            self.name = name

        async def close(self) -> None:
            closed.append(self.name)

    app.market = Closeable("market")
    app.orderflow_client = Closeable("orderflow")
    app.execution = Closeable("primary")
    app.follower_execution = Closeable("follower")

    await app.close()

    assert closed == ["market", "orderflow", "primary", "follower"]
