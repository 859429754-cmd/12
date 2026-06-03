# ADR-0001: Keep Trade PIN Required For Live Mode Switches

## Status

Superseded by ADR-0005 as of 2026-06-04.

This ADR is retained as historical context. The current production rule is console account login with RBAC and audited operator identity. See `docs/adr/0005-console-account-rbac-replaces-trade-pin.md`.

## Context

The console can switch the runtime gateway from mock execution to live Gate.io execution. Removing the password/PIN confirmation would make accidental clicks, browser compromise, unauthenticated local access, or future public exposure capable of enabling real trading.

This conflicts with the project's safety hierarchy:

1. Cold start defaults to paused opening.
2. Per-symbol authorization is required.
3. Live mode switch requires Trade PIN.
4. AI cannot bypass hard risk controls.

## Decision

Historical decision: keep backend `TRADE_PIN` validation mandatory for any mock-to-live transition.

Current decision: ADR-0005 replaces this with authenticated console sessions plus role-based approval and audited operator identity.

## Consequences

- Mock-to-live must remain a deliberate, auditable operator action.
- Missing or unauthenticated console sessions must fail closed.
- Removing Trade PIN is acceptable only because ADR-0005 introduced a stronger authenticated RBAC control.
