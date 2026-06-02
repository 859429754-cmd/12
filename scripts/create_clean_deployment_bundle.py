from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT_EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".scratch",
    ".ssh",
    "data",
    "logs",
    "output",
    "node_modules",
    "test-results",
    "playwright-report",
}

EXCLUDED_PARTS = {
    "console/node_modules",
    "console/output",
    "console/test-results",
}

EXCLUDED_FILE_PATTERNS = [
    ".env",
    ".env.*",
    "*.pyc",
    "*.pyo",
    "*.sqlite3",
    "*.sqlite3-*",
    "*.sqlite3.gz",
    "*.db",
    "*.jsonl",
    "*.log",
    "*.tsbuildinfo",
    "*.bak",
    "*.pem",
    "*.key",
    "*.ppk",
    "id_rsa",
    "id_ed25519",
]

ALLOWED_ENV_FILES = {".env.example"}

HIGH_CONFIDENCE_SECRET_PATTERNS = [
    "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
    "-----BEGIN " + "RSA PRIVATE KEY-----",
    "-----BEGIN " + "EC PRIVATE KEY-----",
    "gh" + "p_",
    "github" + "_pat_",
]

HIGH_CONFIDENCE_SECRET_REGEXES = [
    re.compile(re.escape("sk" + "-") + r"[A-Za-z0-9_\-]{20,}"),
]


def _run(cmd: list[str], cwd: Path) -> str:
    try:
        return subprocess.check_output(cmd, cwd=str(cwd), text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""


def _normalised_relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_excluded(path: Path, root: Path) -> bool:
    rel = _normalised_relative(path, root)
    parts = rel.split("/")

    if parts[0] in ROOT_EXCLUDED_DIRS:
        return True

    if any(part in {"__pycache__", ".pytest_cache", "node_modules"} for part in parts):
        return True

    for excluded in EXCLUDED_PARTS:
        if rel == excluded or rel.startswith(f"{excluded}/"):
            return True

    if path.is_file():
        name = path.name
        if name in ALLOWED_ENV_FILES:
            return False
        for pattern in EXCLUDED_FILE_PATTERNS:
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern):
                return True

    return False


def _copy_tree(source: Path, destination: Path) -> list[str]:
    copied: list[str] = []
    if destination.exists():
        shutil.rmtree(destination, onerror=_handle_remove_readonly)
    destination.mkdir(parents=True, exist_ok=True)

    for path in sorted(source.rglob("*")):
        if _is_excluded(path, source):
            continue
        rel = path.relative_to(source)
        target = destination / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(rel.as_posix())

    return copied


def _handle_remove_readonly(func, path: str, _exc_info: object) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _write_runtime_template(bundle_dir: Path) -> None:
    env_example = bundle_dir / ".env.example"
    runtime_template = bundle_dir / ".env.runtime.template"
    if env_example.exists():
        shutil.copy2(env_example, runtime_template)
    else:
        runtime_template.write_text(
            "\n".join(
                [
                    "DEEPSEEK_API_KEY=",
                    "DEEPSEEK_BASE_URL=https://api.deepseek.com",
                    "GATEIO_TREND_API_KEY=",
                    "GATEIO_TREND_API_SECRET=",
                    "TRADE_PIN=",
                    "CONSOLE_OPERATION_CODE=",
                    "",
                ]
            ),
            encoding="utf-8",
        )


def _write_first_readme(bundle_dir: Path) -> None:
    text = """# 先读我：AI量化交易系统部署包

这是一个干净部署包，不包含真实密钥、SQLite、日志、订单状态或当前持仓。

## 必须先做

1. 复制 `.env.runtime.template` 为 `.env.runtime`。
2. 在目标机器本地填写 DeepSeek 和 Gate.io API。
3. 不要把 `.env.runtime` 发到聊天、GitHub 或日志。
4. 先运行 mock 和 readiness 验收。
5. 只有用户明确确认后才能切 live。

## 详细部署文档

请阅读：

- `docs/ai-quant-system-copy-summary.md`
- `docs/clone-install-runbook.md`
- `deploy/systemd/README.md`

## 核心验收命令

```bash
python -m compileall ai_quant_trader tests scripts
python -m pytest -q
python scripts/public_repo_preflight.py
python scripts/gate_live_readiness.py --config config/config.yaml --env-file .env.runtime
```

前端：

```bash
cd console
npm install
npm run build
```

## 实盘边界

当前主策略是 ETH 1h KC + VOL + KDJ 趋势突破。DeepSeek 只做五档仓位裁剪和否决，不允许绕过本地策略信号和 RiskManager。
"""
    (bundle_dir / "README_DEPLOY_FIRST.md").write_text(text, encoding="utf-8")


def _scan_for_secrets(bundle_dir: Path) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for path in sorted(bundle_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".ico", ".woff", ".woff2", ".zip", ".gz", ".tgz", ".pack"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for marker in HIGH_CONFIDENCE_SECRET_PATTERNS:
            if marker in text:
                findings.append(
                    {
                        "file": path.relative_to(bundle_dir).as_posix(),
                        "rule": marker,
                    }
                )
        for pattern in HIGH_CONFIDENCE_SECRET_REGEXES:
            if pattern.search(text):
                findings.append(
                    {
                        "file": path.relative_to(bundle_dir).as_posix(),
                        "rule": pattern.pattern,
                    }
                )
    return findings


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_manifest(source: Path, bundle_dir: Path, copied_files: list[str], findings: list[dict[str, str]]) -> None:
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_path": str(source),
        "source_git_commit": _run(["git", "rev-parse", "HEAD"], source),
        "source_git_branch": _run(["git", "branch", "--show-current"], source),
        "bundle_name": bundle_dir.name,
        "copied_file_count": len(copied_files),
        "excluded": {
            "runtime_state": ["data/", "logs/", "output/", "*.sqlite3", "*.jsonl", "*.log"],
            "secrets": [".env.runtime", ".env.* except .env.example", ".ssh/", "*.pem", "*.key"],
            "dependencies": [".venv/", "console/node_modules/"],
        },
        "secret_scan_findings": findings,
        "deployment_docs": [
            "README_DEPLOY_FIRST.md",
            "docs/ai-quant-system-copy-summary.md",
            "docs/clone-install-runbook.md",
            "deploy/systemd/README.md",
        ],
    }
    (bundle_dir / "DEPLOYMENT_MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _zip_bundle(bundle_dir: Path) -> Path:
    zip_path = bundle_dir.with_suffix(".zip")
    if zip_path.exists():
        zip_path.unlink()
    with ZipFile(zip_path, "w", ZIP_DEFLATED) as archive:
        for path in sorted(bundle_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(bundle_dir.parent))
    return zip_path


def create_bundle(source: Path, output_root: Path, name: str, make_zip: bool) -> dict[str, object]:
    source = source.resolve()
    output_root = output_root.resolve()
    bundle_dir = output_root / name
    copied = _copy_tree(source, bundle_dir)
    _write_runtime_template(bundle_dir)
    _write_first_readme(bundle_dir)
    findings = _scan_for_secrets(bundle_dir)
    _write_manifest(source, bundle_dir, copied, findings)

    zip_path = _zip_bundle(bundle_dir) if make_zip else None
    return {
        "bundle_dir": str(bundle_dir),
        "zip_path": str(zip_path) if zip_path else None,
        "copied_file_count": len(copied),
        "secret_findings": findings,
        "zip_sha256": _sha256(zip_path) if zip_path else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a clean deployable copy of the AI quant trading system.")
    parser.add_argument("--source", default=".", help="Repository root.")
    parser.add_argument("--output-root", default=r"C:\Users\杨\Documents\Codex\deployment-bundles")
    parser.add_argument("--name", default=f"ai-quant-trader-clean-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    parser.add_argument("--no-zip", action="store_true", help="Only create folder, skip zip.")
    args = parser.parse_args()

    result = create_bundle(
        source=Path(args.source),
        output_root=Path(args.output_root),
        name=args.name,
        make_zip=not args.no_zip,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if result["secret_findings"]:
        print("High-confidence secret findings detected; do not share this bundle.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
