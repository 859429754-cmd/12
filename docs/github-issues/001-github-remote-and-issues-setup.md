# Set Up GitHub Remote And Issue Publishing

Status: ready-for-human
Labels: ready-for-human

## Context

The repo has been initialized locally and `setup-matt-pocock-skills` has been switched to GitHub Issues. Publishing is blocked until GitHub authentication and repo remote are configured.

Current known state:

- Local git repo exists.
- Branch is `main`.
- GitHub CLI is installed at `C:\Program Files\GitHub CLI\gh.exe`.
- `gh auth status` has not passed yet.
- No `origin` remote is configured.

## Tasks

- Log in with GitHub CLI.
- Create or choose the target GitHub repo.
- Add `origin`.
- Push `main`.
- Verify labels exist:
  - `needs-triage`
  - `needs-info`
  - `ready-for-agent`
  - `ready-for-human`
  - `wontfix`
- Publish the draft issues under `docs/github-issues/`.

## Acceptance Criteria

- `git remote -v` shows the intended GitHub repo.
- `gh auth status` passes.
- `gh issue list` works.
- No ignored secret/runtime files appear in `git status --short`.

