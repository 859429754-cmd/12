# PRD: AI Quant Trader Stabilization And Realism Upgrade

Status: ready-for-human
Labels: ready-for-human

## Problem

The system has a working FastAPI/React/SQLite/Gateway architecture, but several areas still determine whether it can survive live trading:

- Strategy definition must stay identical between live signal generation, backtest, AI proxy, and documentation.
- Backtests must model pessimistic intrabar behavior, fees, slippage, stops, and full trade ledger details.
- Live execution must fail closed on API errors, invalid AI output, stale data, and missing authorization.
- The console must not be exposed publicly without authentication.
- GitHub Issues must become the durable task tracker once GitHub auth is available.

## Non-goals

- Do not expose the console without account login/RBAC. ADR-0005 supersedes the older Trade PIN model.
- Do not expose the console publicly without auth.
- Do not let AI bypass hard local risk controls.
- Do not tune parameters only to improve in-sample backtest results.
- Do not call DeepSeek per candle for multi-year backtests.

## Current Strategy Contract

- Timeframe: 1h
- EMA89 has been removed from strategy code, optimizer inputs, chart layers, and AI evidence; reintroducing it requires a separate research path and ADR.
- Volume MA: SMA(volume, 20)
- Volume multiple: 2.5
- Momentum filter: KDJ(9, 3, 3)
- KC middle: EMA(close, 20)
- KC width: ATR14 * 2.8
- ATR fixed stop multiple: 1.5
- Leverage cap: 4x total equity

Entry:

- Long only when previous close <= previous KC upper and current closed candle close > current KC upper.
- Short only when previous close >= previous KC lower and current closed candle close < current KC lower.
- Current volume must exceed volume MA * 2.5.
- KDJ must confirm direction: long requires K > D and J >= 50; short requires K < D and J <= 50.

Exit:

- Long exits when previous close >= previous KC middle and current close < current KC middle.
- Short exits when previous close <= previous KC middle and current close > current KC middle.
- Fixed ATR stop is set at entry and persisted to `data/state_trend.json`; it is not trailing.

## Success Criteria

- Full test suite and frontend build pass after each implementation slice.
- Strategy contract is covered by regression tests.
- Backtest ledger reports entry/exit time, side, prices, PnL, fee, slippage, exit reason, stop price, and max adverse excursion.
- Live exits actually submit reduce-only/close orders.
- No secrets are printed, committed, or written to issue bodies.
