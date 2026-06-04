from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_console_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONSOLE_BASIC_USER", "")
    monkeypatch.setenv("CONSOLE_BASIC_PASSWORD", "")
    monkeypatch.setenv("CONSOLE_USERS_JSON", "")
    monkeypatch.setenv("CONSOLE_ADMIN_USER", "")
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "")
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD_SHA256", "")
    monkeypatch.setenv("CONSOLE_ACCOUNT1_USER", "")
    monkeypatch.setenv("CONSOLE_ACCOUNT1_PASSWORD", "")
    monkeypatch.setenv("CONSOLE_ACCOUNT1_PASSWORD_SHA256", "")
    monkeypatch.setenv("CONSOLE_ACCOUNT2_USER", "")
    monkeypatch.setenv("CONSOLE_ACCOUNT2_PASSWORD", "")
    monkeypatch.setenv("CONSOLE_ACCOUNT2_PASSWORD_SHA256", "")
    monkeypatch.setenv("CONSOLE_RANGE_USER", "")
    monkeypatch.setenv("CONSOLE_RANGE_PASSWORD", "")
    monkeypatch.setenv("CONSOLE_RANGE_PASSWORD_SHA256", "")
    monkeypatch.setenv("CONSOLE_AUTH_DISABLED", "1")
    monkeypatch.setenv("CONSOLE_PASSWORD_STRENGTH_CONFIRMED", "")
