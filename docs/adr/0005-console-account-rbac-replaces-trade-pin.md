# ADR-0005: Console Account RBAC Replaces Trade PIN

## Status

Accepted

## Context

The console is moving from a single-operator dashboard to account-scoped views:

- `admin`: can manage runtime mode, exchange account secrets, strategy parameters, approvals and dangerous execution controls.
- `account1`: can view the trend strategy account and change only its account-level leverage cap.
- `account2`: can view the follower account and change only its account-level leverage cap.
- `range`: reserved range-strategy account view; can change only its account-level leverage cap after the range module is implemented.

The old live-mode safety model depended on `TRADE_PIN` and a separate operation-code prompt. That protected accidental clicks, but it did not express account identity or per-account permissions.

## Decision

Replace console-side `TRADE_PIN` and operation-code checks with authenticated console sessions plus role-based access control.

Runtime switching, API key updates, strategy parameter proposals, proposal approvals and dangerous execution controls are admin-only operations.

Console authentication is fail-closed by default. Unless `CONSOLE_AUTH_DISABLED=1` is explicitly set for local development, protected API routes require a configured console user. If no console user is configured, protected routes return `console_auth_not_configured`; the service must not silently fall back to a local admin identity.

Non-admin accounts are view-only except for their own account-level leverage cap. They cannot:

- change strategy parameters,
- switch mock/live runtime mode,
- update API secrets,
- authorize symbols,
- pause or resume opening,
- manually close positions,
- approve proposals.

## Supersedes

This ADR supersedes ADR-0001 for console runtime control. Future work should treat ADR-0005 as authoritative and ignore older plans that require `TRADE_PIN` or a separate operation code for console operations.

## Consequences

- Operator identity is explicit in the console session.
- Account-scoped views can share one strategy signal and one AI decision while keeping account balances, leverage caps, positions and order status separate.
- Public exposure still requires strong passwords, HTTPS, and ideally network-level access control. RBAC is necessary, not sufficient, for large unattended capital.
- `CONSOLE_AUTH_DISABLED=1` is a local-development escape hatch only and must not be used on a public or live-trading cloud deployment.
- `.env.runtime` secrets remain server-side only and must never be printed, logged, committed or shown in the browser.
