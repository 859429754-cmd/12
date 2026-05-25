# ADR-0002: Model ATR Stop Multiple As Strategy Configuration

## Status

Accepted

## Context

The trend strategy used ATR length but did not model the ATR stop multiple as a first-class parameter. Some paths could therefore drift into hard-coded stop assumptions.

## Decision

Add `strategy.trend.atr_stop_multiple`, defaulting to `3.0`.

The parameter must flow through:

- `TrendStrategyConfig`
- `config/config.yaml`
- effective symbol parameter overrides
- strategy technical evidence
- DeepSeek fallback SL estimates
- trend backtest trade ledger

## Consequences

- ATR stop behavior is auditable and hot-updateable through the existing parameter whitelist.
- Backtests now record ATR-stop exits with stop price and max adverse excursion.
- This does not prove live profitability; it only removes parameter drift and improves risk realism.
