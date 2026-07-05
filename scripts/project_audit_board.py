from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
CONSOLE_DIR = ROOT / "console"
NPM = "npm.cmd" if os.name == "nt" else "npm"
TAIL_CHARS = 6000


SECRET_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9]{12,}"), "sk-[REDACTED]"),
    (re.compile(r"(?i)(api[_-]?secret|secret[_-]?key|private[_-]?key|token|password)\s*[:=]\s*['\"]?[^'\"\s]{8,}"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)(gateio[_-]?api[_-]?key)\s*[:=]\s*['\"]?[^'\"\s]{8,}"), r"\1=[REDACTED]"),
)


@dataclass(frozen=True)
class AuditGroup:
    id: str
    title: str
    command: tuple[str, ...]
    cwd: Path
    modes: tuple[str, ...]
    requirement: str
    optional_env: tuple[str, ...] = ()
    timeout_seconds: int = 600


@dataclass(frozen=True)
class AuditGroupResult:
    id: str
    title: str
    requirement: str
    status: str
    exit_code: int | None
    duration_seconds: float
    command: list[str]
    cwd: str
    stdout_tail: str
    stderr_tail: str
    skipped_reason: str | None = None


def audit_groups() -> tuple[AuditGroup, ...]:
    return (
        AuditGroup(
            id="public_repo_preflight",
            title="公开仓库防泄露检查",
            command=(sys.executable, "scripts/public_repo_preflight.py"),
            cwd=ROOT,
            modes=("core", "full"),
            requirement="Public repo candidate files must exclude runtime data and high-confidence secret literals.",
            timeout_seconds=120,
        ),
        AuditGroup(
            id="compileall",
            title="Python 编译检查",
            command=(sys.executable, "-m", "compileall", "ai_quant_trader", "tests", "scripts"),
            cwd=ROOT,
            modes=("core", "full"),
            requirement="All Python modules must compile before trading/runtime validation.",
            timeout_seconds=180,
        ),
        AuditGroup(
            id="trading_chain_extended",
            title="交易链路 extended 审计",
            command=(sys.executable, "scripts/trading_chain_audit.py", "--mode", "extended"),
            cwd=ROOT,
            modes=("core", "full"),
            requirement="Fake strategy signals must traverse AI/RiskManager/order/native-stop/follower/reconciliation fail-closed paths.",
            timeout_seconds=420,
        ),
        AuditGroup(
            id="frontend_build",
            title="控制台前端构建",
            command=(NPM, "run", "build"),
            cwd=CONSOLE_DIR,
            modes=("core", "full"),
            requirement="Console TypeScript and production build must pass.",
            timeout_seconds=300,
        ),
        AuditGroup(
            id="frontend_e2e",
            title="控制台本地 E2E",
            command=(NPM, "run", "test:e2e"),
            cwd=CONSOLE_DIR,
            modes=("core", "full"),
            requirement="Core console interactions must pass in Playwright without calling real control APIs.",
            timeout_seconds=420,
        ),
        AuditGroup(
            id="backend_full_pytest",
            title="后端全量 pytest",
            command=(sys.executable, "-m", "pytest", "-q"),
            cwd=ROOT,
            modes=("full",),
            requirement="Full backend regression suite must pass.",
            timeout_seconds=900,
        ),
        AuditGroup(
            id="cloud_readonly_e2e",
            title="真实云端只读 E2E",
            command=(NPM, "run", "test:e2e:cloud"),
            cwd=CONSOLE_DIR,
            modes=("full",),
            requirement="Cloud account1/account2/admin readonly console smoke must pass when credentials are provided.",
            optional_env=(
                "CONSOLE_URL",
                "AIQUANT_E2E_ACCOUNT1_PASSWORD",
                "AIQUANT_E2E_ACCOUNT2_PASSWORD",
                "AIQUANT_E2E_ADMIN_PASSWORD",
            ),
            timeout_seconds=480,
        ),
    )


def selected_groups(mode: str, *, skip_cloud: bool = False) -> tuple[AuditGroup, ...]:
    groups = tuple(group for group in audit_groups() if mode in group.modes)
    if skip_cloud:
        groups = tuple(group for group in groups if group.id != "cloud_readonly_e2e")
    return groups


def _redact(text: str | bytes | None) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    cleaned = text[-TAIL_CHARS:]
    for pattern, replacement in SECRET_REDACTIONS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def _missing_env(names: Sequence[str]) -> list[str]:
    return [name for name in names if not os.environ.get(name)]


def run_group(group: AuditGroup) -> AuditGroupResult:
    started_at = time.time()
    missing = _missing_env(group.optional_env)
    if missing:
        return AuditGroupResult(
            id=group.id,
            title=group.title,
            requirement=group.requirement,
            status="skipped",
            exit_code=None,
            duration_seconds=0.0,
            command=list(group.command),
            cwd=str(group.cwd),
            stdout_tail="",
            stderr_tail="",
            skipped_reason=f"missing optional env: {', '.join(missing)}",
        )

    try:
        result = subprocess.run(
            list(group.command),
            cwd=group.cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=group.timeout_seconds,
        )
        status = "passed" if result.returncode == 0 else "failed"
        return AuditGroupResult(
            id=group.id,
            title=group.title,
            requirement=group.requirement,
            status=status,
            exit_code=result.returncode,
            duration_seconds=round(time.time() - started_at, 3),
            command=list(group.command),
            cwd=str(group.cwd),
            stdout_tail=_redact(result.stdout),
            stderr_tail=_redact(result.stderr),
        )
    except subprocess.TimeoutExpired as exc:
        return AuditGroupResult(
            id=group.id,
            title=group.title,
            requirement=group.requirement,
            status="failed",
            exit_code=None,
            duration_seconds=round(time.time() - started_at, 3),
            command=list(group.command),
            cwd=str(group.cwd),
            stdout_tail=_redact(exc.stdout or ""),
            stderr_tail=_redact((exc.stderr or "") + f"\nTIMEOUT after {group.timeout_seconds}s"),
        )


def build_report(mode: str, results: Sequence[AuditGroupResult], started_at: float) -> dict[str, object]:
    failed = [item for item in results if item.status == "failed"]
    passed = [item for item in results if item.status == "passed"]
    skipped = [item for item in results if item.status == "skipped"]
    return {
        "ok": not failed,
        "mode": mode,
        "group_count": len(results),
        "passed_count": len(passed),
        "failed_count": len(failed),
        "skipped_count": len(skipped),
        "duration_seconds": round(time.time() - started_at, 3),
        "groups": [asdict(item) for item in results],
    }


def print_report(text: str) -> None:
    encoding = sys.stdout.encoding or "utf-8"
    safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(safe_text)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the AI quant project audit board without reading runtime secrets or submitting real orders.",
    )
    parser.add_argument("--mode", choices=["core", "full"], default="core")
    parser.add_argument(
        "--skip-cloud",
        action="store_true",
        help="Do not run cloud readonly E2E even when cloud credentials are configured.",
    )
    parser.add_argument("--json-out", type=Path, default=None, help="Optional path to write the JSON audit report.")
    args = parser.parse_args(argv)

    started_at = time.time()
    results = [run_group(group) for group in selected_groups(args.mode, skip_cloud=args.skip_cloud)]
    report = build_report(args.mode, results, started_at)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print_report(text)
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
