from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ai_quant_trader.core.models import AiConfig
from ai_quant_trader.storage.sqlite import SQLiteStore


TERMINAL_ATTEMPT_STATUSES = {"attempt", "success", "failure"}


@dataclass(frozen=True)
class DeepSeekBudgetReservation:
    allowed: bool
    reason: str
    row_id: int | None = None


class DeepSeekBudgetGuard:
    """Persistent guardrail for DeepSeek usage.

    This is deliberately outside DeepSeekBrain. The brain is the API adapter; this
    guard owns runtime budget policy, event dedupe, and failure cooldown.
    """

    table = "ai_call_budget_events"

    def __init__(
        self,
        store: SQLiteStore,
        *,
        enabled: bool = True,
        max_calls_per_hour: int = 8,
        max_calls_per_day: int = 60,
        max_major_news_reviews_per_hour: int = 3,
        max_major_news_reviews_per_day: int = 24,
        event_dedupe_hours: int = 48,
        failure_cooldown_minutes: int = 20,
    ) -> None:
        self.store = store
        self.enabled = enabled
        self.max_calls_per_hour = max_calls_per_hour
        self.max_calls_per_day = max_calls_per_day
        self.max_major_news_reviews_per_hour = max_major_news_reviews_per_hour
        self.max_major_news_reviews_per_day = max_major_news_reviews_per_day
        self.event_dedupe_hours = event_dedupe_hours
        self.failure_cooldown_minutes = failure_cooldown_minutes

    @classmethod
    def from_config(cls, store: SQLiteStore, config: AiConfig) -> "DeepSeekBudgetGuard":
        return cls(
            store,
            enabled=config.call_budget_enabled,
            max_calls_per_hour=config.max_calls_per_hour,
            max_calls_per_day=config.max_calls_per_day,
            max_major_news_reviews_per_hour=config.max_major_news_reviews_per_hour,
            max_major_news_reviews_per_day=config.max_major_news_reviews_per_day,
            event_dedupe_hours=config.event_dedupe_hours,
            failure_cooldown_minutes=config.failure_cooldown_minutes,
        )

    def reserve(self, *, symbol: str, call_type: str, event_key: str | None = None) -> DeepSeekBudgetReservation:
        now = datetime.now(UTC)
        payload = {
            "symbol": symbol,
            "call_type": call_type,
            "event_key": event_key,
            "status": "blocked",
            "reason": "",
            "checked_at": now.isoformat(),
        }
        if not self.enabled:
            payload.update({"status": "attempt", "reason": "budget_disabled"})
            row_id = self.store.insert(self.table, payload, symbol)
            return DeepSeekBudgetReservation(True, "budget_disabled", row_id)

        block_reason = self._blocking_reason(symbol=symbol, call_type=call_type, event_key=event_key, now=now)
        if block_reason is not None:
            payload["reason"] = block_reason
            self.store.insert(self.table, payload, symbol)
            return DeepSeekBudgetReservation(False, block_reason, None)

        payload.update({"status": "attempt", "reason": "reserved"})
        row_id = self.store.insert(self.table, payload, symbol)
        return DeepSeekBudgetReservation(True, "reserved", row_id)

    def record_success(self, row_id: int | None, *, detail: str = "ok") -> None:
        if row_id is None:
            return
        row = self.store.fetch_by_id(self.table, row_id)
        if not row:
            return
        payload = dict(row["payload"])
        payload.update({"status": "success", "reason": detail, "finished_at": datetime.now(UTC).isoformat()})
        self.store.update_payload(self.table, row_id, payload)

    def record_failure(self, row_id: int | None, *, reason: str, error_type: str | None = None) -> None:
        if row_id is None:
            return
        row = self.store.fetch_by_id(self.table, row_id)
        if not row:
            return
        payload = dict(row["payload"])
        payload.update(
            {
                "status": "failure",
                "reason": reason,
                "error_type": error_type,
                "finished_at": datetime.now(UTC).isoformat(),
            }
        )
        self.store.update_payload(self.table, row_id, payload)

    def _blocking_reason(self, *, symbol: str, call_type: str, event_key: str | None, now: datetime) -> str | None:
        if event_key:
            duplicate = self._find_event_key(
                symbol=symbol,
                call_type=call_type,
                event_key=event_key,
                since=now - timedelta(hours=self.event_dedupe_hours),
            )
            if duplicate:
                return "duplicate_event_key"

        if self.failure_cooldown_minutes > 0:
            recent_failure = self._latest_failure(since=now - timedelta(minutes=self.failure_cooldown_minutes))
            if recent_failure is not None:
                return "failure_cooldown_active"

        if self._attempt_count(since=now - timedelta(hours=1)) >= self.max_calls_per_hour:
            return "hourly_limit_exceeded"
        if self._attempt_count(since=now - timedelta(days=1)) >= self.max_calls_per_day:
            return "daily_limit_exceeded"
        if call_type == "major_news_risk_review":
            if self._attempt_count(since=now - timedelta(hours=1), call_type=call_type) >= self.max_major_news_reviews_per_hour:
                return "major_news_hourly_limit_exceeded"
            if self._attempt_count(since=now - timedelta(days=1), call_type=call_type) >= self.max_major_news_reviews_per_day:
                return "major_news_daily_limit_exceeded"
        return None

    def _attempt_count(self, *, since: datetime, call_type: str | None = None) -> int:
        count = 0
        for row in self.store.fetch_payloads(self.table, limit=5000):
            if not self._row_after(row, since):
                continue
            payload = row.get("payload") or {}
            if call_type and payload.get("call_type") != call_type:
                continue
            if payload.get("status") in TERMINAL_ATTEMPT_STATUSES:
                count += 1
        return count

    def _latest_failure(self, *, since: datetime) -> dict[str, Any] | None:
        for row in self.store.fetch_payloads(self.table, limit=5000):
            if not self._row_after(row, since):
                continue
            payload = row.get("payload") or {}
            if payload.get("status") == "failure":
                return row
        return None

    def _find_event_key(self, *, symbol: str, call_type: str, event_key: str, since: datetime) -> dict[str, Any] | None:
        for row in self.store.fetch_payloads(self.table, limit=5000, symbol=symbol):
            if not self._row_after(row, since):
                continue
            payload = row.get("payload") or {}
            if payload.get("call_type") == call_type and payload.get("event_key") == event_key:
                if payload.get("status") != "blocked":
                    return row
        return None

    def _row_after(self, row: dict[str, Any], since: datetime) -> bool:
        created_at = row.get("created_at")
        if not created_at:
            return False
        try:
            created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        except ValueError:
            try:
                created = datetime.strptime(str(created_at), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            except ValueError:
                return False
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return created >= since
