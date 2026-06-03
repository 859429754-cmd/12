from __future__ import annotations

from types import SimpleNamespace

import pytest

from ai_quant_trader.app import TradingApp
from ai_quant_trader.core.models import FollowerAccountConfig, PositionSnapshot, RiskDecision, Side, SignalAction, StrategySignal


class FollowerSizingGateway:
    def __init__(self) -> None:
        self.positions = [PositionSnapshot(symbol="ETH/USDT:USDT", side=Side.LONG, qty=0.1, mark_price=2000.0)]

    async def fetch_balance_summary(self) -> dict[str, object]:
        return {"ok": True, "usdt_total": 1000.0, "usdt_free": 900.0}

    async def fetch_positions(self, symbols: list[str]) -> list[PositionSnapshot]:
        return [position for position in self.positions if position.symbol in symbols]

    async def minimum_order_amount(self, symbol: str, price: float) -> float:
        return 0.001

    async def contract_size(self, symbol: str) -> float:
        return 1.0


@pytest.mark.asyncio
async def test_follower_entry_qty_clips_by_own_equity_leverage_and_existing_position() -> None:
    app = TradingApp.__new__(TradingApp)
    app.follower_execution = FollowerSizingGateway()

    qty, reason = await app._follower_entry_qty(
        FollowerAccountConfig(enabled=True, follow_ratio=1.0, max_leverage=2.0),
        "ETH/USDT:USDT",
        StrategySignal(
            symbol="ETH/USDT:USDT",
            timeframe="1h",
            action=SignalAction.LONG,
            current_price=2000.0,
            suggested_qty=10.0,
            signal_strength=0.8,
        ),
        RiskDecision(
            allowed=True,
            action=SignalAction.LONG,
            symbol="ETH/USDT:USDT",
            position_scale=1.0,
            position_tier="full",
            reason="unit_test",
        ),
    )

    # Account 2 has 1000 USDT and 2x max leverage = 2000 notional.
    # It already uses 0.1 ETH * 2000 = 200 notional, so only 1800 remains.
    assert qty == pytest.approx(0.9)
    assert reason == "follower_sized_from_shared_ai_decision"


def test_live_mode_does_not_activate_follower_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEIO_FOLLOWER_API_KEY", raising=False)
    monkeypatch.delenv("GATEIO_FOLLOWER_API_SECRET", raising=False)
    monkeypatch.delenv("GATEIO_RANGE_API_KEY", raising=False)
    monkeypatch.delenv("GATEIO_RANGE_API_SECRET", raising=False)

    app = TradingApp.__new__(TradingApp)
    app.config = SimpleNamespace(
        runtime=SimpleNamespace(execution_mode="live"),
        followers=[FollowerAccountConfig(enabled=True)],
    )

    assert app._active_followers() == []


def test_mock_mode_can_activate_follower_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEIO_FOLLOWER_API_KEY", raising=False)
    monkeypatch.delenv("GATEIO_FOLLOWER_API_SECRET", raising=False)

    app = TradingApp.__new__(TradingApp)
    app.config = SimpleNamespace(
        runtime=SimpleNamespace(execution_mode="mock"),
        followers=[FollowerAccountConfig(enabled=True)],
    )

    assert len(app._active_followers()) == 1
