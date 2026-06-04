from __future__ import annotations

from pathlib import Path

from scripts.cloud_release_deploy import (
    REMOTE_RSYNC_EXCLUDES,
    iter_tracked_source_files,
    remote_sync_script,
    should_include_source,
)


def test_source_bundle_excludes_runtime_state_and_secret_files() -> None:
    root = Path.cwd()

    assert not should_include_source(root / ".env.runtime")
    assert not should_include_source(root / ".venv" / "bin" / "python")
    assert not should_include_source(root / "data" / "trader.sqlite3")
    assert not should_include_source(root / "logs" / "trader.log")
    assert not should_include_source(root / "output" / "preview.png")
    assert not should_include_source(root / "console" / "node_modules" / "x" / "index.js")
    assert not should_include_source(root / "console" / "dist" / "index.html")
    assert should_include_source(root / "ai_quant_trader" / "data" / "market.py")


def test_source_bundle_uses_git_tracked_files_only() -> None:
    root = Path.cwd()
    tracked = {path.relative_to(root).as_posix() for path in iter_tracked_source_files()}

    assert not any("/.scratch/" in path or path.startswith(".scratch/") for path in tracked)
    assert "ai_quant_trader/data/market.py" in tracked
    assert "console/src/App.tsx" in tracked


def test_remote_sync_preserves_runtime_directories() -> None:
    script = remote_sync_script("/root/ai-quant-trader", restart=True, install_deps=False)

    for required in (
        ".env.runtime",
        ".venv/",
        "data/",
        "logs/",
        "output/",
        "backups/",
        "config/config.yaml",
    ):
        assert required in REMOTE_RSYNC_EXCLUDES
        assert f"--exclude '{required}'" in script
    assert "systemctl restart ai-quant-console.service ai-quant-trader.service" in script
