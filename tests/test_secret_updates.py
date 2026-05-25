from __future__ import annotations

import json

import pytest

from ai_quant_trader.core.models import SecretService, SecretUpdateCommand
from ai_quant_trader.core.secrets import SecretCommandError, SecretUpdateManager
from ai_quant_trader.storage.sqlite import SQLiteStore


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "trader.sqlite3"
    audit = tmp_path / "audit.jsonl"
    store = SQLiteStore(str(db), str(audit))
    try:
        yield store
    finally:
        store.close()


def test_parse_gate_secret_update(store, tmp_path) -> None:
    manager = SecretUpdateManager(store, str(tmp_path / ".env.runtime"), admin_user_ids=["admin"])
    command = manager.parse_command(
        "update Gate API key gate_key_123456 secret gate_secret_abcdef",
        operator_id="admin",
    )
    assert command.service == SecretService.GATEIO
    assert command.values["GATEIO_API_KEY"] == "gate_key_123456"
    assert command.values["GATEIO_API_SECRET"] == "gate_secret_abcdef"


@pytest.mark.asyncio
async def test_apply_secret_update_masks_storage(store, tmp_path) -> None:
    env_path = tmp_path / ".env.runtime"
    manager = SecretUpdateManager(store, str(env_path), admin_user_ids=["admin"])
    command = manager.parse_command("update DeepSeek API sk-test-secret-123456", operator_id="admin")
    record = await manager.apply_command(command)

    assert record.key_tail == "******123456"
    rows = store.fetch_secret_versions("deepseek", limit=1)
    payload_text = json.dumps(rows[0]["payload"], ensure_ascii=False)
    assert "sk-test-secret-123456" not in payload_text
    assert "sk-test-secret-123456" in env_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_non_admin_rejected(store, tmp_path) -> None:
    manager = SecretUpdateManager(store, str(tmp_path / ".env.runtime"), admin_user_ids=["admin"])
    command = manager.parse_command("update DeepSeek API sk-test-secret-123456", operator_id="user")
    with pytest.raises(PermissionError):
        await manager.apply_command(command)


def test_missing_gate_secret_rejected(store, tmp_path) -> None:
    manager = SecretUpdateManager(store, str(tmp_path / ".env.runtime"), admin_user_ids=["admin"])
    with pytest.raises(SecretCommandError):
        manager.parse_command("update Gate API key gate_key_123456", operator_id="admin")


@pytest.mark.asyncio
async def test_apply_two_gate_account_slots_masks_storage(store, tmp_path) -> None:
    env_path = tmp_path / ".env.runtime"
    manager = SecretUpdateManager(store, str(env_path), admin_user_ids=["admin"])

    trend = await manager.apply_command(
        SecretUpdateCommand(
            service=SecretService.GATEIO_TREND,
            values={"GATEIO_TREND_API_KEY": "trend_key_123456", "GATEIO_TREND_API_SECRET": "trend_secret_abcdef"},
            operator_id="admin",
        )
    )
    range_account = await manager.apply_command(
        SecretUpdateCommand(
            service=SecretService.GATEIO_RANGE,
            values={"GATEIO_RANGE_API_KEY": "range_key_123456", "GATEIO_RANGE_API_SECRET": "range_secret_abcdef"},
            operator_id="admin",
        )
    )

    assert trend.key_tail == "******123456"
    assert range_account.secret_tail == "******abcdef"
    payload_text = json.dumps(store.fetch_secret_versions("gateio_trend", limit=1), ensure_ascii=False)
    assert "trend_key_123456" not in payload_text
    env_text = env_path.read_text(encoding="utf-8")
    assert "GATEIO_TREND_API_KEY=trend_key_123456" in env_text
    assert "GATEIO_RANGE_API_SECRET=range_secret_abcdef" in env_text


