# ADR-0003: Adopt QuantDinger-Style Platform Shell While Keeping Local Trading Core

## Status

Accepted

## Context

The current Web console is fragile and difficult to extend. The core value of this system is not the current UI shell; it is the trading core:

- strategy signal logic
- backtest/live consistency model
- DeepSeek five-level sizing overlay
- news/orderflow/dense-zone inputs
- Gate.io Gateway isolation
- local hard risk manager

QuantDinger provides a useful reference architecture for a quant operating system: authenticated workspaces, strategy/backtest areas, persisted history, agent gateway, background workers, deployment composition, and multi-user operational controls.

Directly replacing the current trading core with QuantDinger would create unacceptable live-trading risk because it would change framework, persistence, strategy runtime, execution path, and operational controls at the same time.

## Decision

Adopt the QuantDinger-style platform shell, not its trading core.

The target architecture is:

- Platform shell: QuantDinger-style workspaces, authentication, task orchestration, persisted backtest history, agent/API access, deployment layout.
- Trading core: this repository's strategy, DeepSeek, news/orderflow, RiskManager, Gateway, and audit logic.

The platform shell must call the local trading core through explicit Python/FastAPI interfaces. It must not duplicate or reimplement order sizing, live execution, or risk approval logic.

## Required Seams

The migration must introduce or preserve these seams:

- Strategy profile seam: per-symbol strategy configuration and research/live status.
- Backtest run seam: immutable persisted run record with data source, params, costs, ledger, and equity curve.
- AI decision seam: structured DeepSeek request/response records, five-level sizing result, and audit metadata.
- Execution command seam: all live orders pass through Gateway and RiskManager.
- Platform agent seam: authenticated automation API; paper-only by default.

## Non-Negotiable Constraints

- No platform UI or agent route may bypass `RiskManager`.
- No platform module may call exchange execution directly.
- Live mode still requires the existing live-mode protection from ADR-0001 or a stronger authenticated replacement.
- Backtest and live strategy signal logic must share the same strategy interface.
- Agent/API tokens default to paper-only.
- Secrets remain in `.env.runtime` or environment variables and must not be logged.
- DingTalk remains disabled by default.

## Consequences

- UI and platform work can move faster without destabilizing live execution.
- The codebase gains stronger locality: platform concerns move into workspaces and persisted records; execution and risk remain concentrated in the trading core.
- Migration can happen in phases instead of a single rewrite.
- Some QuantDinger features will be copied as product ideas, not source code, especially where the frontend source is separate or where implementation conflicts with the local risk model.

## Migration Order

1. Add persisted strategy profiles and backtest run history.
2. Rebuild console as a QuantDinger-style workspace shell on top of current FastAPI endpoints.
3. Add authenticated platform access before any external exposure.
4. Add audited agent/API access, paper-only by default.
5. Consider PostgreSQL only after the data model is stable.
