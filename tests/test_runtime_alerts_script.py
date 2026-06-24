from __future__ import annotations

import base64
import json

from scripts import runtime_alerts


def test_alert_script_uses_admin_from_console_users_json(monkeypatch) -> None:
    monkeypatch.delenv("CONSOLE_ALERT_USER", raising=False)
    monkeypatch.delenv("CONSOLE_ALERT_PASSWORD", raising=False)
    monkeypatch.delenv("CONSOLE_BASIC_USER", raising=False)
    monkeypatch.delenv("CONSOLE_BASIC_PASSWORD", raising=False)
    monkeypatch.delenv("CONSOLE_ADMIN_USER", raising=False)
    monkeypatch.delenv("CONSOLE_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv(
        "CONSOLE_USERS_JSON",
        json.dumps(
            {
                "users": [
                    {"username": "account1", "role": "account1", "password": "yx"},
                    {"username": "admin", "role": "admin", "password": "1234567"},
                ]
            }
        ),
    )

    header = runtime_alerts._auth_header()
    encoded = header["Authorization"].split(" ", 1)[1]

    assert base64.b64decode(encoded).decode("utf-8") == "admin:1234567"


def test_alert_script_strips_env_file_quotes(monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_BASIC_USER", '"admin"')
    monkeypatch.setenv("CONSOLE_BASIC_PASSWORD", "'secret'")

    header = runtime_alerts._auth_header()
    encoded = header["Authorization"].split(" ", 1)[1]

    assert base64.b64decode(encoded).decode("utf-8") == "admin:secret"
