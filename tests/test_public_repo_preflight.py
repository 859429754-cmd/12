from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.public_repo_preflight import run_preflight


def test_public_repo_preflight_keeps_runtime_data_out_and_source_data_in(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text("/data/\n/logs/\n/output/\n.env.*\n!.env.example\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("GATEIO_API_KEY=\n", encoding="utf-8")
    source_dir = tmp_path / "ai_quant_trader" / "data"
    source_dir.mkdir(parents=True)
    (source_dir / "news.py").write_text("NEWS_SOURCE = 'fixture'\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "trader.sqlite3").write_text("runtime", encoding="utf-8")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "audit.jsonl").write_text("runtime", encoding="utf-8")
    (tmp_path / "output" / "playwright").mkdir(parents=True)
    (tmp_path / "output" / "playwright" / "example.png").write_text("runtime", encoding="utf-8")

    result = run_preflight(tmp_path)

    assert result.ok is True
    assert result.ignored_runtime_paths["data/trader.sqlite3"] is True
    assert result.tracked_source_paths["ai_quant_trader/data/news.py"] is True


def test_public_repo_preflight_reports_secret_without_printing_value(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text("", encoding="utf-8")
    (tmp_path / "ai_quant_trader").mkdir()
    (tmp_path / "ai_quant_trader" / "data").mkdir()
    (tmp_path / "ai_quant_trader" / "data" / "news.py").write_text("NEWS_SOURCE = 'fixture'\n", encoding="utf-8")
    fake_secret_line = "API_" + "SECRET = '" + "abcdefghijklmnopqrstuvwxyz" + "'\n"
    (tmp_path / "config.py").write_text(fake_secret_line, encoding="utf-8")

    result = run_preflight(tmp_path, runtime_paths=[])

    assert result.ok is False
    assert result.findings[0].path == "config.py"
    assert result.findings[0].rule == "non_empty_secret_assignment"
