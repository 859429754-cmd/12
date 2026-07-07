from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.runtime_env_audit import audit_runtime_env


def write_env(path: Path, values: dict[str, str]) -> None:
    path.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8")


def complete_cloud_values() -> dict[str, str]:
    return {
        "DEEPSEEK_API_KEY": "primary-deepseek-key-123",
        "DEEPSEEK_BACKUP_API_KEY": "backup-deepseek-key-456",
        "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
        "GATEIO_TREND_API_KEY": "trend-key",
        "GATEIO_TREND_API_SECRET": "trend-secret",
        "GATEIO_FOLLOWER_API_KEY": "follower-key",
        "GATEIO_FOLLOWER_API_SECRET": "follower-secret",
        "CONSOLE_ADMIN_USER": "admin",
        "CONSOLE_ADMIN_PASSWORD": "AdminStrong123",
        "CONSOLE_ACCOUNT1_USER": "account1",
        "CONSOLE_ACCOUNT1_PASSWORD": "AccountOne123",
        "CONSOLE_ACCOUNT2_USER": "account2",
        "CONSOLE_ACCOUNT2_PASSWORD": "AccountTwo123",
        "CONSOLE_PASSWORD_STRENGTH_CONFIRMED": "1",
        "CONSOLE_CORS_ORIGINS": "http://47.84.92.81",
    }


def test_runtime_env_audit_base_accepts_legacy_gate_pair(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.runtime"
    write_env(
        env_file,
        {
            "DEEPSEEK_API_KEY": "deepseek-primary",
            "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
            "GATEIO_API_KEY": "legacy-key",
            "GATEIO_API_SECRET": "legacy-secret",
        },
    )

    result = audit_runtime_env(env_file, mode="base")

    assert result.ok is True
    assert "using_legacy_gate_credentials" in result.warnings


def test_runtime_env_audit_cloud_live_requires_backup_follower_and_console(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.runtime"
    write_env(
        env_file,
        {
            "DEEPSEEK_API_KEY": "deepseek-primary",
            "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
            "GATEIO_TREND_API_KEY": "trend-key",
            "GATEIO_TREND_API_SECRET": "trend-secret",
        },
    )

    result = audit_runtime_env(env_file, mode="cloud-live")

    assert result.ok is False
    assert "DEEPSEEK_BACKUP_API_KEY" in result.missing
    assert "GATEIO_FOLLOWER_API_KEY" in result.missing
    assert "CONSOLE_ADMIN_PASSWORD" in result.missing


def test_runtime_env_audit_cloud_live_rejects_auth_disabled_and_weak_passwords(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.runtime"
    values = complete_cloud_values()
    values["CONSOLE_AUTH_DISABLED"] = "1"
    values["CONSOLE_ACCOUNT1_PASSWORD"] = "yx"
    write_env(env_file, values)

    result = audit_runtime_env(env_file, mode="cloud-live")

    assert result.ok is False
    assert "console_auth_disabled" in result.failures
    assert "weak_password:CONSOLE_ACCOUNT1_PASSWORD" in result.failures


def test_runtime_env_audit_can_allow_weak_passwords_for_small_funds_grey_test(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.runtime"
    values = complete_cloud_values()
    values["CONSOLE_ACCOUNT2_PASSWORD"] = "wx"
    write_env(env_file, values)

    result = audit_runtime_env(env_file, mode="cloud-live", allow_weak_passwords=True)

    assert result.ok is True
    assert "weak_password_allowed:CONSOLE_ACCOUNT2_PASSWORD" in result.warnings


def test_runtime_env_audit_cli_does_not_print_secret_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.runtime"
    secret_value = "super-secret-value-12345"
    values = complete_cloud_values()
    values["DEEPSEEK_API_KEY"] = secret_value
    write_env(env_file, values)

    result = subprocess.run(
        [sys.executable, "scripts/runtime_env_audit.py", "--env-file", str(env_file), "--mode", "cloud-live"],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)

    assert secret_value not in result.stdout
    assert payload["keys"]["DEEPSEEK_API_KEY"]["present"] is True
    assert payload["keys"]["DEEPSEEK_API_KEY"]["nonempty"] is True
    assert payload["keys"]["DEEPSEEK_API_KEY"]["length"] == len(secret_value)
