from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_quant_trader.storage.sqlite import SQLiteStore


STATUS_VALUE = {"ok": 0, "warn": 1, "block": 2, "unknown": 1}


def collect_runtime_metrics(
    *,
    store: SQLiteStore,
    database_path: str,
    audit_log_path: str,
    readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checks = (readiness or {}).get("checks") or []
    check_counts = {"ok": 0, "warn": 0, "block": 0}
    for check in checks:
        status = str(check.get("status") or "warn")
        if status in check_counts:
            check_counts[status] += 1

    worker_rows = (readiness or {}).get("latest_worker_heartbeats") or {}
    worker_metrics = {}
    for worker, row in worker_rows.items():
        payload = (row or {}).get("payload") or {}
        status = str(payload.get("status") or "warn")
        worker_metrics[worker] = {
            "status": status,
            "status_value": STATUS_VALUE.get(status, 1),
            "age_seconds": _row_age_seconds(row),
            "reason": payload.get("reason") or "",
        }

    latest_data_health = (readiness or {}).get("latest_data_health") or store.fetch_latest("data_health")
    latest_ai_drift = (readiness or {}).get("latest_ai_drift") or store.fetch_latest("ai_drift_checks")
    latest_exchange = (readiness or {}).get("exchange_safety") or store.fetch_latest("exchange_health")
    latest_maintenance = store.fetch_latest("maintenance_runs")

    metrics = {
        "checked_at": datetime.now(UTC).isoformat(),
        "readiness": {
            "overall": (readiness or {}).get("overall") or "unknown",
            "overall_value": STATUS_VALUE.get(str((readiness or {}).get("overall") or "unknown"), 1),
            "checks": check_counts,
        },
        "worker_heartbeats": worker_metrics,
        "exchange": _payload_status_metric(latest_exchange, default="unknown"),
        "data_health": _payload_status_metric(latest_data_health, default="warn"),
        "ai_drift": _payload_status_metric(latest_ai_drift, default="warn"),
        "storage": {
            "database_bytes": _path_size(database_path),
            "audit_log_bytes": _path_size(audit_log_path),
        },
        "maintenance": {
            "latest": latest_maintenance,
            "age_seconds": _row_age_seconds(latest_maintenance),
        },
    }
    store.insert("runtime_metrics", metrics)
    return metrics


def metrics_to_prometheus(metrics: dict[str, Any]) -> str:
    lines = [
        "# HELP ai_quant_readiness_status Overall readiness status: ok=0 warn=1 block=2.",
        "# TYPE ai_quant_readiness_status gauge",
        f"ai_quant_readiness_status {metrics['readiness']['overall_value']}",
        "# HELP ai_quant_readiness_checks Readiness check counts by status.",
        "# TYPE ai_quant_readiness_checks gauge",
    ]
    for status, count in metrics["readiness"]["checks"].items():
        lines.append(f'ai_quant_readiness_checks{{status="{status}"}} {count}')
    lines.extend(
        [
            "# HELP ai_quant_worker_status Worker heartbeat status: ok=0 warn=1 block=2.",
            "# TYPE ai_quant_worker_status gauge",
        ]
    )
    for worker, item in metrics["worker_heartbeats"].items():
        lines.append(f'ai_quant_worker_status{{worker="{worker}"}} {item["status_value"]}')
        if item.get("age_seconds") is not None:
            lines.append(f'ai_quant_worker_age_seconds{{worker="{worker}"}} {item["age_seconds"]:.3f}')
    lines.extend(
        [
            "# HELP ai_quant_storage_bytes Runtime storage file sizes.",
            "# TYPE ai_quant_storage_bytes gauge",
            f'ai_quant_storage_bytes{{file="sqlite"}} {metrics["storage"]["database_bytes"] or 0}',
            f'ai_quant_storage_bytes{{file="audit_log"}} {metrics["storage"]["audit_log_bytes"] or 0}',
        ]
    )
    latest_maintenance = (metrics.get("maintenance") or {}).get("latest") or {}
    maintenance_payload = latest_maintenance.get("payload") or {}
    integrity_ok = 1 if maintenance_payload.get("sqlite_backup_integrity") == "ok" else 0
    disk_ok = 1 if maintenance_payload.get("disk_status") == "ok" else 0
    lines.extend(
        [
            "# HELP ai_quant_maintenance_ok Maintenance sub-checks: 1=ok, 0=not ok.",
            "# TYPE ai_quant_maintenance_ok gauge",
            f'ai_quant_maintenance_ok{{check="sqlite_backup_integrity"}} {integrity_ok}',
            f'ai_quant_maintenance_ok{{check="disk_space"}} {disk_ok}',
        ]
    )
    return "\n".join(lines) + "\n"


def _payload_status_metric(row: dict[str, Any] | None, *, default: str) -> dict[str, Any]:
    payload = (row or {}).get("payload") or {}
    status = str(payload.get("status") or default)
    return {
        "status": status,
        "status_value": STATUS_VALUE.get(status, 1),
        "age_seconds": _row_age_seconds(row),
        "reason": payload.get("reason") or "",
    }


def _row_age_seconds(row: dict[str, Any] | None) -> float | None:
    if not row:
        return None
    raw = str(((row.get("payload") or {}).get("checked_at")) or row.get("created_at") or "")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max((datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds(), 0.0)


def _path_size(path: str) -> int | None:
    item = Path(path)
    return item.stat().st_size if item.exists() else None
