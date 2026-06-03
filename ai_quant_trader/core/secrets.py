from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Callable

from ai_quant_trader.core.models import SecretService, SecretUpdateCommand, SecretVersionRecord
from ai_quant_trader.storage.sqlite import SQLiteStore


class SecretCommandError(ValueError):
    pass


class SecretUpdateManager:
    """Runtime API-key updater.

    Plaintext is accepted at the command boundary by user request, but storage,
    audit logs, and replies only use masked fingerprints.
    """

    KEY_MAP = {
        SecretService.DEEPSEEK: ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"),
        SecretService.GATEIO: ("GATEIO_API_KEY", "GATEIO_API_SECRET"),
        SecretService.GATEIO_TREND: ("GATEIO_TREND_API_KEY", "GATEIO_TREND_API_SECRET"),
        SecretService.GATEIO_RANGE: ("GATEIO_RANGE_API_KEY", "GATEIO_RANGE_API_SECRET"),
        SecretService.GATEIO_FOLLOWER: ("GATEIO_FOLLOWER_API_KEY", "GATEIO_FOLLOWER_API_SECRET"),
    }

    def __init__(
        self,
        store: SQLiteStore,
        runtime_env_path: str = ".env.runtime",
        admin_user_ids: list[str] | None = None,
        reload_callback: Callable[[SecretService], object] | None = None,
    ):
        self.store = store
        self.runtime_env_path = Path(runtime_env_path)
        self.admin_user_ids = set(admin_user_ids or [])
        self.reload_callback = reload_callback

    def parse_command(self, text: str, operator_id: str, raw_message_id: str | None = None) -> SecretUpdateCommand:
        stripped = text.strip()
        lowered = stripped.lower()
        if "deepseek" in lowered:
            match = re.search(r"(sk-[A-Za-z0-9_\-\.]{8,}|[A-Za-z0-9_\-\.]{12,})", stripped, flags=re.I)
            if not match:
                raise SecretCommandError("missing_deepseek_api_key")
            return SecretUpdateCommand(
                service=SecretService.DEEPSEEK,
                values={"DEEPSEEK_API_KEY": match.group(1)},
                operator_id=operator_id,
                raw_message_id=raw_message_id,
            )
        if "gate" in lowered:
            key = self._extract_after(stripped, ["key", "api key", "apikey"])
            secret = self._extract_after(stripped, ["secret", "api secret", "apisecret"])
            if not key or not secret:
                raise SecretCommandError("missing_gate_key_or_secret")
            return SecretUpdateCommand(
                service=SecretService.GATEIO,
                values={"GATEIO_API_KEY": key, "GATEIO_API_SECRET": secret},
                operator_id=operator_id,
                raw_message_id=raw_message_id,
            )
        raise SecretCommandError("unsupported_secret_update_command")

    async def apply_command(self, command: SecretUpdateCommand) -> SecretVersionRecord:
        self._assert_admin(command.operator_id)
        self._validate_values(command)
        previous_values = self._read_runtime_env()
        updated_values = {**previous_values, **command.values}
        self._write_runtime_env(updated_values)
        for key, value in command.values.items():
            os.environ[key] = value

        if self.reload_callback:
            maybe_result = self.reload_callback(command.service)
            if hasattr(maybe_result, "__await__"):
                await maybe_result

        version = self.store.next_secret_version(command.service.value)
        record = self._record_for(command, version)
        self.store.insert("secret_versions", record.model_dump(mode="json"), symbol=command.service.value)
        return record

    async def rollback(self, service: SecretService, operator_id: str) -> SecretVersionRecord:
        self._assert_admin(operator_id)
        records = self.store.fetch_secret_versions(service.value, limit=2)
        if len(records) < 2:
            raise SecretCommandError("no_previous_secret_version")
        previous = records[1]["payload"]
        # We intentionally cannot reconstruct plaintext from audit records.
        raise SecretCommandError(
            f"rollback_requires_plaintext_reapply:{previous.get('key_tail', 'unknown')}"
        )

    def status(self) -> list[dict[str, str | int]]:
        rows = []
        for service in SecretService:
            latest = self.store.fetch_secret_versions(service.value, limit=1)
            if latest:
                payload = latest[0]["payload"]
                rows.append(
                    {
                        "service": service.value,
                        "version": payload.get("version", 0),
                        "key_tail": payload.get("key_tail", "-"),
                        "status": payload.get("status", "unknown"),
                    }
                )
            else:
                rows.append({"service": service.value, "version": 0, "key_tail": "-", "status": "not_set"})
        return rows

    def _assert_admin(self, operator_id: str) -> None:
        if self.admin_user_ids and operator_id not in self.admin_user_ids:
            raise PermissionError("operator_not_in_admin_whitelist")

    def assert_admin(self, operator_id: str) -> None:
        self._assert_admin(operator_id)

    def _validate_values(self, command: SecretUpdateCommand) -> None:
        required = self.KEY_MAP[command.service]
        missing = [key for key in required if key not in command.values and key != "DEEPSEEK_BASE_URL"]
        if missing:
            raise SecretCommandError(f"missing_required_secret_fields:{','.join(missing)}")
        for key, value in command.values.items():
            if not value or len(value.strip()) < 8:
                raise SecretCommandError(f"invalid_secret_value:{key}")
            if "\n" in value or "\r" in value:
                raise SecretCommandError(f"multiline_secret_rejected:{key}")

    def _record_for(self, command: SecretUpdateCommand, version: int) -> SecretVersionRecord:
        key_name = self.KEY_MAP[command.service][0]
        secret_name = self.KEY_MAP[command.service][1]
        key_value = command.values.get(key_name, "")
        secret_value = command.values.get(secret_name)
        return SecretVersionRecord(
            service=command.service,
            version=version,
            operator_id=command.operator_id,
            key_fingerprint=self._fingerprint(key_value),
            key_tail=self._tail(key_value),
            secret_fingerprint=self._fingerprint(secret_value) if secret_value else None,
            secret_tail=self._tail(secret_value) if secret_value else None,
            status="applied",
        )

    def _read_runtime_env(self) -> dict[str, str]:
        values: dict[str, str] = {}
        if not self.runtime_env_path.exists():
            return values
        for raw_line in self.runtime_env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key] = value
        return values

    def _write_runtime_env(self, values: dict[str, str]) -> None:
        self.runtime_env_path.parent.mkdir(parents=True, exist_ok=True)
        content = "\n".join(f"{key}={value}" for key, value in sorted(values.items())) + "\n"
        self.runtime_env_path.write_text(content, encoding="utf-8")
        try:
            self.runtime_env_path.chmod(0o600)
        except OSError:
            # Windows does not honor POSIX chmod the same way; keep going.
            pass

    def _extract_after(self, text: str, labels: list[str]) -> str | None:
        for label in labels:
            pattern = rf"{re.escape(label)}\s*(?:is|=|:|to|为|是)?\s*([A-Za-z0-9_\-\.]{{8,}})"
            match = re.search(pattern, text, flags=re.I)
            if match:
                return match.group(1).strip()
        return None

    def _fingerprint(self, value: str | None) -> str:
        if not value:
            return ""
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

    def _tail(self, value: str | None) -> str:
        if not value:
            return ""
        return f"******{value[-6:]}"
