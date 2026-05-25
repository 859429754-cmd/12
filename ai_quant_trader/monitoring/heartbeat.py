from __future__ import annotations

from typing import Any

from ai_quant_trader.core.models import HealthStatus, WorkerHeartbeat, utc_now
from ai_quant_trader.storage.sqlite import SQLiteStore


class WorkerHeartbeatRecorder:
    """Persist worker liveness as an auditable readiness input."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def ok(
        self,
        worker: str,
        *,
        reason: str = "worker_ok",
        interval_seconds: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> WorkerHeartbeat:
        now = utc_now()
        heartbeat = WorkerHeartbeat(
            worker=worker,
            status=HealthStatus.OK,
            reason=reason,
            interval_seconds=interval_seconds,
            details=details or {},
            last_success_at=now,
            checked_at=now,
        )
        self.store.insert("worker_heartbeats", heartbeat, worker)
        return heartbeat

    def fail(
        self,
        worker: str,
        *,
        reason: str,
        status: HealthStatus = HealthStatus.BLOCK,
        interval_seconds: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> WorkerHeartbeat:
        now = utc_now()
        latest = self.store.fetch_latest("worker_heartbeats", worker)
        latest_payload = (latest or {}).get("payload") or {}
        last_success_at = latest_payload.get("last_success_at")
        heartbeat = WorkerHeartbeat(
            worker=worker,
            status=status,
            reason=reason,
            interval_seconds=interval_seconds,
            details=details or {},
            last_success_at=last_success_at,
            checked_at=now,
        )
        self.store.insert("worker_heartbeats", heartbeat, worker)
        return heartbeat
