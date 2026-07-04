from __future__ import annotations

import json
from pathlib import Path

from scripts import record_release_run


def test_record_release_run_writes_release_audit(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AIQUANT_RELEASE_ID", "abc123")
    monkeypatch.setenv("AIQUANT_RELEASE_STATUS", "success")
    monkeypatch.setenv("AIQUANT_PREVIOUS_TARGET", "/srv/ai/releases/old")
    monkeypatch.setenv("AIQUANT_CURRENT_TARGET", "/srv/ai/releases/abc123")
    monkeypatch.setenv("AIQUANT_HEALTH_JSON", json.dumps({"ok": True, "mode": "health"}))
    monkeypatch.setenv(
        "AIQUANT_READINESS_JSON",
        json.dumps({"ok": True, "mode": "readiness", "overall": "ok", "blocking": []}),
    )

    assert record_release_run.main(["--remote-dir", str(tmp_path)]) == 0

    from ai_quant_trader.storage.sqlite import SQLiteStore

    store = SQLiteStore(str(tmp_path / "data" / "trader.sqlite3"), str(tmp_path / "logs" / "audit.jsonl"))
    try:
        row = store.fetch_latest("release_runs", "cloud_release")
    finally:
        store.close()

    assert row is not None
    payload = row["payload"]
    assert payload["release_id"] == "abc123"
    assert payload["status"] == "success"
    assert payload["health"]["ok"] is True
    assert payload["readiness"]["overall"] == "ok"
    assert payload["source"] == "cloud_release_deploy_v2"
