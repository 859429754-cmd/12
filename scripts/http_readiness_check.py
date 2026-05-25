from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def fetch_json(url: str, *, timeout: float, username: str | None = None, password: str | None = None) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if username and password:
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - explicit operator-provided local URL.
        body = response.read().decode("utf-8")
    return json.loads(body)


def main() -> int:
    parser = argparse.ArgumentParser(description="HTTP liveness/readiness check for systemd timers and watchdog scripts.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8090", help="Local console base URL.")
    parser.add_argument("--mode", choices=["health", "readiness"], default="readiness")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--allow-warn", action="store_true", help="Return success when readiness is warn instead of ok.")
    args = parser.parse_args()

    username = os.getenv("CONSOLE_BASIC_USER") or None
    password = os.getenv("CONSOLE_BASIC_PASSWORD") or None
    path = "/api/health" if args.mode == "health" else "/api/system/readiness"
    try:
        payload = fetch_json(args.base_url.rstrip("/") + path, timeout=args.timeout, username=username, password=password)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "mode": args.mode, "error_type": type(exc).__name__}, ensure_ascii=False))
        return 2

    if args.mode == "health":
        ok = payload.get("ok") is True
        print(json.dumps({"ok": ok, "mode": "health", "service": payload.get("service")}, ensure_ascii=False))
        return 0 if ok else 1

    overall = str(payload.get("overall") or "block")
    ok = overall == "ok" or (args.allow_warn and overall == "warn")
    blocking = [item.get("id") for item in payload.get("checks", []) if item.get("status") == "block"]
    print(json.dumps({"ok": ok, "mode": "readiness", "overall": overall, "blocking": blocking}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
