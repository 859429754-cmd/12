from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _load_runtime_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _post(path: str, payload: dict) -> dict:
    user = os.getenv("CONSOLE_BASIC_USER", "")
    password = os.getenv("CONSOLE_BASIC_PASSWORD", "")
    response = requests.post(
        f"http://127.0.0.1:8090{path}",
        json=payload,
        auth=(user, password),
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def _get(path: str) -> dict:
    user = os.getenv("CONSOLE_BASIC_USER", "")
    password = os.getenv("CONSOLE_BASIC_PASSWORD", "")
    response = requests.get(f"http://127.0.0.1:8090{path}", auth=(user, password), timeout=20)
    response.raise_for_status()
    return response.json()


def main() -> None:
    _load_runtime_env(Path(".env.runtime"))
    trade_pin = os.getenv("TRADE_PIN", "")
    if not trade_pin:
        raise SystemExit("TRADE_PIN missing")
    if not os.getenv("CONSOLE_BASIC_USER") or not os.getenv("CONSOLE_BASIC_PASSWORD"):
        raise SystemExit("console auth missing")
    outputs = {
        "runtime_mode": _post("/api/control/runtime-mode", {"operator_id": "codex", "dry_run": False, "trade_pin": trade_pin}),
        "enable_report": _post("/api/control/enable-report", {"operator_id": "codex", "symbols": ["ETH/USDT:USDT"]}),
        "authorize": _post("/api/control/authorize", {"operator_id": "codex", "symbols": ["ETH/USDT:USDT"]}),
        "status": _get("/api/status"),
    }
    status = outputs["status"]
    print(
        json.dumps(
            {
                "ok": True,
                "execution_mode": status.get("execution_mode"),
                "dry_run": status.get("dry_run"),
                "opening_paused": status.get("opening_paused"),
                "enabled_symbols": status.get("enabled_symbols"),
                "report_symbols": status.get("report_symbols"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
