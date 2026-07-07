from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts.runtime_env_repair import parse_env, repair_runtime_env


def test_runtime_env_repair_strips_bom_and_maps_legacy_gate_pair(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.runtime"
    env_file.write_text(
        "\ufeffDEEPSEEK_API_KEY=deepseek-secret\n"
        "DEEPSEEK_BASE_URL=https://api.deepseek.com\n"
        "GATEIO_API_KEY=legacy-key\n"
        "GATEIO_API_SECRET=legacy-secret\n",
        encoding="utf-8",
    )

    result = repair_runtime_env(env_file, cors_origin="http://47.84.92.81", apply=True)
    values = parse_env(env_file)

    assert result.dry_run is False
    assert result.backup_file is not None
    assert "strip_utf8_bom" in result.changed_keys
    assert values["DEEPSEEK_API_KEY"] == "deepseek-secret"
    assert values["GATEIO_TREND_API_KEY"] == "legacy-key"
    assert values["GATEIO_TREND_API_SECRET"] == "legacy-secret"
    assert values["CONSOLE_ADMIN_USER"] == "admin"
    assert values["CONSOLE_CORS_ORIGINS"] == "http://47.84.92.81"
    assert not env_file.read_text(encoding="utf-8").startswith("\ufeff")


def test_runtime_env_repair_dry_run_does_not_write_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.runtime"
    original = "GATEIO_API_KEY=legacy-key\nGATEIO_API_SECRET=legacy-secret\n"
    env_file.write_text(original, encoding="utf-8")

    result = repair_runtime_env(env_file, cors_origin="http://47.84.92.81", apply=False)

    assert result.dry_run is True
    assert "GATEIO_TREND_API_KEY" in result.changed_keys
    assert env_file.read_text(encoding="utf-8") == original


def test_runtime_env_repair_cli_does_not_print_secret_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.runtime"
    secret = "super-secret-value-12345"
    env_file.write_text(f"GATEIO_API_KEY={secret}\nGATEIO_API_SECRET=legacy-secret\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "scripts/runtime_env_repair.py", "--env-file", str(env_file), "--cors-origin", "http://47.84.92.81"],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )

    assert secret not in result.stdout
    assert "GATEIO_TREND_API_KEY" in result.stdout
