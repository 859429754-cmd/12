# Issue Tracker: GitHub Issues

Issues and PRDs for this repo are tracked in GitHub Issues.

## Current prerequisite status

At the time this file was switched:

- The local workspace was not yet a git repository.
- No `origin` GitHub remote was configured.
- `gh` CLI was not available on PATH.

That means skills should treat GitHub Issues as the intended tracker, but must verify a GitHub repo and authenticated issue-writing capability before publishing.

## Conventions

- Use one GitHub issue per independently shippable task.
- Use a PRD issue for larger features, then implementation issues linked from that PRD.
- Apply the triage labels from `docs/agents/triage-labels.md`.
- Never include secrets, API keys, webhook tokens, private key contents, `.env.runtime` contents, or full server credentials in issue bodies.
- Use redacted identifiers and local path references instead.

## When a skill says "publish to the issue tracker"

Create a GitHub issue in the configured repo.

Preferred command shape when `gh` is available:

```powershell
gh issue create --title "<title>" --body-file "<body-file>" --label "<label>"
```

## When a skill says "fetch the relevant ticket"

Read the referenced GitHub issue by URL or issue number.

Preferred command shape when `gh` is available:

```powershell
gh issue view <number> --json title,body,labels,state,comments
```

## Required setup before first publish

Before creating issues, verify:

1. This workspace is a git repo.
2. `git remote -v` points at the intended GitHub repo.
3. `gh auth status` passes, or a GitHub plugin/connector with issue-write capability is available.
4. The repo has the labels listed in `docs/agents/triage-labels.md`, or the user approves creating them.
