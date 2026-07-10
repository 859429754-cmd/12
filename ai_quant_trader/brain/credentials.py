from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ai_quant_trader.storage.sqlite import SQLiteStore


QUOTA_ERROR_STATUSES = {402}
AUTH_ERROR_STATUSES = {401, 403}
RATE_LIMIT_STATUSES = {429}
TRANSIENT_STATUSES = {408, 500, 502, 503, 504}


@dataclass(frozen=True)
class CredentialFailure:
    category: str
    http_status: int | None
    error_type: str
    message: str


class DeepSeekCredentialRouter:
    """Persistent DeepSeek key routing without storing secret values."""

    state_symbol = "deepseek_credentials"

    def __init__(
        self,
        store: SQLiteStore | None = None,
        *,
        quota_probe_after_hours: int = 24,
        rate_limit_cooldown_minutes: int = 15,
        transient_cooldown_minutes: int = 3,
    ) -> None:
        self.store = store
        self.quota_probe_after_hours = quota_probe_after_hours
        self.rate_limit_cooldown_minutes = rate_limit_cooldown_minutes
        self.transient_cooldown_minutes = transient_cooldown_minutes
        self._memory_state: dict[str, Any] = self._default_state()

    def candidates(self, keys: list[tuple[str, str]]) -> list[tuple[str, str]]:
        if not keys:
            return []
        available = {label: key for label, key in keys if key}
        state = self._load_state()
        state = self._refresh_rotated_credentials(state, available)
        active = str(state.get("active_label") or "primary")
        ordered: list[str] = []
        if active in available and not self._is_disabled(state, active):
            ordered.append(active)
        for label in ["primary", "backup"]:
            if label in available and label not in ordered and not self._is_disabled(state, label):
                ordered.append(label)
        return [(label, available[label]) for label in ordered]

    def record_success(self, label: str) -> None:
        state = self._load_state()
        credential = dict(state.get("credentials", {}).get(label, {}))
        credential.update(
            {
                "status": "available",
                "last_success_at": self._now(),
                "last_error": None,
                "last_http_status": None,
                "disabled_until": None,
            }
        )
        state.setdefault("credentials", {})[label] = credential
        if state.get("active_label") not in {"primary", "backup"}:
            state["active_label"] = label
        self._save_state(state, reason=f"{label}_success")

    def record_failure(self, label: str, failure: CredentialFailure) -> None:
        state = self._load_state()
        credential = dict(state.get("credentials", {}).get(label, {}))
        disabled_until: str | None = None
        status = failure.category
        if failure.category == "quota_exhausted":
            disabled_until = (datetime.now(UTC) + timedelta(hours=self.quota_probe_after_hours)).isoformat()
            self._switch_active_to_other(state, label)
        elif failure.category == "invalid_auth":
            disabled_until = None
            self._switch_active_to_other(state, label)
        elif failure.category == "rate_limited":
            disabled_until = (datetime.now(UTC) + timedelta(minutes=self.rate_limit_cooldown_minutes)).isoformat()
        elif failure.category == "transient_error":
            disabled_until = None
        credential.update(
            {
                "status": status,
                "last_error": failure.message[:240],
                "last_error_at": self._now(),
                "last_error_type": failure.error_type,
                "last_http_status": failure.http_status,
                "disabled_until": disabled_until,
            }
        )
        state.setdefault("credentials", {})[label] = credential
        self._save_state(state, reason=f"{label}_{failure.category}")

    def classify_exception(self, exc: BaseException) -> CredentialFailure:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        message = str(exc)
        if status in QUOTA_ERROR_STATUSES or "insufficient balance" in message.lower():
            category = "quota_exhausted"
        elif status in AUTH_ERROR_STATUSES:
            category = "invalid_auth"
        elif status in RATE_LIMIT_STATUSES:
            category = "rate_limited"
        elif status in TRANSIENT_STATUSES or exc.__class__.__name__ in {"Timeout", "TimeoutError", "ReadTimeout", "ConnectTimeout"}:
            category = "transient_error"
        else:
            category = "transient_error"
        return CredentialFailure(
            category=category,
            http_status=int(status) if status is not None else None,
            error_type=exc.__class__.__name__,
            message=message,
        )

    def _switch_active_to_other(self, state: dict[str, Any], failed_label: str) -> None:
        other = "backup" if failed_label == "primary" else "primary"
        state["active_label"] = other
        state["last_switch_reason"] = f"{failed_label}_unavailable"
        state["last_switch_at"] = self._now()

    def _is_disabled(self, state: dict[str, Any], label: str) -> bool:
        credential = (state.get("credentials") or {}).get(label) or {}
        if credential.get("status") == "invalid_auth":
            return True
        disabled_until = credential.get("disabled_until")
        if not disabled_until:
            return False
        try:
            until = datetime.fromisoformat(str(disabled_until).replace("Z", "+00:00"))
        except ValueError:
            return False
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        return until > datetime.now(UTC)

    def _refresh_rotated_credentials(self, state: dict[str, Any], available: dict[str, str]) -> dict[str, Any]:
        changed = False
        credentials = dict(state.get("credentials") or {})
        for label, key in available.items():
            fingerprint = self._fingerprint(key)
            credential = dict(credentials.get(label, {}))
            if credential.get("key_fingerprint") == fingerprint:
                continue
            credential.update(
                {
                    "status": "available",
                    "key_fingerprint": fingerprint,
                    "last_error": None,
                    "last_http_status": None,
                    "disabled_until": None,
                    "rotated_at": self._now(),
                }
            )
            credentials[label] = credential
            if label == "primary":
                state["active_label"] = "primary"
                state["last_switch_reason"] = "primary_key_rotated"
                state["last_switch_at"] = self._now()
            changed = True
        if not changed:
            return state
        state = dict(state)
        state["credentials"] = credentials
        self._save_state(state, reason="credential_key_rotation_detected")
        return state

    def _fingerprint(self, value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

    def _load_state(self) -> dict[str, Any]:
        if self.store is None:
            return self._memory_state
        row = self.store.fetch_latest("runtime_state", self.state_symbol)
        if not row:
            return self._default_state()
        payload = row.get("payload") or {}
        return payload if isinstance(payload, dict) else self._default_state()

    def _save_state(self, state: dict[str, Any], *, reason: str) -> None:
        state = dict(state)
        state["updated_at"] = self._now()
        state["reason"] = reason
        if self.store is None:
            self._memory_state = state
            return
        self.store.insert("runtime_state", state, self.state_symbol)

    def _default_state(self) -> dict[str, Any]:
        return {
            "active_label": "primary",
            "credentials": {
                "primary": {"status": "available"},
                "backup": {"status": "available"},
            },
        }

    def _now(self) -> str:
        return datetime.now(UTC).isoformat()
