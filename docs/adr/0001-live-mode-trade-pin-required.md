# ADR-0001: Keep Trade PIN Required For Live Mode Switches

## Status

Accepted

## Context

The console can switch the runtime gateway from mock execution to live Gate.io execution. Removing the password/PIN confirmation would make accidental clicks, browser compromise, unauthenticated local access, or future public exposure capable of enabling real trading.

This conflicts with the project's safety hierarchy:

1. Cold start defaults to paused opening.
2. Per-symbol authorization is required.
3. Live mode switch requires Trade PIN.
4. AI cannot bypass hard risk controls.

## Decision

Keep backend `TRADE_PIN` validation mandatory for any mock-to-live transition.

The frontend may improve UX, but it must not remove the live-mode secret challenge unless another stronger control is already in place, such as authenticated console sessions plus role-based approval and audited operator identity.

## Consequences

- Mock-to-live remains a deliberate, auditable operator action.
- Live switching fails closed when `TRADE_PIN` is missing.
- Removing the modal alone is not an acceptable optimization because it would either break live switching or pressure the backend to remove the real safety control.
