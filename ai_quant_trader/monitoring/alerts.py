from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal


AlertLevel = Literal["info", "warn", "critical"]


def build_runtime_alerts(readiness: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert readiness state into operator alerts.

    This layer is intentionally deterministic. It does not decide trades; it
    names runtime states that require operator attention in live mode.
    """

    alerts: list[dict[str, Any]] = []
    execution_mode = str(readiness.get("execution_mode") or "unknown")
    checks = {str(item.get("id")): item for item in readiness.get("checks") or [] if isinstance(item, dict)}

    def add(event: str, level: AlertLevel, message: str, *, source: str, payload: dict[str, Any] | None = None) -> None:
        alerts.append(
            {
                "event": event,
                "level": level,
                "source": source,
                "message": message,
                "execution_mode": execution_mode,
                "created_at": datetime.now(UTC).isoformat(),
                "payload": payload or {},
            }
        )

    _add_blocking_check_alert(add, checks, "exchange_safety", "gate_position_read_failed")
    _add_blocking_check_alert(add, checks, "reconciliation", "exchange_reconciliation_failed")
    _add_blocking_check_alert(add, checks, "order_lifecycle", "order_lifecycle_problem")
    _add_blocking_check_alert(add, checks, "runtime_maintenance", "runtime_maintenance_failed")
    _add_blocking_check_alert(add, checks, "worker_heartbeat", "worker_heartbeat_failed")

    latest_order = _payload(readiness.get("latest_order_lifecycle"))
    order_status = str(latest_order.get("status") or latest_order.get("state") or "").lower()
    order_type = str(latest_order.get("order_type") or latest_order.get("kind") or "").lower()
    if order_status in {"unknown", "submit_unknown", "cancel_failed"}:
        add(
            "order_status_unknown",
            "critical" if execution_mode == "live" else "warn",
            "Latest order lifecycle is unknown; new live entries must remain blocked until reconciliation.",
            source="order_lifecycle",
            payload=latest_order,
        )
    if order_type in {"stop_loss", "native_stop", "stop"} and order_status in {"unknown", "rejected", "failed", "cancel_failed"}:
        add(
            "native_stop_failure",
            "critical" if execution_mode == "live" else "warn",
            "Native stop-loss lifecycle indicates failure or unknown state.",
            source="order_lifecycle",
            payload=latest_order,
        )

    latest_news = checks.get("news") or {}
    if str(latest_news.get("status")) in {"warn", "block"}:
        add(
            "news_stale",
            "critical" if execution_mode == "live" and str(latest_news.get("status")) == "block" else "warn",
            str(latest_news.get("detail") or "News cache is stale or missing."),
            source="news",
            payload=latest_news,
        )

    deepseek_check = checks.get("deepseek") or {}
    budget_check = checks.get("deepseek_budget") or {}
    if str(deepseek_check.get("status")) == "block" or str(budget_check.get("status")) == "block":
        add(
            "deepseek_all_failed_or_blocked",
            "critical" if execution_mode == "live" else "warn",
            "AI provider fallback is not healthy enough for live entries.",
            source="deepseek",
            payload={"deepseek": deepseek_check, "budget": budget_check},
        )

    latest_exchange = _payload(readiness.get("exchange_safety"))
    reason = str(latest_exchange.get("reason") or "").lower()
    if "balance" in reason or "equity" in reason:
        add(
            "balance_read_failed",
            "critical" if execution_mode == "live" else "warn",
            "Exchange balance or equity read failed.",
            source="exchange_safety",
            payload=latest_exchange,
        )

    latest_maintenance = _payload(readiness.get("latest_maintenance"))
    if latest_maintenance.get("disk_status") == "block":
        add(
            "disk_space_low",
            "critical",
            "Disk free space is below the configured production floor.",
            source="runtime_maintenance",
            payload=latest_maintenance,
        )
    if latest_maintenance.get("sqlite_backup_integrity") not in {None, "ok"}:
        add(
            "backup_integrity_failed",
            "critical",
            "Latest SQLite backup failed integrity verification.",
            source="runtime_maintenance",
            payload=latest_maintenance,
        )
    if latest_maintenance.get("restore_drill_status") not in {None, "not_run", "ok"}:
        add(
            "restore_drill_failed",
            "critical",
            "Latest SQLite restore drill failed.",
            source="runtime_maintenance",
            payload=latest_maintenance,
        )

    return alerts


def alert_summary(alerts: list[dict[str, Any]]) -> dict[str, Any]:
    critical = sum(1 for item in alerts if item.get("level") == "critical")
    warn = sum(1 for item in alerts if item.get("level") == "warn")
    return {
        "total": len(alerts),
        "critical": critical,
        "warn": warn,
        "status": "block" if critical else ("warn" if warn else "ok"),
    }


def _payload(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        payload = row.get("payload")
        if isinstance(payload, dict):
            return payload
        return row
    return {}


def _add_blocking_check_alert(add, checks: dict[str, dict[str, Any]], check_id: str, event: str) -> None:
    check = checks.get(check_id) or {}
    status = str(check.get("status") or "")
    if status not in {"warn", "block"}:
        return
    add(
        event,
        "critical" if status == "block" else "warn",
        str(check.get("detail") or check.get("name") or event),
        source=check_id,
        payload=check,
    )
