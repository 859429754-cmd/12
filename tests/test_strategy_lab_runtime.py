from __future__ import annotations

from pathlib import Path

import pandas as pd

from ai_quant_trader.core.models import PositionSnapshot, SignalAction
from ai_quant_trader.strategy import lab


CUSTOM_CODE = """
def generate_signal(candles, position, context):
    close = float(candles["close"].iloc[-1])
    prev = float(candles["close"].iloc[-2])
    if position.qty > 0 and close < prev:
        return {"action": "EXIT_LONG", "reason": "价格转弱", "signal_strength": 0.6}
    if close > prev:
        return {"action": "LONG", "reason": "价格走强", "signal_strength": 0.8}
    return {"action": "HOLD", "reason": "无信号"}
"""


def test_strategy_lab_save_activate_and_generate_signal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(lab, "STRATEGY_LAB_DIR", tmp_path)
    monkeypatch.setattr(lab, "ACTIVE_STRATEGY_PATH", tmp_path / "active.json")

    meta = lab.save_strategy_code("中文策略", CUSTOM_CODE, "测试")
    active = lab.activate_strategy(meta["id"], ["ETH/USDT:USDT"], "tester")
    assert active["id"] == meta["id"]

    candles = pd.DataFrame(
        [
            ["2026-01-01T00:00:00Z", 100, 101, 99, 100, 1000],
            ["2026-01-01T01:00:00Z", 100, 103, 100, 102, 1200],
        ],
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    signal = lab.generate_custom_signal(
        meta["id"],
        candles,
        PositionSnapshot(symbol="ETH/USDT:USDT", qty=0, mark_price=102),
        "ETH/USDT:USDT",
        "1h",
        10_000,
    )
    assert signal.action == SignalAction.LONG
    assert signal.technical_evidence["source"] == "strategy_lab"


def test_strategy_lab_rejects_high_risk_code(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(lab, "STRATEGY_LAB_DIR", tmp_path)
    monkeypatch.setattr(lab, "ACTIVE_STRATEGY_PATH", tmp_path / "active.json")

    try:
        lab.save_strategy_code("bad", "open('x', 'w')\ndef generate_signal(candles, position, context):\n    return {'action': 'HOLD'}")
    except lab.StrategyCodeError as exc:
        assert "禁止调用" in str(exc)
    else:
        raise AssertionError("high risk strategy code should be rejected")


def test_custom_strategy_backtest_runs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(lab, "STRATEGY_LAB_DIR", tmp_path)
    monkeypatch.setattr(lab, "ACTIVE_STRATEGY_PATH", tmp_path / "active.json")
    meta = lab.save_strategy_code("回测策略", CUSTOM_CODE)
    candles = pd.DataFrame(
        [[i, 100 + i, 101 + i, 99 + i, 100 + i, 1000 + i] for i in range(150)],
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    result = lab.backtest_custom_strategy(candles, "ETH/USDT:USDT", "1h", meta["id"], warmup=2)
    assert result["strategy_id"] == meta["id"]
    assert result["trade_count"] >= 0
