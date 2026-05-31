from __future__ import annotations

import pandas as pd

from ai_quant_trader.core.models import TrendStrategyConfig
from ai_quant_trader.strategy.lab import (
    BacktestCostModel,
    _is_negative_ai_overlay,
    backtest_trend_strategy,
    intrabar_path_labels,
    pessimistic_intrabar_exit,
    timeframe_hours,
)
from tests.test_strategy_trend import _candles_with_breakout


def test_cost_model_allows_zero_cost_tv_alignment_mode() -> None:
    model = BacktestCostModel.from_inputs(fee_rate=0.0, slippage_bps=0.0)
    assert model.taker_fee_rate == 0.0
    assert model.base_slippage_bps == 0.0
    assert model.slippage(100.0, pd.Series({"high": 110.0, "low": 90.0, "close": 100.0})) == (0.0, 0.0)


def test_backtest_reports_zero_costs_when_user_passes_zero() -> None:
    prices = [100.0] * 120 + [106, 112, 119, 127, 136, 146, 157, 169, 182, 196] + [188, 176, 164, 152, 140]
    candles = pd.DataFrame(
        {
            "open": prices,
            "high": [p * 1.015 for p in prices],
            "low": [p * 0.985 for p in prices],
            "close": prices,
            "volume": [100.0] * 120 + [300.0] * 10 + [280.0] * 5,
        }
    )
    result = backtest_trend_strategy(
        candles,
        symbol="ETH/USDT:USDT",
        timeframe="1h",
        config=TrendStrategyConfig(ema_length=20, kc_length=10, vma_length=10, atr_length=10, volume_multiple=1.2),
        fee_rate=0.0,
        slippage_bps=0.0,
    )
    assert result["cost_model"]["enabled"] is True
    assert result["cost_model"]["taker_fee_rate"] == 0.0
    assert result["cost_model"]["base_slippage_bps"] == 0.0
    assert "毒打回测" in result["note"]
def test_intrabar_path_uses_fmz_bullish_and_bearish_order() -> None:
    bullish = pd.Series({"open": 100.0, "high": 110.0, "low": 90.0, "close": 105.0})
    bearish = pd.Series({"open": 100.0, "high": 110.0, "low": 90.0, "close": 95.0})
    assert intrabar_path_labels(bullish) == ["open", "low", "high", "close"]
    assert intrabar_path_labels(bearish) == ["open", "high", "low", "close"]


def test_same_candle_long_take_profit_and_stop_chooses_stop() -> None:
    row = pd.Series({"open": 100.0, "high": 110.0, "low": 90.0, "close": 105.0})
    exit_event = pessimistic_intrabar_exit("long", row, stop_loss_price=95.0, take_profit_price=108.0)
    assert exit_event is not None
    assert exit_event.reason == "atr_stop"
    assert exit_event.price == 95.0


def test_same_candle_short_take_profit_and_stop_chooses_stop() -> None:
    row = pd.Series({"open": 100.0, "high": 110.0, "low": 90.0, "close": 95.0})
    exit_event = pessimistic_intrabar_exit("short", row, stop_loss_price=105.0, take_profit_price=92.0)
    assert exit_event is not None
    assert exit_event.reason == "atr_stop"
    assert exit_event.price == 105.0


def test_backtest_skips_entries_below_min_order_qty() -> None:
    candles = _cost_breakout_candles("long")

    result = backtest_trend_strategy(
        candles,
        symbol="ETH/USDT:USDT",
        timeframe="1h",
        config=TrendStrategyConfig(),
        initial_equity=100.0,
        leverage=1.0,
        fee_rate=0.0,
        slippage_bps=0.0,
        min_order_qty=10.0,
    )

    assert result["trade_count"] == 0
    assert result["skipped_orders"][0]["reason"] == "below_min_order_qty"


def test_backtest_partial_fill_caps_qty_by_bar_volume() -> None:
    candles = _cost_breakout_candles("long", next_volume=1.0)

    result = backtest_trend_strategy(
        candles,
        symbol="ETH/USDT:USDT",
        timeframe="1h",
        config=TrendStrategyConfig(),
        initial_equity=10_000.0,
        leverage=4.0,
        fee_rate=0.0,
        slippage_bps=0.0,
        max_volume_participation=0.25,
    )

    assert result["trade_count"] == 1
    trade = result["trade_ledger"][0]
    assert trade["requested_qty"] > trade["qty"]
    assert trade["filled_qty"] == trade["qty"] == 0.25
    assert 0 < trade["fill_ratio"] < 1


def test_backtest_charges_pessimistic_funding_cost() -> None:
    candles = _cost_breakout_candles("long")

    result = backtest_trend_strategy(
        candles,
        symbol="ETH/USDT:USDT",
        timeframe="1h",
        config=TrendStrategyConfig(),
        initial_equity=10_000.0,
        leverage=1.0,
        fee_rate=0.0,
        slippage_bps=0.0,
        funding_rate_per_8h=0.0008,
    )

    trade = result["trade_ledger"][0]
    assert trade["funding_paid"] > 0
    assert result["cost_model"]["total_funding_paid"] == trade["funding_paid"]
    assert result["cost_model"]["total_cost_paid"] == trade["funding_paid"]


def test_ai_overlay_guard_rejects_score_regression_even_when_return_improves() -> None:
    baseline = {
        "total_return_pct": -1.08,
        "max_drawdown_pct": -1.81,
        "profit_factor": 0.46,
    }
    result = {
        "total_return_pct": -0.79,
        "max_drawdown_pct": -0.79,
        "profit_factor": 0.0,
    }

    assert _is_negative_ai_overlay(result, baseline) is True


def test_timeframe_hours_distinguishes_month_from_minute() -> None:
    assert timeframe_hours("15m") == 0.25
    assert timeframe_hours("1M") == 24.0 * 30


def _cost_breakout_candles(direction: str, next_volume: float = 1000.0) -> pd.DataFrame:
    candles = _candles_with_breakout(direction)
    next_open = float(candles.iloc[-1]["close"])
    next_row = pd.DataFrame(
        [
            {
                "timestamp": len(candles),
                "open": next_open,
                "high": next_open * 1.01,
                "low": next_open * 0.995,
                "close": next_open * 1.002,
                "volume": next_volume,
            }
        ]
    )
    return pd.concat([candles, next_row], ignore_index=True)
