from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SERVICES = ("ai-quant-trader.service", "ai-quant-console.service", "nginx", "fail2ban")
ERROR_PATTERN = r"Traceback|ERROR|Exception|RuntimeError|critical"
TAIL_CHARS = 6000


SECRET_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9]{12,}"), "sk-[REDACTED]"),
    (re.compile(r"(?i)(password|token|secret|api[_-]?key)\s*[:=]\s*['\"]?[^'\"\s]{8,}"), r"\1=[REDACTED]"),
)


@dataclass(frozen=True)
class RemoteCommandResult:
    ok: bool
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class CloudRuntimeAudit:
    ok: bool
    host: str
    remote_dir: str
    expected_release: str | None
    current_target: str | None
    last_successful_release: str | None
    service_statuses: dict[str, str]
    readiness: dict[str, object] | None
    latest_release_runs: list[dict[str, object]]
    recent_error_log_tail: str
    failures: list[str]
    duration_seconds: float


def _redact(text: str | bytes | None) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    cleaned = text[-TAIL_CHARS:]
    for pattern, replacement in SECRET_REDACTIONS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def _ssh_base(host: str, key: Path) -> list[str]:
    return ["ssh", "-i", str(key), "-o", "StrictHostKeyChecking=no", host]


def run_remote(host: str, key: Path, script: str, timeout: int = 30) -> RemoteCommandResult:
    result = subprocess.run(
        [*_ssh_base(host, key), script],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    return RemoteCommandResult(
        ok=result.returncode == 0,
        exit_code=result.returncode,
        stdout=_redact(result.stdout),
        stderr=_redact(result.stderr),
    )


def _parse_json_line(text: str) -> dict[str, object] | None:
    stripped = text.strip()
    if not stripped:
        return None
    return json.loads(stripped.splitlines()[-1])


def _current_target(host: str, key: Path, remote_dir: str) -> tuple[str | None, list[str]]:
    result = run_remote(host, key, f"readlink -f '{remote_dir}/current' 2>/dev/null || true")
    failures = [] if result.ok else [f"current_target_ssh_failed:{result.exit_code}:{result.stderr}"]
    return (result.stdout.strip() or None), failures


def _last_successful_release(host: str, key: Path, remote_dir: str) -> tuple[str | None, list[str]]:
    result = run_remote(host, key, f"cat '{remote_dir}/releases/.last_successful_release' 2>/dev/null || true")
    failures = [] if result.ok else [f"last_success_ssh_failed:{result.exit_code}:{result.stderr}"]
    return (result.stdout.strip() or None), failures


def _service_statuses(host: str, key: Path, services: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    result = run_remote(host, key, "systemctl is-active " + " ".join(services))
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    statuses = {service: (lines[idx] if idx < len(lines) else "missing") for idx, service in enumerate(services)}
    failures: list[str] = []
    if not result.ok:
        failures.append(f"systemctl_is_active_failed:{result.exit_code}:{result.stderr}")
    for service, status in statuses.items():
        if status != "active":
            failures.append(f"service_not_active:{service}:{status}")
    return statuses, failures


def _readiness(host: str, key: Path, remote_dir: str) -> tuple[dict[str, object] | None, list[str]]:
    command = (
        "python3 - <<'PY'\n"
        "import json\n"
        "import urllib.request\n"
        "payload = json.load(urllib.request.urlopen('http://127.0.0.1:8090/api/system/readiness', timeout=5))\n"
        "keys = [\n"
        "    'ok', 'overall', 'blocking', 'execution_mode', 'trade_mode',\n"
        "    'profile_count', 'enabled_profile_count', 'authorized_profile_count', 'live_ready_profile_count',\n"
        "    'deepseek_ready',\n"
        "]\n"
        "print(json.dumps({key: payload.get(key) for key in keys}, ensure_ascii=True))\n"
        "PY"
    )
    result = run_remote(host, key, command, timeout=45)
    failures: list[str] = []
    payload: dict[str, object] | None = None
    if not result.ok:
        detail = result.stderr.strip() or result.stdout.strip()
        failures.append(f"readiness_check_failed:{result.exit_code}:{detail}")
        return None, failures
    try:
        payload = _parse_json_line(result.stdout)
    except json.JSONDecodeError as exc:
        failures.append(f"readiness_json_invalid:{exc}")
        return None, failures
    overall = payload.get("overall") if payload else None
    if overall not in {"ok", "warn"}:
        failures.append(f"readiness_not_ok:{overall}")
    return payload, failures


def _latest_release_runs(host: str, key: Path, remote_dir: str, limit: int) -> tuple[list[dict[str, object]], list[str]]:
    script = f"""python3 - <<'PY'
import json
import sqlite3
from pathlib import Path
db = Path({str(remote_dir + '/data/trader.sqlite3')!r})
if not db.exists():
    print(json.dumps({{"rows": [], "error": "database_missing"}}))
    raise SystemExit(0)
con = sqlite3.connect(str(db))
rows = []
for created_at, payload_text in con.execute("select created_at,payload from release_runs order by id desc limit {int(limit)}"):
    payload = json.loads(payload_text)
    rows.append({{"created_at": created_at, "release_id": payload.get("release_id"), "status": payload.get("status")}})
print(json.dumps({{"rows": rows}}, ensure_ascii=True))
PY"""
    result = run_remote(host, key, script, timeout=45)
    failures: list[str] = []
    if not result.ok:
        return [], [f"release_runs_query_failed:{result.exit_code}:{result.stderr}"]
    try:
        payload = _parse_json_line(result.stdout) or {}
    except json.JSONDecodeError as exc:
        return [], [f"release_runs_json_invalid:{exc}"]
    if payload.get("error"):
        failures.append(str(payload["error"]))
    rows = payload.get("rows", [])
    if not isinstance(rows, list):
        return [], ["release_runs_rows_invalid"]
    return rows, failures


def _recent_errors(host: str, key: Path, minutes: int) -> tuple[str, list[str]]:
    command = (
        "journalctl -u ai-quant-trader.service -u ai-quant-console.service "
        f"--since '{int(minutes)} minutes ago' --no-pager | grep -Ei '{ERROR_PATTERN}' || true"
    )
    result = run_remote(host, key, command, timeout=45)
    failures: list[str] = []
    if not result.ok:
        failures.append(f"journalctl_query_failed:{result.exit_code}:{result.stderr}")
    error_tail = result.stdout.strip()
    if error_tail:
        failures.append("recent_service_errors_detected")
    return error_tail, failures


def run_audit(
    *,
    host: str,
    key: Path,
    remote_dir: str,
    expected_release: str | None = None,
    expect_live_ready: bool = False,
    log_minutes: int = 15,
    release_run_limit: int = 3,
    services: Sequence[str] = DEFAULT_SERVICES,
) -> CloudRuntimeAudit:
    started_at = time.time()
    failures: list[str] = []
    if not key.exists():
        failures.append(f"ssh_key_missing:{key}")
        return CloudRuntimeAudit(
            ok=False,
            host=host,
            remote_dir=remote_dir,
            expected_release=expected_release,
            current_target=None,
            last_successful_release=None,
            service_statuses={},
            readiness=None,
            latest_release_runs=[],
            recent_error_log_tail="",
            failures=failures,
            duration_seconds=round(time.time() - started_at, 3),
        )

    current_target, step_failures = _current_target(host, key, remote_dir)
    failures.extend(step_failures)
    last_success, step_failures = _last_successful_release(host, key, remote_dir)
    failures.extend(step_failures)
    service_statuses, step_failures = _service_statuses(host, key, services)
    failures.extend(step_failures)
    readiness, step_failures = _readiness(host, key, remote_dir)
    failures.extend(step_failures)
    release_runs, step_failures = _latest_release_runs(host, key, remote_dir, release_run_limit)
    failures.extend(step_failures)
    error_tail, step_failures = _recent_errors(host, key, log_minutes)
    failures.extend(step_failures)

    if expected_release:
        if last_success != expected_release:
            failures.append(f"last_successful_release_mismatch:{last_success}!={expected_release}")
        if not current_target:
            failures.append("current_target_missing")
        elif not current_target.rstrip("/").endswith(f"/{expected_release}"):
            failures.append(f"current_target_mismatch:{current_target}!={expected_release}")
        if release_runs and release_runs[0].get("release_id") != expected_release:
            failures.append(f"latest_release_run_mismatch:{release_runs[0].get('release_id')}!={expected_release}")
        if release_runs and release_runs[0].get("status") != "success":
            failures.append(f"latest_release_run_not_success:{release_runs[0].get('status')}")

    if expect_live_ready:
        if not readiness:
            failures.append("live_ready_readiness_missing")
        else:
            if readiness.get("execution_mode") != "live":
                failures.append(f"execution_mode_not_live:{readiness.get('execution_mode')}")
            try:
                authorized_count = int(readiness.get("authorized_profile_count") or 0)
            except (TypeError, ValueError):
                authorized_count = 0
            try:
                live_ready_count = int(readiness.get("live_ready_profile_count") or 0)
            except (TypeError, ValueError):
                live_ready_count = 0
            if authorized_count < 1:
                failures.append(f"authorized_profile_count_too_low:{authorized_count}")
            if live_ready_count < 1:
                failures.append(f"live_ready_profile_count_too_low:{live_ready_count}")

    return CloudRuntimeAudit(
        ok=not failures,
        host=host,
        remote_dir=remote_dir,
        expected_release=expected_release,
        current_target=current_target,
        last_successful_release=last_success,
        service_statuses=service_statuses,
        readiness=readiness,
        latest_release_runs=release_runs,
        recent_error_log_tail=error_tail,
        failures=failures,
        duration_seconds=round(time.time() - started_at, 3),
    )


def print_report(text: str) -> None:
    encoding = sys.stdout.encoding or "utf-8"
    print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a read-only cloud runtime audit without reading runtime secrets.")
    parser.add_argument("--host", default="root@8.209.200.19")
    parser.add_argument("--key", type=Path, default=ROOT / ".ssh" / "aiquant_aliyun")
    parser.add_argument("--remote-dir", default="/root/ai-quant-trader")
    parser.add_argument("--expected-release", default=None)
    parser.add_argument(
        "--expect-live-ready",
        action="store_true",
        help="Fail unless cloud readiness reports live mode with at least one authorized and live-ready profile.",
    )
    parser.add_argument("--log-minutes", type=int, default=15)
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args(argv)

    report = run_audit(
        host=args.host,
        key=args.key,
        remote_dir=args.remote_dir,
        expected_release=args.expected_release,
        expect_live_ready=args.expect_live_ready,
        log_minutes=args.log_minutes,
    )
    payload = asdict(report)
    file_text = json.dumps(payload, ensure_ascii=False, indent=2)
    console_text = json.dumps(payload, ensure_ascii=True, indent=2)
    print_report(console_text)
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(file_text + "\n", encoding="utf-8")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
