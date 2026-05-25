# QuantDinger Migration Assessment

## Decision

Do not replace the trading core with QuantDinger.

Use the QuantDinger-style platform shell for the control terminal, task orchestration, authentication, persisted backtest history, agent gateway, and deployment shape.

The local system's core assets stay in place:

- ETH strategy signal engine
- backtest/live execution consistency rules
- DeepSeek five-level overlay
- news and orderflow inputs
- Gate.io gateway isolation
- hard risk manager

This is now recorded in ADR-0003.

## Why Not A Direct Replacement

QuantDinger is a full Flask/Vue/PostgreSQL/Redis/Docker platform. The public repository ships the backend, Compose stack, docs, and references to a prebuilt frontend image. Its frontend source is maintained in a separate Vue repository.

Direct migration would force a backend framework switch, database migration, strategy runtime rewrite, and execution path rewrite at the same time. That is high risk for a live-trading system.

## Reusable Ideas

1. Agent Gateway

   Add an audited agent/API seam for automation and AI clients. Calls should be scoped, rate-limited, idempotent, and paper-only unless explicitly enabled.

2. Strategy Workspace

   Replace the current cramped Strategy Lab with a real workspace:

   - strategy profiles per symbol
   - saved backtest runs
   - parameter search history
   - walk-forward validation
   - promotion from research to paper to live

3. Persisted Backtest History

   Move from transient UI results to stored runs with:

   - strategy version
   - data source and data hash
   - fee/slippage model
   - trade ledger
   - equity curve
   - yearly/market-regime breakdown

4. Professional Terminal Layout

   Rebuild the console around persistent work areas:

   - market/chart workspace
   - strategy/backtest workspace
   - AI decision audit workspace
   - execution/risk workspace
   - data/news health workspace

5. Deployment Shape

   Adopt a clearer production stack:

   - API worker
   - news worker
   - trading worker
   - price monitor worker
   - database
   - reverse proxy and authentication

## Migration Plan

### Phase 1: Stabilize Current Core

- Keep FastAPI and current trading modules.
- Disable DingTalk by default.
- Keep ETH strategy enabled; BTC/SOL remain research-only until separately optimized.
- Add authenticated console access before any public exposure.

### Phase 2: Platformize The Existing Backend

- Add persisted backtest run tables.
- Add strategy profile records per symbol.
- Add an audited internal agent API.
- Add task/job records for long backtests and DeepSeek evaluations.

### Phase 3: Rebuild The Console

- Replace the single large React screen with a workspace shell.
- Keep using our FastAPI endpoints.
- Reuse QuantDinger's information architecture, not its private frontend source.

### Phase 4: Optional Postgres Migration

- Migrate SQLite WAL to Postgres only after the data model is stable.
- Keep the Gateway and RiskManager interfaces unchanged during migration.

## Non-negotiable Boundaries

- No UI migration may change live order logic.
- No platform code may bypass `RiskManager`.
- No agent or AI path may place live orders without explicit live enablement and local risk approval.
- Backtest and live strategy signal logic must share the same strategy interface.
- Secrets stay in `.env.runtime` or environment variables and must never be logged.
