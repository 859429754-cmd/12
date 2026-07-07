from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, timezone
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
        avoid_peak_pricing: bool = False,
        peak_pricing_timezone_offset_hours: int = 8,
        peak_pricing_windows: list[str] | None = None,
        peak_pricing_blocked_call_types: list[str] | None = None,
    ) -> None:
        self.store = store
        self.enabled = enabled
        self.max_calls_per_hour = max_calls_per_hour
        self.max_calls_per_day = max_calls_per_day
        self.max_major_news_reviews_per_hour = max_major_news_reviews_per_hour
        self.max_major_news_reviews_per_day = max_major_news_reviews_per_day
        self.event_dedupe_hours = event_dedupe_hours
        self.failure_cooldown_minutes = failure_cooldown_minutes
        self.avoid_peak_pricing = avoid_peak_pricing
        self.peak_pricing_timezone_offset_hours = peak_pricing_timezone_offset_hours
        self.peak_pricing_windows = peak_pricing_windows or ["09:00-12:00", "14:00-18:00"]
        self.peak_pricing_blocked_call_types = set(
            peak_pricing_blocked_call_types or ["major_news_risk_review", "price_wakeup", "optimization_proposal"]
        )

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
            avoid_peak_pricing=config.avoid_peak_pricing,
            peak_pricing_timezone_offset_hours=config.peak_pricing_timezone_offset_hours,
            peak_pricing_windows=config.peak_pricing_windows,
            peak_pricing_blocked_call_types=config.peak_pricing_blocked_call_types,
        )

    def reserve(
        self,
        *,
        symbol: str,
        call_type: str,
        event_key: str | None = None,
        now: datetime | None = None,
    ) -> DeepSeekBudgetReservation:
        now = now or datetime.now(UTC)
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

    def record_skipped(self, *, symbol: str, call_type: str, reason: str, event_key: str | None = None) -> int:
        payload = {
            "symbol": symbol,
            "call_type": call_type,
            "event_key": event_key,
            "status": "skipped",
            "reason": reason,
            "checked_at": datetime.now(UTC).isoformat(),
        }
        return self.store.insert(self.table, payload, symbol)

    def _blocking_reason(self, *, symbol: str, call_type: str, event_key: str | None, now: datetime) -> str | None:
        if self._peak_pricing_blocks(call_type=call_type, now=now):
            return "peak_pricing_window_active"

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

    def _peak_pricing_blocks(self, *, call_type: str, now: datetime) -> bool:
        if not self.avoid_peak_pricing:
            return False
        if call_type not in self.peak_pricing_blocked_call_types:
            return False
        local_now = now.astimezone(timezone(timedelta(hours=self.peak_pricing_timezone_offset_hours)))
        current = local_now.time()
        for window in self.peak_pricing_windows:
            parsed = self._parse_window(window)
            if parsed is None:
                continue
            start, end = parsed
            if start <= end:
                if start <= current < end:
                    return True
            elif current >= start or current < end:
                return True
        return False

    def _parse_window(self, window: str) -> tuple[time, time] | None:
        try:
            start_text, end_text = [part.strip() for part in window.split("-", 1)]
            start_hour, start_minute = [int(part) for part in start_text.split(":", 1)]
            end_hour, end_minute = [int(part) for part in end_text.split(":", 1)]
            return time(start_hour, start_minute), time(end_hour, end_minute)
        except (ValueError, TypeError):
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
