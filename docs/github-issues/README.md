# GitHub Issue Drafts

These files are GitHub Issues drafts for this repo.

GitHub publishing is currently blocked because this workspace has no authenticated GitHub session. Once `gh auth status` passes and `origin` points to the intended GitHub repo, publish these drafts with:

```powershell
gh issue create --title "<title>" --body-file "docs/github-issues/<file>.md" --label "ready-for-agent"
```

Safety rules:

- Do not include `.env.runtime` contents.
- Do not include API keys, webhook tokens, SSH private key contents, or unredacted credentials.
- For live trading, exchange permission, cloud deployment, or credential work, start with `ready-for-human`.

