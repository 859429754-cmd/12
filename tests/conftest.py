from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_console_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONSOLE_BASIC_USER", "")
    monkeypatch.setenv("CONSOLE_BASIC_PASSWORD", "")
