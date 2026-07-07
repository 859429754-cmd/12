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
            id="ai_brain_contract",
            title="AI 大脑契约审计",
            command=(sys.executable, "scripts/ai_brain_contract_audit.py", "--mode", "extended"),
            cwd=ROOT,
            modes=("core", "full"),
            requirement="DeepSeek output normalization, budget guards, credential failover, news alignment, and five-tier sizing must remain deterministic and fail closed.",
            timeout_seconds=420,
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
        AuditGroup(
            id="cloud_runtime_audit",
            title="真实云端运行审计",
            command=(sys.executable, "scripts/cloud_runtime_audit.py"),
            cwd=ROOT,
            modes=(),
            requirement="Cloud current release, last-successful marker, systemd services, readiness, release_runs, and recent logs must be consistent.",
            timeout_seconds=180,
        ),
    )


def selected_groups(mode: str, *, skip_cloud: bool = False, include_cloud_runtime: bool = False) -> tuple[AuditGroup, ...]:
    groups = tuple(group for group in audit_groups() if mode in group.modes)
    if include_cloud_runtime and not skip_cloud:
        cloud_runtime = next(group for group in audit_groups() if group.id == "cloud_runtime_audit")
        groups = (*groups, cloud_runtime)
    if skip_cloud:
        groups = tuple(group for group in groups if group.id not in {"cloud_readonly_e2e", "cloud_runtime_audit"})
    return groups


def _git_short_head() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _with_cloud_runtime_expectations(
    groups: Sequence[AuditGroup],
    expected_release: str | None,
    *,
    expect_live_ready: bool = False,
    cloud_host: str | None = None,
    cloud_runtime_env_mode: str = "cloud-live",
    cloud_allow_weak_passwords: bool = False,
    cloud_expect_peak_pricing_guard: bool = False,
    cloud_log_minutes: int | None = None,
) -> tuple[AuditGroup, ...]:
    if (
        not expected_release
        and not expect_live_ready
        and not cloud_host
        and cloud_runtime_env_mode == "cloud-live"
        and not cloud_allow_weak_passwords
        and not cloud_expect_peak_pricing_guard
        and cloud_log_minutes is None
    ):
        return tuple(groups)
    updated: list[AuditGroup] = []
    for group in groups:
        if group.id != "cloud_runtime_audit":
            updated.append(group)
            continue
        command = group.command
        requirement = group.requirement
        if cloud_host:
            command = (*command, "--host", cloud_host)
            requirement = f"{requirement} Cloud host must be {cloud_host}."
        if expected_release:
            command = (*command, "--expected-release", expected_release)
            requirement = f"{requirement} Expected release must match {expected_release}."
        if expect_live_ready:
            command = (*command, "--expect-live-ready")
            command = (*command, "--runtime-env-mode", cloud_runtime_env_mode)
            requirement = (
                f"{requirement} Cloud readiness must report live mode with at least one authorized live-ready profile "
                f"and runtime env mode {cloud_runtime_env_mode}."
            )
        if cloud_allow_weak_passwords:
            command = (*command, "--allow-weak-passwords")
            requirement = (
                f"{requirement} Weak console passwords are explicitly allowed for small-funds gray testing; "
                "this is not a large-funds unattended acceptance."
            )
        if cloud_expect_peak_pricing_guard:
            command = (*command, "--expect-peak-pricing-guard")
            requirement = (
                f"{requirement} Remote config must enable DeepSeek peak-pricing avoidance for noncritical calls "
                "without blocking trading_cycle."
            )
        if cloud_log_minutes is not None:
            command = (*command, "--log-minutes", str(cloud_log_minutes))
            requirement = f"{requirement} Recent service log window is {cloud_log_minutes} minute(s)."
        updated.append(
            AuditGroup(
                id=group.id,
                title=group.title,
                command=command,
                cwd=group.cwd,
                modes=group.modes,
                requirement=requirement,
                optional_env=group.optional_env,
                timeout_seconds=group.timeout_seconds,
            )
        )
    return tuple(updated)


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
        env = os.environ.copy()
        if group.id == "frontend_e2e":
            env.pop("CONSOLE_URL", None)
        result = subprocess.run(
            list(group.command),
            cwd=group.cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
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


def build_report(
    mode: str,
    results: Sequence[AuditGroupResult],
    started_at: float,
    *,
    fail_on_skipped: bool = False,
) -> dict[str, object]:
    failed = [item for item in results if item.status == "failed"]
    passed = [item for item in results if item.status == "passed"]
    skipped = [item for item in results if item.status == "skipped"]
    ok = not failed and (not fail_on_skipped or not skipped)
    return {
        "ok": ok,
        "mode": mode,
        "fail_on_skipped": fail_on_skipped,
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
    parser.add_argument(
        "--include-cloud-runtime",
        action="store_true",
        help="Also run the read-only SSH cloud runtime audit. This is intentionally opt-in.",
    )
    parser.add_argument(
        "--fail-on-skipped",
        action="store_true",
        help="Fail the audit when any selected group is skipped. Use this for final acceptance gates.",
    )
    parser.add_argument(
        "--expected-cloud-release",
        default=None,
        help=(
            "Expected cloud release id for cloud_runtime_audit. If omitted while "
            "--include-cloud-runtime and --fail-on-skipped are both set, the current git short HEAD is used."
        ),
    )
    parser.add_argument(
        "--cloud-host",
        default=None,
        help="SSH host for cloud_runtime_audit, for example root@47.84.92.81. Defaults to cloud_runtime_audit.py default.",
    )
    parser.add_argument(
        "--expect-cloud-live-ready",
        action="store_true",
        help="For final live acceptance, require cloud runtime audit to prove live mode and at least one authorized live-ready profile.",
    )
    parser.add_argument(
        "--cloud-runtime-env-mode",
        choices=["trend-live", "cloud-live"],
        default="cloud-live",
        help="Runtime env contract passed to cloud_runtime_audit when --expect-cloud-live-ready is set.",
    )
    parser.add_argument(
        "--cloud-allow-weak-passwords",
        action="store_true",
        help=(
            "Pass --allow-weak-passwords to cloud_runtime_audit. Use only for small-funds gray testing; "
            "large-funds unattended acceptance must omit this flag."
        ),
    )
    parser.add_argument(
        "--cloud-log-minutes",
        type=int,
        default=None,
        help="Recent journal window passed to cloud_runtime_audit. Defaults to cloud_runtime_audit.py default.",
    )
    parser.add_argument(
        "--cloud-expect-peak-pricing-guard",
        action="store_true",
        help="Pass --expect-peak-pricing-guard to cloud_runtime_audit.",
    )
    parser.add_argument("--json-out", type=Path, default=None, help="Optional path to write the JSON audit report.")
    args = parser.parse_args(argv)

    started_at = time.time()
    groups = selected_groups(
        args.mode,
        skip_cloud=args.skip_cloud,
        include_cloud_runtime=args.include_cloud_runtime,
    )
    expected_cloud_release = args.expected_cloud_release
    if (
        expected_cloud_release is None
        and args.include_cloud_runtime
        and not args.skip_cloud
        and args.fail_on_skipped
    ):
        expected_cloud_release = _git_short_head()
    groups = _with_cloud_runtime_expectations(
        groups,
        expected_cloud_release,
        expect_live_ready=args.expect_cloud_live_ready,
        cloud_host=args.cloud_host,
        cloud_runtime_env_mode=args.cloud_runtime_env_mode,
        cloud_allow_weak_passwords=args.cloud_allow_weak_passwords,
        cloud_expect_peak_pricing_guard=args.cloud_expect_peak_pricing_guard,
        cloud_log_minutes=args.cloud_log_minutes,
    )
    results = [
        run_group(group)
        for group in groups
    ]
    report = build_report(args.mode, results, started_at, fail_on_skipped=args.fail_on_skipped)
    file_text = json.dumps(report, ensure_ascii=False, indent=2)
    console_text = json.dumps(report, ensure_ascii=True, indent=2)
    print_report(console_text)
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(file_text + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
