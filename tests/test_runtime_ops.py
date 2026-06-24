from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path

from ai_quant_trader.core.models import HealthStatus
from ai_quant_trader.monitoring.heartbeat import WorkerHeartbeatRecorder
from ai_quant_trader.ops.maintenance import (
    backup_sqlite,
    copy_offsite_backup,
    disk_space_status,
    prune_backups,
    rotate_text_log,
    run_runtime_maintenance,
    run_restore_drill,
    verify_sqlite_backup,
)
from ai_quant_trader.storage.sqlite import SQLiteStore


def test_worker_heartbeat_records_success_and_failure(tmp_path: Path) -> None:
    store = SQLiteStore(str(tmp_path / "trader.sqlite3"), str(tmp_path / "audit.jsonl"))
    recorder = WorkerHeartbeatRecorder(store)

    ok = recorder.ok("order_status_worker", reason="poll_ok", interval_seconds=60)
    failed = recorder.fail("order_status_worker", reason="poll_failed", status=HealthStatus.BLOCK)
    latest = store.fetch_latest("worker_heartbeats", "order_status_worker")

    assert ok.status == HealthStatus.OK
    assert failed.status == HealthStatus.BLOCK
    assert latest is not None
    assert latest["payload"]["reason"] == "poll_failed"
    assert latest["payload"]["last_success_at"] is not None
    store.close()


def test_sqlite_backup_uses_consistent_gzip_copy(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO sample (value) VALUES ('ok')")

    backup = backup_sqlite(db_path, tmp_path / "backups")

    assert backup is not None
    assert backup.suffix == ".gz"
    with gzip.open(backup, "rb") as fh:
        assert fh.read(16).startswith(b"SQLite format 3")
    assert verify_sqlite_backup(backup) == "ok"


def test_offsite_backup_copy_and_restore_drill(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO sample (value) VALUES ('ok')")

    backup = backup_sqlite(db_path, tmp_path / "backups")
    assert backup is not None

    offsite = copy_offsite_backup(backup, tmp_path / "offsite")
    status, restored = run_restore_drill(offsite, tmp_path / "restore-drills")

    assert offsite.exists()
    assert offsite.stat().st_size == backup.stat().st_size
    assert status == "ok"
    assert restored is not None and restored.exists()


def test_log_rotation_and_runtime_maintenance(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    audit_log = tmp_path / "audit.jsonl"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
    audit_log.write_text("x" * 128, encoding="utf-8")

    rotated = rotate_text_log(audit_log, max_bytes=32, keep=2)
    result = run_runtime_maintenance(
        database_path=db_path,
        audit_log_path=audit_log,
        backup_dir=tmp_path / "backups",
        offsite_backup_dir=tmp_path / "offsite",
        restore_drill_dir=tmp_path / "restore-drills",
        max_log_bytes=32,
        keep=2,
    )

    assert [path.name for path in rotated] == ["audit.jsonl.1"]
    assert audit_log.exists()
    assert result.sqlite_backup_path is not None
    assert result.sqlite_backup_bytes > 0
    assert result.sqlite_backup_integrity == "ok"
    assert result.offsite_backup_path is not None
    assert result.offsite_backup_bytes > 0
    assert result.restore_drill_status == "ok"
    assert result.restore_drill_path is not None
    assert result.disk_status in {"ok", "block"}
    assert result.retained_backups


def test_sqlite_backup_integrity_flags_corruption(tmp_path: Path) -> None:
    corrupt = tmp_path / "broken.sqlite3.gz"
    corrupt.write_bytes(b"not-a-gzip")

    assert verify_sqlite_backup(corrupt) == "failed"


def test_backup_retention_prunes_oldest_files(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for idx in range(4):
        item = backup_dir / f"runtime-20260526T00000{idx}.sqlite3.gz"
        item.write_text(str(idx), encoding="utf-8")

    retained, pruned = prune_backups(backup_dir, keep=2)

    assert len(retained) == 2
    assert len(pruned) == 2
    assert all(not path.exists() for path in pruned)


def test_disk_space_status_blocks_unrealistic_floor(tmp_path: Path) -> None:
    status, free_bytes, free_ratio = disk_space_status(
        tmp_path,
        min_free_bytes=10**30,
        min_free_ratio=0.99,
    )

    assert status == "block"
    assert free_bytes is not None
    assert free_ratio is not None
