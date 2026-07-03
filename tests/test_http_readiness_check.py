from __future__ import annotations

import io
import json
from urllib.error import HTTPError

from scripts import http_readiness_check


class FakeHeaders(dict):
    def get(self, key: str, default=None):  # noqa: ANN001
        return super().get(key, default)


class FakeResponse:
    def __init__(self, payload: dict, *, cookie: str | None = None) -> None:
        self.payload = payload
        self.headers = FakeHeaders()
        if cookie:
            self.headers["Set-Cookie"] = cookie

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_http_readiness_check_logs_in_when_readiness_requires_session(monkeypatch, capsys) -> None:
    monkeypatch.setenv("CONSOLE_ADMIN_USER", "admin")
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "admin-secret")
    calls: list[str] = []

    def fake_urlopen(request, timeout):  # noqa: ANN001
        calls.append(request.full_url)
        if request.full_url.endswith("/api/system/readiness") and not request.get_header("Cookie"):
            raise HTTPError(request.full_url, 401, "auth_required", {}, io.BytesIO())
        if request.full_url.endswith("/api/auth/login"):
            assert request.data is not None
            assert b"admin-secret" in request.data
            return FakeResponse({"ok": True}, cookie="aiq_session=test-session; Path=/")
        if request.full_url.endswith("/api/system/readiness"):
            assert request.get_header("Cookie") == "aiq_session=test-session; Path=/"
            return FakeResponse({"overall": "ok", "checks": []})
        raise AssertionError(request.full_url)

    monkeypatch.setattr(http_readiness_check, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        "sys.argv",
        ["http_readiness_check.py", "--base-url", "http://127.0.0.1:8090", "--mode", "readiness"],
    )

    assert http_readiness_check.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["mode"] == "readiness"
    assert calls == [
        "http://127.0.0.1:8090/api/system/readiness",
        "http://127.0.0.1:8090/api/auth/login",
        "http://127.0.0.1:8090/api/system/readiness",
    ]


def test_http_readiness_check_can_login_from_console_users_json(monkeypatch, capsys) -> None:
    monkeypatch.delenv("CONSOLE_ADMIN_USER", raising=False)
    monkeypatch.delenv("CONSOLE_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("CONSOLE_BASIC_USER", raising=False)
    monkeypatch.delenv("CONSOLE_BASIC_PASSWORD", raising=False)
    monkeypatch.setenv(
        "CONSOLE_USERS_JSON",
        json.dumps(
            {
                "users": [
                    {"username": "account1", "role": "account1", "password": "yx"},
                    {"username": "admin", "role": "admin", "password": "admin-json-secret"},
                ]
            }
        ),
    )

    def fake_urlopen(request, timeout):  # noqa: ANN001
        if request.full_url.endswith("/api/system/readiness") and not request.get_header("Cookie"):
            raise HTTPError(request.full_url, 401, "auth_required", {}, io.BytesIO())
        if request.full_url.endswith("/api/auth/login"):
            assert request.data is not None
            assert b"admin-json-secret" in request.data
            return FakeResponse({"ok": True}, cookie="aiq_session=json-session; Path=/")
        if request.full_url.endswith("/api/system/readiness"):
            assert request.get_header("Cookie") == "aiq_session=json-session; Path=/"
            return FakeResponse({"overall": "warn", "checks": []})
        raise AssertionError(request.full_url)

    monkeypatch.setattr(http_readiness_check, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        "sys.argv",
        ["http_readiness_check.py", "--base-url", "http://127.0.0.1:8090", "--mode", "readiness", "--allow-warn"],
    )

    assert http_readiness_check.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["overall"] == "warn"
