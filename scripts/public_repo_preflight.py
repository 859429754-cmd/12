from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


SECRET_PATTERNS = {
    "private_key_marker": re.compile(r"BEGIN (RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY"),
    "github_token": re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    "non_empty_secret_assignment": re.compile(
        r"(?i)\b(API_KEY|API_SECRET|SECRET_KEY|PRIVATE_KEY|TOKEN|TRADE_PIN|PASSWORD)\b\s*[:=]\s*['\"]([A-Za-z0-9_./+=:-]{16,})['\"]"
    ),
}

TEXT_SUFFIXES = {
    "",
    ".css",
    ".html",
    ".js",
    ".json",
    ".lock",
    ".md",
    ".ps1",
    ".py",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    candidate_count: int
    findings: list[Finding]
    ignored_runtime_paths: dict[str, bool]
    tracked_source_paths: dict[str, bool]

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["findings"] = [asdict(item) for item in self.findings]
        return payload


def git_files(repo: Path) -> list[str]:
    raw = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repo,
    )
    return [item.decode("utf-8", errors="replace") for item in raw.split(b"\0") if item]


def git_is_ignored(repo: Path, path: str) -> bool:
    result = subprocess.run(["git", "check-ignore", "-q", path], cwd=repo, check=False)
    return result.returncode == 0


def is_text_candidate(path: str) -> bool:
    return Path(path).suffix.lower() in TEXT_SUFFIXES


def scan_file(repo: Path, path: str) -> list[Finding]:
    if not is_text_candidate(path):
        return []
    full_path = repo / path
    if not full_path.exists() or full_path.is_dir():
        return []
    findings: list[Finding] = []
    try:
        lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return findings
    for idx, line in enumerate(lines, start=1):
        for rule, pattern in SECRET_PATTERNS.items():
            match = pattern.search(line)
            if not match:
                continue
            if rule == "non_empty_secret_assignment" and _looks_like_placeholder(match.group(2)):
                continue
            findings.append(Finding(path=path, line=idx, rule=rule))
    return findings


def _looks_like_placeholder(value: str) -> bool:
    normalized = value.strip().strip("'\"").lower()
    return normalized in {"changeme", "example", "placeholder", "your_key_here", "your_secret_here"} or set(normalized) <= {"x", "*"}


def run_preflight(repo: Path, runtime_paths: Iterable[str] | None = None) -> PreflightResult:
    runtime_paths = runtime_paths or [".env.runtime", "data/trader.sqlite3", "logs/audit.jsonl", "output/playwright/example.png"]
    candidates = git_files(repo)
    findings = [finding for path in candidates for finding in scan_file(repo, path)]
    ignored_runtime_paths = {path: git_is_ignored(repo, path) for path in runtime_paths}
    tracked_source_paths = {"ai_quant_trader/data/news.py": (repo / "ai_quant_trader/data/news.py").exists() and not git_is_ignored(repo, "ai_quant_trader/data/news.py")}
    ok = not findings and all(ignored_runtime_paths.values()) and all(tracked_source_paths.values())
    return PreflightResult(
        ok=ok,
        candidate_count=len(candidates),
        findings=findings,
        ignored_runtime_paths=ignored_runtime_paths,
        tracked_source_paths=tracked_source_paths,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Check whether the repository is safe to publish to a public GitHub repo.")
    parser.add_argument("--repo", default=".", help="Repository root.")
    args = parser.parse_args()
    result = run_preflight(Path(args.repo).resolve())
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    raise SystemExit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
