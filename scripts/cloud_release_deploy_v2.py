from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from scripts.cloud_release_deploy import REPO_ROOT, build_console_dist_tar, build_source_tar, run
    from scripts.cloud_runtime_audit import run_audit as run_cloud_audit
except ModuleNotFoundError:  # pragma: no cover - direct script execution path
    from cloud_release_deploy import REPO_ROOT, build_console_dist_tar, build_source_tar, run
    from cloud_runtime_audit import run_audit as run_cloud_audit


def git_sha() -> str:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--short=12", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def playwright_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PLAYWRIGHT_OUTPUT_DIR", str(Path(tempfile.gettempdir()) / "aiquant-playwright-results"))
    return env


def run_console_e2e() -> None:
    npm = "npm.cmd" if os.name == "nt" else "npm"
    subprocess.run([npm, "run", "test:e2e"], cwd=REPO_ROOT / "console", check=True, env=playwright_env())


def run_cloud_console_readonly_e2e(console_url: str) -> None:
    npm = "npm.cmd" if os.name == "nt" else "npm"
    env = playwright_env()
    env["CONSOLE_URL"] = console_url
    required = [
        "AIQUANT_E2E_ACCOUNT1_PASSWORD",
        "AIQUANT_E2E_ACCOUNT2_PASSWORD",
        "AIQUANT_E2E_ADMIN_PASSWORD",
    ]
    missing = [name for name in required if not env.get(name)]
    if missing:
        raise RuntimeError(f"Missing cloud console E2E credential env var(s): {', '.join(missing)}")
    subprocess.run([npm, "run", "test:e2e:cloud"], cwd=REPO_ROOT / "console", check=True, env=env)


def run_post_release_cloud_runtime_audit(
    host: str,
    key: Path,
    remote_dir: str,
    release_id: str,
    *,
    log_minutes: int = 3,
) -> None:
    report = run_cloud_audit(host=host, key=key, remote_dir=remote_dir, expected_release=release_id, log_minutes=log_minutes)
    report_path = REPO_ROOT / "output" / "audit" / f"cloud_runtime_audit_{release_id}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json_dumps_report(report) + "\n",
        encoding="utf-8",
    )
    if not report.ok:
        raise RuntimeError(f"Post-release cloud runtime audit failed: {', '.join(report.failures)}")


def json_dumps_report(report) -> str:  # noqa: ANN001
    import json
    from dataclasses import asdict

    return json.dumps(asdict(report), ensure_ascii=False, indent=2)


def run_full_local_validation() -> None:
    subprocess.run(
        [
            sys.executable,
            "scripts/project_audit_board.py",
            "--mode",
            "full",
            "--skip-cloud",
            "--json-out",
            "output/audit/project_audit_board_release_gate.json",
        ],
        cwd=REPO_ROOT,
        check=True,
    )


def remote_current_target(host: str, key: Path, remote_dir: str) -> str:
    result = subprocess.run(
        [
            "ssh",
            "-i",
            str(key),
            "-o",
            "StrictHostKeyChecking=no",
            host,
            f"readlink -f '{remote_dir}/current' 2>/dev/null || true",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def remote_mark_success_script(remote_dir: str, release_id: str) -> str:
    return f"""set -euo pipefail
remote_dir='{remote_dir}'
release_id='{release_id}'
mkdir -p "$remote_dir/releases"
echo "$release_id" > "$remote_dir/releases/.last_successful_release"
"""


def remote_rollback_script(remote_dir: str, previous_target: str, restart: bool) -> str:
    restart_block = ""
    if restart:
        restart_block = "systemctl restart ai-quant-console.service ai-quant-trader.service || true\n"
    return f"""set -euo pipefail
remote_dir='{remote_dir}'
previous_target='{previous_target}'
current_link="$remote_dir/current"
if [ -n "$previous_target" ] && [ -e "$previous_target" ]; then
  ln -sfn "$previous_target" "$current_link"
  previous_release_id="$(basename "$previous_target")"
  echo "$previous_release_id" > "$remote_dir/releases/.last_successful_release"
  {restart_block}echo 'cloud_console_e2e_failed_rolled_back' >&2
  exit 0
fi
echo 'cloud_console_e2e_failed_no_previous_release' >&2
exit 53
"""


def remote_release_script(
    remote_dir: str,
    release_id: str,
    restart: bool,
    install_deps: bool,
    health_timeout: int,
    mark_success_after_remote: bool = True,
) -> str:
    install_block = ""
    if install_deps:
        install_block = (
            f"cd '{remote_dir}'\n"
            "if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi\n"
            ".venv/bin/python -m pip install -r current/requirements.txt\n"
        )
    restart_block = ""
    if restart:
        restart_block = (
            "systemctl daemon-reload\n"
            "systemctl restart ai-quant-console.service ai-quant-trader.service\n"
            f"sleep {max(health_timeout, 1)}\n"
            "set +u\n"
            "if [ -f \"$remote_dir/.env.runtime\" ]; then set -a; . \"$remote_dir/.env.runtime\"; set +a; fi\n"
            "set -u\n"
            "if ! health_json=\"$(.venv/bin/python current/scripts/http_readiness_check.py --base-url http://127.0.0.1:8090 --mode health --timeout 5)\"; then\n"
            "  if [ -n \"$previous_target\" ] && [ -e \"$previous_target\" ]; then\n"
            "    ln -sfn \"$previous_target\" \"$current_link\"\n"
            "    systemctl restart ai-quant-console.service ai-quant-trader.service || true\n"
            "  fi\n"
            "  echo 'release_health_check_failed_rolled_back' >&2\n"
            "  exit 50\n"
            "fi\n"
            "echo \"$health_json\"\n"
            "if ! readiness_json=\"$(.venv/bin/python current/scripts/http_readiness_check.py --base-url http://127.0.0.1:8090 --mode readiness --allow-warn --timeout 5)\"; then\n"
            "  if [ -n \"$previous_target\" ] && [ -e \"$previous_target\" ]; then\n"
            "    ln -sfn \"$previous_target\" \"$current_link\"\n"
            "    systemctl restart ai-quant-console.service ai-quant-trader.service || true\n"
            "  fi\n"
            "  echo 'release_readiness_check_failed_rolled_back' >&2\n"
            "  exit 51\n"
            "fi\n"
            "echo \"$readiness_json\"\n"
            "current_target=\"$(readlink -f \"$current_link\" || true)\"\n"
            "if ! AIQUANT_RELEASE_ID=\"$release_id\" AIQUANT_RELEASE_STATUS=\"success\" AIQUANT_PREVIOUS_TARGET=\"$previous_target\" AIQUANT_CURRENT_TARGET=\"$current_target\" AIQUANT_HEALTH_JSON=\"$health_json\" AIQUANT_READINESS_JSON=\"$readiness_json\" .venv/bin/python current/scripts/record_release_run.py --remote-dir \"$remote_dir\"; then\n"
            "  if [ -n \"$previous_target\" ] && [ -e \"$previous_target\" ]; then\n"
            "    ln -sfn \"$previous_target\" \"$current_link\"\n"
            "    systemctl restart ai-quant-console.service ai-quant-trader.service || true\n"
            "  fi\n"
            "  echo 'release_audit_record_failed_rolled_back' >&2\n"
            "  exit 52\n"
            "fi\n"
        )
    success_marker = f'echo "$release_id" > "$remote_dir/releases/.last_successful_release"\n' if mark_success_after_remote else ""
    return f"""set -euo pipefail
remote_dir='{remote_dir}'
release_id='{release_id}'
release_root="$remote_dir/releases"
release_dir="$release_root/$release_id"
current_link="$remote_dir/current"
previous_target=""
if [ -L "$current_link" ]; then
  previous_target="$(readlink -f "$current_link" || true)"
fi
src_stage='/tmp/aiquant-release-src'
console_stage='/tmp/aiquant-release-console'
rm -rf "$src_stage" "$console_stage" "$release_dir"
mkdir -p "$src_stage" "$console_stage" "$release_dir" "$release_root"
tar -xf /tmp/aiquant-src-current.tar -C "$src_stage"
tar -xf /tmp/aiquant-console-dist.tar -C "$console_stage"
rsync -a --delete "$src_stage"/ "$release_dir"/
rm -rf "$remote_dir/console/dist"
mkdir -p "$remote_dir/console/dist"
rsync -a --delete "$console_stage"/ "$remote_dir/console/dist"/
ln -sfn "$release_dir" "$current_link"
if [ -d "$release_dir/deploy/systemd" ]; then
  cp "$release_dir"/deploy/systemd/*.service "$release_dir"/deploy/systemd/*.timer /etc/systemd/system/
fi
cd "$remote_dir"
{install_block}{restart_block}{success_marker}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy release/<git_sha> with current symlink and health-gated rollback.")
    parser.add_argument("--host", default="root@8.209.200.19")
    parser.add_argument("--key", default=str(REPO_ROOT / ".ssh" / "aiquant_aliyun"))
    parser.add_argument("--remote-dir", default="/root/ai-quant-trader")
    parser.add_argument("--release-id", default=None)
    parser.add_argument("--install-deps", action="store_true")
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--health-timeout", type=int, default=20)
    parser.add_argument(
        "--run-console-e2e",
        action="store_true",
        help="Run the Playwright console smoke tests before uploading the release.",
    )
    parser.add_argument(
        "--full-local-validation",
        action="store_true",
        help="Run compileall, pytest, console build, public preflight, and console E2E before uploading.",
    )
    parser.add_argument(
        "--cloud-console-readonly-e2e-url",
        default=None,
        help="After a successful restart, run read-only Playwright smoke tests against this public console URL.",
    )
    args = parser.parse_args()

    key = Path(args.key)
    if not key.exists():
        raise FileNotFoundError(f"SSH key not found: {key}")
    release_id = args.release_id or git_sha()

    if args.full_local_validation:
        run_full_local_validation()
    elif args.run_console_e2e:
        run_console_e2e()

    previous_target_for_local_gate = ""
    if args.cloud_console_readonly_e2e_url:
        previous_target_for_local_gate = remote_current_target(args.host, key, args.remote_dir)

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        source_tar = tmpdir / "aiquant-src-current.tar"
        console_tar = tmpdir / "aiquant-console-dist.tar"
        build_source_tar(source_tar)
        build_console_dist_tar(console_tar)
        scp_base = ["scp", "-O", "-i", str(key), "-o", "StrictHostKeyChecking=no"]
        run([*scp_base, str(source_tar), f"{args.host}:/tmp/aiquant-src-current.tar"])
        run([*scp_base, str(console_tar), f"{args.host}:/tmp/aiquant-console-dist.tar"])
        ssh_base = ["ssh", "-i", str(key), "-o", "StrictHostKeyChecking=no", args.host]
        run(
            [
                *ssh_base,
                remote_release_script(
                    args.remote_dir,
                    release_id,
                    restart=args.restart,
                    install_deps=args.install_deps,
                    health_timeout=args.health_timeout,
                    mark_success_after_remote=not bool(args.cloud_console_readonly_e2e_url),
                ),
            ]
        )
    if args.cloud_console_readonly_e2e_url:
        ssh_base = ["ssh", "-i", str(key), "-o", "StrictHostKeyChecking=no", args.host]
        try:
            run_cloud_console_readonly_e2e(args.cloud_console_readonly_e2e_url)
        except Exception:
            run([*ssh_base, remote_rollback_script(args.remote_dir, previous_target_for_local_gate, restart=args.restart)])
            raise
        run([*ssh_base, remote_mark_success_script(args.remote_dir, release_id)])
        try:
            run_post_release_cloud_runtime_audit(args.host, key, args.remote_dir, release_id)
        except Exception:
            run([*ssh_base, remote_rollback_script(args.remote_dir, previous_target_for_local_gate, restart=args.restart)])
            raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
