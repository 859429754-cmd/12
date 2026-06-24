from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
from dataclasses import dataclass, asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MaintenanceResult:
    checked_at: str
    sqlite_backup_path: str | None
    sqlite_backup_bytes: int
    sqlite_backup_integrity: str
    offsite_backup_path: str | None
    offsite_backup_bytes: int
    restore_drill_status: str
    restore_drill_path: str | None
    retained_restore_drills: list[str]
    pruned_restore_drills: list[str]
    rotated_logs: list[str]
    retained_backups: list[str]
    pruned_backups: list[str]
    disk_free_bytes: int | None
    disk_free_ratio: float | None
    disk_status: str
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def backup_sqlite(db_path: str | Path, backup_dir: str | Path) -> Path | None:
    source = Path(db_path)
    if not source.exists():
        return None
    output_dir = Path(backup_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    temp_path = output_dir / f"{source.stem}-{stamp}.sqlite3"
    gzip_path = temp_path.with_suffix(temp_path.suffix + ".gz")

    src = sqlite3.connect(source)
    dst = sqlite3.connect(temp_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    with temp_path.open("rb") as src_fh, gzip.open(gzip_path, "wb") as gz_fh:
        shutil.copyfileobj(src_fh, gz_fh)
    temp_path.unlink()
    return gzip_path


def verify_sqlite_backup(backup_path: str | Path) -> str:
    source = Path(backup_path)
    if not source.exists():
        return "missing"
    restore_path = source.with_suffix(source.suffix + ".verify.sqlite3")
    try:
        with gzip.open(source, "rb") as src_fh, restore_path.open("wb") as dst_fh:
            shutil.copyfileobj(src_fh, dst_fh)
        conn = sqlite3.connect(restore_path)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
        return "ok" if result and result[0] == "ok" else "failed"
    except (OSError, sqlite3.DatabaseError, gzip.BadGzipFile):
        return "failed"
    finally:
        if restore_path.exists():
            restore_path.unlink()


def copy_offsite_backup(backup_path: str | Path, offsite_backup_dir: str | Path) -> Path:
    source = Path(backup_path)
    if not source.exists():
        raise FileNotFoundError(source)
    target_dir = Path(offsite_backup_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    shutil.copy2(source, target)
    return target


def run_restore_drill(backup_path: str | Path, restore_dir: str | Path) -> tuple[str, Path | None]:
    source = Path(backup_path)
    if not source.exists():
        return "missing", None
    target_dir = Path(restore_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    restored_path = target_dir / f"{source.stem}.{stamp}.restore.sqlite3"
    try:
        with gzip.open(source, "rb") as src_fh, restored_path.open("wb") as dst_fh:
            shutil.copyfileobj(src_fh, dst_fh)
        conn = sqlite3.connect(restored_path)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
        status = "ok" if result and result[0] == "ok" else "failed"
        return status, restored_path if status == "ok" else None
    except (OSError, sqlite3.DatabaseError, gzip.BadGzipFile):
        if restored_path.exists():
            restored_path.unlink()
        return "failed", None


def rotate_text_log(log_path: str | Path, *, max_bytes: int, keep: int = 5) -> list[Path]:
    path = Path(log_path)
    if max_bytes <= 0:
        raise ValueError("max_bytes_must_be_positive")
    if keep < 1:
        raise ValueError("keep_must_be_positive")
    if not path.exists() or path.stat().st_size <= max_bytes:
        return []

    rotated: list[Path] = []
    oldest = path.with_name(f"{path.name}.{keep}")
    if oldest.exists():
        oldest.unlink()
    for idx in range(keep - 1, 0, -1):
        current = path.with_name(f"{path.name}.{idx}")
        target = path.with_name(f"{path.name}.{idx + 1}")
        if current.exists():
            current.replace(target)
            rotated.append(target)
    first = path.with_name(f"{path.name}.1")
    path.replace(first)
    path.touch()
    rotated.append(first)
    return rotated


def prune_backups(
    backup_dir: str | Path,
    *,
    keep: int = 24,
    pattern: str = "*.sqlite3.gz",
) -> tuple[list[Path], list[Path]]:
    if keep < 1:
        raise ValueError("backup_keep_must_be_positive")
    directory = Path(backup_dir)
    if not directory.exists():
        return [], []
    backups = sorted(
        [path for path in directory.glob(pattern) if path.is_file() and path.parent.resolve() == directory.resolve()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    retained = backups[:keep]
    pruned = backups[keep:]
    for path in pruned:
        path.unlink()
    return retained, pruned


def disk_space_status(path: str | Path, *, min_free_bytes: int, min_free_ratio: float) -> tuple[str, int | None, float | None]:
    target = Path(path)
    probe = target if target.exists() else target.parent
    if not probe.exists():
        return "warn", None, None
    usage = shutil.disk_usage(probe)
    free_ratio = usage.free / usage.total if usage.total else 0.0
    status = "ok" if usage.free >= min_free_bytes and free_ratio >= min_free_ratio else "block"
    return status, int(usage.free), float(free_ratio)


def run_runtime_maintenance(
    *,
    database_path: str | Path,
    audit_log_path: str | Path,
    backup_dir: str | Path = "data/backups",
    offsite_backup_dir: str | Path | None = None,
    restore_drill_dir: str | Path | None = None,
    log_paths: list[str | Path] | None = None,
    max_log_bytes: int = 10 * 1024 * 1024,
    keep: int = 5,
    backup_keep: int = 24,
    restore_drill_keep: int = 3,
    min_free_bytes: int = 1_000_000_000,
    min_free_ratio: float = 0.10,
) -> MaintenanceResult:
    warnings: list[str] = []
    disk_status, disk_free_bytes, disk_free_ratio = disk_space_status(
        backup_dir,
        min_free_bytes=min_free_bytes,
        min_free_ratio=min_free_ratio,
    )
    if disk_status != "ok":
        warnings.append("disk_space_low")
    backup_path = backup_sqlite(database_path, backup_dir)
    backup_integrity = "not_run"
    offsite_backup_path: Path | None = None
    restore_drill_status = "not_run"
    restore_drill_path: Path | None = None
    if backup_path is None:
        warnings.append("sqlite_database_missing")
        backup_integrity = "missing"
    else:
        backup_integrity = verify_sqlite_backup(backup_path)
        if backup_integrity != "ok":
            warnings.append(f"sqlite_backup_integrity:{backup_integrity}")
        if offsite_backup_dir:
            try:
                offsite_backup_path = copy_offsite_backup(backup_path, offsite_backup_dir)
            except OSError:
                warnings.append("offsite_backup_copy_failed")
        if restore_drill_dir:
            restore_drill_status, restore_drill_path = run_restore_drill(backup_path, restore_drill_dir)
            if restore_drill_status != "ok":
                warnings.append(f"sqlite_restore_drill:{restore_drill_status}")
    retained_restore_drills: list[Path] = []
    pruned_restore_drills: list[Path] = []
    if restore_drill_dir:
        retained_restore_drills, pruned_restore_drills = prune_backups(
            restore_drill_dir,
            keep=restore_drill_keep,
            pattern="*.restore.sqlite3",
        )

    rotated: list[str] = []
    for item in [audit_log_path, *(log_paths or [])]:
        try:
            rotated.extend(str(path) for path in rotate_text_log(item, max_bytes=max_log_bytes, keep=keep))
        except FileNotFoundError:
            warnings.append(f"log_missing:{Path(item).name}")
    retained, pruned = prune_backups(backup_dir, keep=backup_keep)

    return MaintenanceResult(
        checked_at=datetime.now(UTC).isoformat(),
        sqlite_backup_path=str(backup_path) if backup_path else None,
        sqlite_backup_bytes=backup_path.stat().st_size if backup_path else 0,
        sqlite_backup_integrity=backup_integrity,
        offsite_backup_path=str(offsite_backup_path) if offsite_backup_path else None,
        offsite_backup_bytes=offsite_backup_path.stat().st_size if offsite_backup_path else 0,
        restore_drill_status=restore_drill_status,
        restore_drill_path=str(restore_drill_path) if restore_drill_path else None,
        retained_restore_drills=[str(path) for path in retained_restore_drills],
        pruned_restore_drills=[str(path) for path in pruned_restore_drills],
        rotated_logs=rotated,
        retained_backups=[str(path) for path in retained],
        pruned_backups=[str(path) for path in pruned],
        disk_free_bytes=disk_free_bytes,
        disk_free_ratio=disk_free_ratio,
        disk_status=disk_status,
        warnings=warnings,
    )


def result_to_json(result: MaintenanceResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
