# Domain Docs

How engineering skills should consume this repo's domain documentation.

## Layout

This is a single-context repo.

Read these before architecture, debugging, TDD, triage, PRD, or issue-generation work:

- `CONTEXT.md` at the repo root
- Relevant ADRs under `docs/adr/`, if any exist
- Relevant source and tests for the module being changed

If ADRs do not exist yet, proceed silently. Create ADRs only when a design decision needs durable documentation.

## Domain vocabulary

Use the project's existing terms:

- Gateway separation
- MockExchangeGateway
- GateRealGateway
- Trade PIN
- cold-start lock
- per-symbol authorization
- total leverage hard cap
- AI veto
- AI candidate approval
- FMZ pessimistic intrabar path model
- Trade Ledger
- dense zone
- orderflow alignment
- news timeline

Do not rename these concepts casually. If a new term is needed, define it in `CONTEXT.md` or an ADR.

## Conflict handling

If a proposed change conflicts with `CONTEXT.md` or an ADR, surface the conflict before editing. For trading safety conflicts, stop and ask for explicit approval.
