# Implement FMZ-Style Pessimistic Intrabar Backtest Ledger

Status: ready-for-agent
Labels: ready-for-agent

## Problem

The backtest is closer to reality than before, but still needs a formal intrabar path model to avoid optimistic fills and same-candle TP/SL ambiguity.

## Required Behavior

- Bullish candle path: Open -> Low -> High -> Close.
- Bearish candle path: Open -> High -> Low -> Close.
- If TP and SL are both touched in the same candle, assume SL is hit first.
- Fixed ATR stop must use the ATR value captured at entry.
- Stop price must not move after entry.
- Trade ledger must include:
  - entry time
  - side
  - entry price
  - exit time
  - exit price
  - PnL
  - max adverse excursion
  - fee
  - slippage
  - exit reason
  - stop loss price

## Acceptance Criteria

- Unit tests cover long and short same-candle stop behavior.
- A fixture proves same-candle favorable and unfavorable touches choose the unfavorable path.
- `python -m pytest tests/test_backtest_costs.py tests/test_strategy_trend.py -q` passes.
- Full suite passes.

