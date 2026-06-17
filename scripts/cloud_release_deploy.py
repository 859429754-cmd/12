from __future__ import annotations

import argparse
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

SOURCE_EXCLUDES = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "node_modules",
    "data",
    "logs",
    "output",
    "backups",
    "console/dist",
    "console/node_modules",
    "console/test-results",
    "console/playwright-report",
    "console/tsconfig.tsbuildinfo",
    ".env.runtime",
}

REMOTE_RSYNC_EXCLUDES = (
    ".env.runtime",
    ".venv/",
    "/data/",
    "/logs/",
    "/output/",
    "/backups/",
    "config/config.yaml",
    "console/dist/",
    "console/node_modules/",
)


def _relative_key(path: Path) -> str:
    key = path.relative_to(REPO_ROOT).as_posix()
    return key


def should_include_source(path: Path) -> bool:
    key = _relative_key(path)
    parts = set(key.split("/"))
    if key in SOURCE_EXCLUDES:
        return False
    if parts & {"__pycache__", ".pytest_cache", ".mypy_cache", "node_modules"}:
        return False
    for excluded in SOURCE_EXCLUDES:
        if key.startswith(f"{excluded}/"):
            return False
    return True


def iter_tracked_source_files() -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    files: list[Path] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        path = REPO_ROOT / raw.decode("utf-8")
        if path.is_file() and should_include_source(path):
            files.append(path)
    return files


def build_source_tar(target: Path) -> None:
    with tarfile.open(target, "w") as archive:
        for path in iter_tracked_source_files():
            archive.add(path, arcname=_relative_key(path))


def build_console_dist_tar(target: Path) -> None:
    dist = REPO_ROOT / "console" / "dist"
    if not dist.exists():
        raise FileNotFoundError("console/dist does not exist; run `cd console && npm.cmd run build` first.")
    with tarfile.open(target, "w") as archive:
        for path in dist.rglob("*"):
            if path.is_file():
                archive.add(path, arcname=path.relative_to(dist).as_posix())


def remote_sync_script(remote_dir: str, restart: bool, install_deps: bool) -> str:
    excludes = " ".join(f"--exclude '{item}'" for item in REMOTE_RSYNC_EXCLUDES)
    restart_block = ""
    if restart:
        restart_block = (
            "systemctl daemon-reload\n"
            "systemctl restart ai-quant-console.service ai-quant-trader.service\n"
        )
    install_block = ""
    if install_deps:
        install_block = (
            f"cd '{remote_dir}'\n"
            "if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi\n"
            ".venv/bin/python -m pip install -r requirements.txt\n"
        )
    return f"""set -euo pipefail
remote_dir='{remote_dir}'
stage='/tmp/aiquant-src-current'
console_stage='/tmp/aiquant-console-dist'
rm -rf "$stage" "$console_stage"
mkdir -p "$stage" "$console_stage"
tar -xf /tmp/aiquant-src-current.tar -C "$stage"
tar -xf /tmp/aiquant-console-dist.tar -C "$console_stage"
mkdir -p "$remote_dir"
rsync -a --delete {excludes} "$stage"/ "$remote_dir"/
rm -rf "$remote_dir/console/dist"
mkdir -p "$remote_dir/console/dist"
rsync -a --delete "$console_stage"/ "$remote_dir/console/dist"/
{install_block}{restart_block}"""


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy a protected release to the AI quant cloud host.")
    parser.add_argument("--host", default="root@8.209.200.19")
    parser.add_argument("--key", default=str(REPO_ROOT / ".ssh" / "aiquant_aliyun"))
    parser.add_argument("--remote-dir", default="/root/ai-quant-trader")
    parser.add_argument("--install-deps", action="store_true", help="Install Python dependencies on the remote host.")
    parser.add_argument("--restart", action="store_true", help="Restart console and trader systemd services after sync.")
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
        run([*ssh_base, remote_sync_script(args.remote_dir, restart=args.restart, install_deps=args.install_deps)])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
