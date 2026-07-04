from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[1]


def _json_env(name: str) -> dict[str, Any]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw, "parse_error": "invalid_json"}
    return value if isinstance(value, dict) else {"value": value}


def _build_payload() -> dict[str, Any]:
    return {
        "release_id": os.getenv("AIQUANT_RELEASE_ID", ""),
        "status": os.getenv("AIQUANT_RELEASE_STATUS", "success"),
        "previous_target": os.getenv("AIQUANT_PREVIOUS_TARGET", ""),
        "current_target": os.getenv("AIQUANT_CURRENT_TARGET", ""),
        "health": _json_env("AIQUANT_HEALTH_JSON"),
        "readiness": _json_env("AIQUANT_READINESS_JSON"),
        "source": "cloud_release_deploy_v2",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def record_release_run(remote_dir: Path) -> int:
    sys.path.insert(0, str(_repo_root_from_script()))

    from ai_quant_trader.storage.sqlite import SQLiteStore

    store = SQLiteStore(str(remote_dir / "data" / "trader.sqlite3"), str(remote_dir / "logs" / "audit.jsonl"))
    try:
        return store.insert("release_runs", _build_payload(), symbol="cloud_release")
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record a cloud release health/readiness result in SQLite audit storage.")
    parser.add_argument("--remote-dir", default=".", help="Runtime root containing data/ and logs/.")
    args = parser.parse_args(argv)

    row_id = record_release_run(Path(args.remote_dir).resolve())
    print(json.dumps({"ok": True, "row_id": row_id, "release_id": os.getenv("AIQUANT_RELEASE_ID", "")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
