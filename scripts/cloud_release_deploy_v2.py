from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

from cloud_release_deploy import REPO_ROOT, build_console_dist_tar, build_source_tar, run


def git_sha() -> str:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--short=12", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def remote_release_script(remote_dir: str, release_id: str, restart: bool, install_deps: bool, health_timeout: int) -> str:
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
            "if ! .venv/bin/python current/scripts/http_readiness_check.py --base-url http://127.0.0.1:8090 --mode health --timeout 5; then\n"
            "  if [ -n \"$previous_target\" ] && [ -e \"$previous_target\" ]; then\n"
            "    ln -sfn \"$previous_target\" \"$current_link\"\n"
            "    systemctl restart ai-quant-console.service ai-quant-trader.service || true\n"
            "  fi\n"
            "  echo 'release_health_check_failed_rolled_back' >&2\n"
            "  exit 50\n"
            "fi\n"
            "if ! .venv/bin/python current/scripts/http_readiness_check.py --base-url http://127.0.0.1:8090 --mode readiness --allow-warn --timeout 5; then\n"
            "  if [ -n \"$previous_target\" ] && [ -e \"$previous_target\" ]; then\n"
            "    ln -sfn \"$previous_target\" \"$current_link\"\n"
            "    systemctl restart ai-quant-console.service ai-quant-trader.service || true\n"
            "  fi\n"
            "  echo 'release_readiness_check_failed_rolled_back' >&2\n"
            "  exit 51\n"
            "fi\n"
        )
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
{install_block}{restart_block}echo "$release_id" > "$remote_dir/releases/.last_successful_release"
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy release/<git_sha> with current symlink and health-gated rollback.")
    parser.add_argument("--host", default="root@8.209.200.19")
    parser.add_argument("--key", default=str(REPO_ROOT / ".ssh" / "aiquant_aliyun"))
    parser.add_argument("--remote-dir", default="/root/ai-quant-trader")
    parser.add_argument("--release-id", default=git_sha())
    parser.add_argument("--install-deps", action="store_true")
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--health-timeout", type=int, default=8)
    args = parser.parse_args()

    key = Path(args.key)
    if not key.exists():
        raise FileNotFoundError(f"SSH key not found: {key}")

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
                    args.release_id,
                    restart=args.restart,
                    install_deps=args.install_deps,
                    health_timeout=args.health_timeout,
                ),
            ]
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
