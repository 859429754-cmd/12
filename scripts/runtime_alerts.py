from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request


def _auth_header() -> dict[str, str]:
    user = _clean_env_secret(os.getenv("CONSOLE_ALERT_USER") or os.getenv("CONSOLE_BASIC_USER") or os.getenv("CONSOLE_ADMIN_USER"))
    password = _clean_env_secret(
        os.getenv("CONSOLE_ALERT_PASSWORD") or os.getenv("CONSOLE_BASIC_PASSWORD") or os.getenv("CONSOLE_ADMIN_PASSWORD")
    )
    if not user or not password:
        user, password = _auth_from_console_users_json()
    if not user or not password:
        return {}
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _auth_from_console_users_json() -> tuple[str | None, str | None]:
    raw = os.getenv("CONSOLE_USERS_JSON", "").strip()
    if not raw:
        return None, None
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return None, None
    users = loaded.get("users") if isinstance(loaded, dict) else loaded
    if not isinstance(users, list):
        return None, None
    candidates = [item for item in users if isinstance(item, dict)]
    candidates.sort(key=lambda item: 0 if str(item.get("role") or "").lower() == "admin" else 1)
    for item in candidates:
        username = _clean_env_secret(str(item.get("username") or ""))
        password = _clean_env_secret(str(item.get("password") or ""))
        if username and password:
            return username, password
    return None, None


def _clean_env_secret(value: str | None) -> str:
    cleaned = str(value or "").strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
        cleaned = cleaned[1:-1].strip()
    return cleaned


def _http_json(url: str, *, timeout: float, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Accept": "application/json", "Content-Type": "application/json", **_auth_header()}
    request = urllib.request.Request(url, data=data, headers=headers, method="GET" if payload is None else "POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch runtime alerts and optionally forward them to a webhook.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8090")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--fail-on-critical", action="store_true")
    args = parser.parse_args()

    try:
        body = _http_json(f"{args.base_url.rstrip('/')}/api/system/alerts", timeout=args.timeout)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": "alert_fetch_failed", "detail": str(exc)}, ensure_ascii=False))
        return 2 if args.fail_on_critical else 0

    webhook = os.getenv("AI_QUANT_ALERT_WEBHOOK_URL", "").strip()
    summary = body.get("summary") or {}
    if webhook and int(summary.get("total") or 0) > 0:
        try:
            _http_json(webhook, timeout=args.timeout, payload={"source": "ai_quant_trader", **body})
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            print(json.dumps({"ok": False, "error": "alert_webhook_failed", "detail": str(exc)}, ensure_ascii=False))
            return 2 if args.fail_on_critical else 0

    print(json.dumps({"ok": True, **body}, ensure_ascii=False))
    if args.fail_on_critical and int(summary.get("critical") or 0) > 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
