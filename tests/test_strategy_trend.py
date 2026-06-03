from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from ai_quant_trader.app import TradingApp
from ai_quant_trader.core.models import PositionSnapshot, SignalAction, TrendStrategyConfig
from ai_quant_trader.strategy.trend import TrendStrategy
from ai_quant_trader.strategy.lab import backtest_trend_strategy, optimize_trend_parameters
from ai_quant_trader.strategy.indicators import atr, keltner_channel
from ai_quant_trader.strategy.trend_state import TrendStateStore


def _candles_with_breakout(direction: str) -> pd.DataFrame:
    rows = []
    price = 100.0
    for i in range(120):
        price *= 1.001
        rows.append(
            {
                "timestamp": i,
                "open": price * 0.995,
                "high": price * 1.005,
                "low": price * 0.995,
                "close": price,
                "volume": 1000,
            }
        )
    if direction == "long":
        rows[-1]["close"] = rows[-2]["close"] * 1.08
        rows[-1]["high"] = rows[-1]["close"] * 1.01
        rows[-1]["volume"] = 5000
    else:
        rows[-1]["close"] = rows[-2]["close"] * 0.92
        rows[-1]["low"] = rows[-1]["close"] * 0.99
        rows[-1]["volume"] = 5000
    return pd.DataFrame(rows)


def test_trend_strategy_generates_long_signal() -> None:
    strategy = TrendStrategy(TrendStrategyConfig())
    signal = strategy.generate_signal(
        "ETH/USDT:USDT",
        "1h",
        _candles_with_breakout("long"),
        PositionSnapshot(symbol="ETH/USDT:USDT"),
        equity=1000,
        ai_multiplier=1.0,
    )
    assert signal.action == SignalAction.LONG
    assert signal.signal_strength > 0.5
    assert signal.suggested_qty > 0
    assert signal.technical_evidence["atr_stop_multiple"] == 1.5
    assert signal.technical_evidence["position_fraction"] == 0.5
    assert signal.technical_evidence["stop_loss_estimate"] is not None


def test_trend_strategy_uses_configured_leverage_for_live_sizing() -> None:
    strategy = TrendStrategy(TrendStrategyConfig(position_fraction=0.5))
    signal = strategy.generate_signal(
        "ETH/USDT:USDT",
        "1h",
        _candles_with_breakout("long"),
        PositionSnapshot(symbol="ETH/USDT:USDT"),
        equity=1000,
        ai_multiplier=1.0,
        leverage=8.0,
    )
    assert signal.action == SignalAction.LONG
    assert signal.technical_evidence["target_leverage"] == 8.0
    assert signal.technical_evidence["base_nominal"] == 4000.0
    assert signal.suggested_qty == pytest.approx(4000.0 / signal.current_price)


def test_trend_strategy_generates_short_signal() -> None:
    strategy = TrendStrategy(TrendStrategyConfig())
    signal = strategy.generate_signal(
        "ETH/USDT:USDT",
        "1h",
        _candles_with_breakout("short"),
        PositionSnapshot(symbol="ETH/USDT:USDT"),
        equity=1000,
        ai_multiplier=1.0,
    )
    assert signal.action == SignalAction.SHORT
    assert signal.signal_strength > 0.5


def test_trend_strategy_records_signal_candle_timestamps() -> None:
    candles = _candles_with_breakout("short")
    start = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    candles["timestamp"] = [start + timedelta(hours=i) for i in range(len(candles))]
    strategy = TrendStrategy(TrendStrategyConfig())

    signal = strategy.generate_signal(
        "ETH/USDT:USDT",
        "1h",
        candles,
        PositionSnapshot(symbol="ETH/USDT:USDT"),
        equity=1000,
        ai_multiplier=1.0,
    )

    assert signal.action == SignalAction.SHORT
    assert signal.technical_evidence["signal_candle_time"] == "2026-06-05T23:00:00Z"
    assert signal.technical_evidence["signal_candle_close_time"] == "2026-06-06T00:00:00Z"
    assert signal.technical_evidence["prev_candle_close_time"] == "2026-06-05T23:00:00Z"


def test_trend_backtest_uses_atr_stop_multiple_in_trade_ledger() -> None:
    candles = _candles_with_breakout("long")
    entry_probe = TrendStrategy(TrendStrategyConfig()).add_indicators(candles).iloc[-1]
    atr_value = float(entry_probe["atr"])
    entry_close = float(candles.iloc[-1]["close"])
    candles = pd.concat(
        [
            candles,
            pd.DataFrame(
                [
                    {
                        "timestamp": len(candles),
                        "open": entry_close,
                        "high": entry_close * 1.01,
                        "low": entry_close - atr_value * 3.4,
                        "close": entry_close * 1.005,
                        "volume": 1000,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    result = backtest_trend_strategy(
        candles,
        "ETH/USDT:USDT",
        "1h",
        TrendStrategyConfig(atr_stop_multiple=3.0),
        slippage_bps=2.0,
    )
    assert result["trade_count"] >= 1
    assert len(result["trade_ledger"]) == result["trade_count"]
    trade = result["trades"][-1]
    assert trade["exit_reason"] == "atr_stop"
    assert trade["stop_loss_price"] is not None
    assert trade["max_adverse_excursion"] < 0
    assert trade["intrabar_path"] == "open->low->high->close"


def test_trend_backtest_respects_configured_leverage() -> None:
    candles = _candles_with_breakout("long")
    entry_probe = TrendStrategy(TrendStrategyConfig()).add_indicators(candles).iloc[-1]
    atr_value = float(entry_probe["atr"])
    entry_close = float(candles.iloc[-1]["close"])
    candles = pd.concat(
        [
            candles,
            pd.DataFrame(
                [
                    {
                        "timestamp": len(candles),
                        "open": entry_close,
                        "high": entry_close * 1.01,
                        "low": entry_close - atr_value * 3.4,
                        "close": entry_close * 1.005,
                        "volume": 1000,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    one_x = backtest_trend_strategy(candles, "ETH/USDT:USDT", "1h", TrendStrategyConfig(), leverage=1.0)
    four_x = backtest_trend_strategy(candles, "ETH/USDT:USDT", "1h", TrendStrategyConfig(), leverage=4.0)

    assert one_x["leverage"] == 1.0
    assert four_x["leverage"] == 4.0
    assert four_x["trades"][-1]["qty"] == pytest.approx(one_x["trades"][-1]["qty"] * 4)


def test_trend_backtest_prioritizes_intrabar_stop_before_reversal_signal() -> None:
    config = TrendStrategyConfig(atr_stop_multiple=20.0)
    candles = _candles_with_breakout("long")
    entry_close = float(candles.iloc[-1]["close"])
    entry_probe = TrendStrategy(config).add_indicators(candles).iloc[-1]
    long_stop_probe = entry_close - float(entry_probe["atr"]) * config.atr_stop_multiple
    reversal_close = entry_close * 0.8
    candles = pd.concat(
        [
            candles,
            pd.DataFrame(
                [
                    {
                        "timestamp": len(candles),
                        "open": entry_close,
                        "high": entry_close * 1.01,
                        "low": min(reversal_close * 0.99, long_stop_probe * 0.99),
                        "close": reversal_close,
                        "volume": 6000,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    reversal_probe = TrendStrategy(config).add_indicators(candles).iloc[-1]
    short_stop_probe = float(reversal_probe["close"]) + float(reversal_probe["atr"]) * config.atr_stop_multiple
    candles = pd.concat(
        [
            candles,
            pd.DataFrame(
                [
                    {
                        "timestamp": len(candles),
                        "open": reversal_close,
                        "high": short_stop_probe * 1.01,
                        "low": reversal_close * 0.99,
                        "close": reversal_close * 0.995,
                        "volume": 1000,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )

    result = backtest_trend_strategy(candles, "ETH/USDT:USDT", "1h", config, leverage=4.0)

    assert (result["trades"][0]["side"], result["trades"][0]["exit_reason"]) == ("long", "atr_stop")


def test_trend_backtest_closes_open_position_at_end() -> None:
    candles = _candles_with_breakout("long")
    entry_open = float(candles.iloc[-1]["close"])
    candles = pd.concat(
        [
            candles,
            pd.DataFrame(
                [
                    {
                        "timestamp": len(candles),
                        "open": entry_open,
                        "high": entry_open * 1.01,
                        "low": entry_open * 0.995,
                        "close": entry_open * 1.002,
                        "volume": 1000,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )

    result = backtest_trend_strategy(candles, "ETH/USDT:USDT", "1h", TrendStrategyConfig(), leverage=4.0)

    assert result["trade_count"] == 1
    assert result["trades"][0]["side"] == "long"
    assert result["trades"][0]["exit_reason"] == "end_of_backtest"
    assert result["trade_ledger"][0]["exit_reason"] == "end_of_backtest"
    assert result["execution_model"] == "close_signal_next_open_fill_intrabar_stop"


def test_trend_backtest_fills_signal_on_next_bar_open() -> None:
    candles = _candles_with_breakout("long")
    signal_close = float(candles.iloc[-1]["close"])
    next_open = signal_close * 1.03
    candles = pd.concat(
        [
            candles,
            pd.DataFrame(
                [
                    {
                        "timestamp": len(candles),
                        "open": next_open,
                        "high": next_open * 1.01,
                        "low": next_open * 0.995,
                        "close": next_open * 1.002,
                        "volume": 1000,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )

    result = backtest_trend_strategy(candles, "ETH/USDT:USDT", "1h", TrendStrategyConfig(), fee_rate=0, slippage_bps=0)

    assert result["trade_count"] == 1
    assert result["trades"][0]["entry_price"] == pytest.approx(next_open)


def test_trend_backtest_charges_entry_fee_once() -> None:
    candles = _candles_with_breakout("long")
    signal_close = float(candles.iloc[-1]["close"])
    candles = pd.concat(
        [
            candles,
            pd.DataFrame(
                [
                    {
                        "timestamp": len(candles),
                        "open": signal_close,
                        "high": signal_close * 1.01,
                        "low": signal_close * 0.995,
                        "close": signal_close,
                        "volume": 1000,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )

    result = backtest_trend_strategy(
        candles,
        "ETH/USDT:USDT",
        "1h",
        TrendStrategyConfig(),
        initial_equity=1000,
        fee_rate=0.001,
        slippage_bps=0,
        leverage=1,
    )

    assert result["trade_count"] == 1
    assert result["trades"][0]["pnl"] == pytest.approx(-1.0)
    assert result["final_equity"] == pytest.approx(999.0)


def test_optimize_trend_parameters_returns_walk_forward_candidates() -> None:
    candles = pd.concat(
        [
            _candles_with_breakout("long"),
            _candles_with_breakout("short"),
            _candles_with_breakout("long"),
        ],
        ignore_index=True,
    )
    candles["timestamp"] = range(len(candles))

    result = optimize_trend_parameters(
        candles,
        "ETH/USDT:USDT",
        "1h",
        TrendStrategyConfig(),
        ema_lengths=[55, 89],
        kc_lengths=[18, 20],
        kc_scalars=[2.4, 2.8],
        atr_lengths=[14],
        vma_lengths=[20],
        volume_multiples=[1.2, 1.5],
        atr_stop_multiples=[2.0],
        position_fractions=[0.4, 0.5],
        use_ema_filters=[True],
        use_volume_filters=[True, False],
        max_candidates=8,
        min_trades=1,
    )

    assert result["best"]["params"]["kc_length"] == 20
    assert "use_volume_filter" in result["best"]["params"]
    assert result["candidates"]
    assert result["baseline"]["trade_count"] >= 0


def test_trend_strategy_can_disable_volume_filter() -> None:
    candles = _candles_with_breakout("long")
    candles.loc[candles.index[-1], "volume"] = 1
    strict = TrendStrategy(TrendStrategyConfig()).generate_signal(
        "ETH/USDT:USDT",
        "1h",
        candles,
        PositionSnapshot(symbol="ETH/USDT:USDT"),
        equity=1000,
        ai_multiplier=1.0,
    )
    no_volume = TrendStrategy(TrendStrategyConfig(variant="no_volume")).generate_signal(
        "ETH/USDT:USDT",
        "1h",
        candles,
        PositionSnapshot(symbol="ETH/USDT:USDT"),
        equity=1000,
        ai_multiplier=1.0,
    )

    assert strict.action == SignalAction.HOLD
    assert no_volume.action == SignalAction.LONG


def test_trend_strategy_kdj_filter_is_recorded() -> None:
    signal = TrendStrategy(TrendStrategyConfig(variant="no_volume", momentum_filter="kdj")).generate_signal(
        "ETH/USDT:USDT",
        "1h",
        _candles_with_breakout("long"),
        PositionSnapshot(symbol="ETH/USDT:USDT"),
        equity=1000,
        ai_multiplier=1.0,
    )

    assert signal.technical_evidence["momentum_filter"] == "kdj"
    assert "momentum_long_ok" in signal.technical_evidence


def test_trend_strategy_can_disable_symbol_profile() -> None:
    signal = TrendStrategy(TrendStrategyConfig(enabled=False, profile_name="btc_research_only")).generate_signal(
        "BTC/USDT:USDT",
        "1h",
        _candles_with_breakout("long"),
        PositionSnapshot(symbol="BTC/USDT:USDT"),
        equity=1000,
        ai_multiplier=1.0,
    )

    assert signal.action == SignalAction.HOLD
    assert signal.technical_evidence["reason"] == "strategy_profile_disabled"
    assert signal.technical_evidence["profile_name"] == "btc_research_only"


def test_keltner_channel_uses_ema20_middle_and_atr14_width() -> None:
    candles = _candles_with_breakout("long")
    channel = keltner_channel(candles, length=20, scalar=2.8, atr_length=14)
    middle = candles["close"].ewm(span=20, adjust=False, min_periods=20).mean()
    atr14 = atr(candles, 14)
    assert channel["KCMe_20_2.8"].iloc[-1] == middle.iloc[-1]
    assert channel["KCUe_20_2.8"].iloc[-1] == middle.iloc[-1] + 2.8 * atr14.iloc[-1]


def test_trend_strategy_requires_closed_bar_cross_above_keltner() -> None:
    candles = _candles_with_breakout("long")
    strategy = TrendStrategy(TrendStrategyConfig())
    decorated = strategy.add_indicators(candles)
    kcu = "KCUe_20_2.8"
    candles.loc[candles.index[-2], "close"] = float(decorated.iloc[-2][kcu]) * 1.01
    signal = strategy.generate_signal(
        "ETH/USDT:USDT",
        "1h",
        candles,
        PositionSnapshot(symbol="ETH/USDT:USDT"),
        equity=1000,
        ai_multiplier=1.0,
    )
    assert signal.action == SignalAction.HOLD


def test_trend_state_store_records_fixed_atr_stop(tmp_path) -> None:
    store = TrendStateStore(str(tmp_path / "state_trend.json"))
    state = store.record_entry("ETH/USDT:USDT", "long", entry_price=100.0, atr_value=2.0, atr_stop_multiple=3.0)
    assert state.stop_loss_price == 94.0
    assert store.get("ETH/USDT:USDT") == state
    store.clear("ETH/USDT:USDT")
    assert store.get("ETH/USDT:USDT") is None


def test_live_entry_state_uses_same_previous_atr_as_backtest(tmp_path) -> None:
    app = TradingApp.__new__(TradingApp)
    app.trend_state = TrendStateStore(str(tmp_path / "state_trend.json"))
    app.config = SimpleNamespace(strategy=SimpleNamespace(trend=TrendStrategyConfig(atr_stop_multiple=2.0)))
    signal = TrendStrategy(TrendStrategyConfig()).generate_signal(
        "ETH/USDT:USDT",
        "1h",
        _candles_with_breakout("long"),
        PositionSnapshot(symbol="ETH/USDT:USDT"),
        equity=1000,
        ai_multiplier=1.0,
    )
    signal.technical_evidence["entry_stop_atr"] = 3.0
    signal.technical_evidence["atr"] = 99.0

    app._record_trend_entry_state("ETH/USDT:USDT", signal, entry_price=100.0)

    state = app.trend_state.get("ETH/USDT:USDT")
    assert state is not None
    assert state.atr_value == 3.0
    assert state.stop_loss_price == 95.5
