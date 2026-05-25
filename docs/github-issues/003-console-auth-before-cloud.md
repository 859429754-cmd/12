# Add Console Authentication Before Cloud Exposure

Status: ready-for-human
Labels: ready-for-human

## Problem

The web console can control runtime mode, strategy activation, manual tests, authorization, and close-all actions. It must not be exposed publicly without authentication.

## Options

Fast validation:

- Basic Auth or a single admin token at reverse proxy/app layer.
- Suitable for private testing only.

Production-grade:

- Cloudflare Access, Tailscale, or JWT-backed admin sessions.
- Audited operator identity on mode switch, authorization, strategy activation, and manual orders.

## Requirements

- No public unauthenticated console.
- Trade PIN remains mandatory for mock-to-live switch.
- Sensitive endpoints must log operator identity.
- Failed auth must not reveal config or runtime secrets.

## Acceptance Criteria

- Unauthenticated requests to control endpoints are rejected.
- Local development can still run with explicit dev auth configuration.
- Tests cover authenticated and unauthenticated API calls.
- Frontend shows auth failure clearly, not infinite loading.

