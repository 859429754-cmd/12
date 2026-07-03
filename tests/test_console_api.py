from __future__ import annotations

import base64
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

from ai_quant_trader.api import server
from ai_quant_trader.api.server import ConsoleContext, create_app
from ai_quant_trader.core.models import NewsDigest, NewsItem, PositionSnapshot, Side
from ai_quant_trader.storage.sqlite import SQLiteStore


def write_config(path: Path, db_path: Path, audit_path: Path, symbols: list[str] | None = None) -> None:
    symbols = symbols or ["ETH/USDT:USDT", "BTC/USDT:USDT"]
    symbol_yaml = "\n".join(f'  - symbol: "{symbol}"\n    timeframe: "1h"' for symbol in symbols)
    path.write_text(
        f"""
runtime:
  dry_run: true
  database_path: "{db_path.as_posix()}"
  audit_log_path: "{audit_path.as_posix()}"
symbols:
{symbol_yaml}
strategy:
  trend:
    kc_length: 20
    kc_scalar: 2.8
    vma_length: 20
    atr_length: 14
    volume_multiple: 1.5
ai:
  decision_model: "deepseek-v4-pro"
  report_model: "deepseek-v4-pro"
  emergency_screening_model: "deepseek-v4-flash"
  emergency_decision_model: "deepseek-v4-pro"
""",
        encoding="utf-8",
    )


def test_major_news_budget_cap_is_readiness_warning_not_live_block() -> None:
    latest = {
        "payload": {
            "call_type": "major_news_risk_review",
            "status": "blocked",
            "reason": "major_news_hourly_limit_exceeded",
        }
    }

    status, detail = server._ai_budget_readiness_status(latest, "live")

    assert status == "warn"
    assert "Major news" in detail


def test_auth_login_failure_lockout_and_security_headers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_AUTH_DISABLED", "0")
    monkeypatch.setenv("CONSOLE_ADMIN_USER", "admin")
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "correct-password")
    monkeypatch.setenv("CONSOLE_LOGIN_MAX_FAILURES", "2")
    monkeypatch.setenv("CONSOLE_LOGIN_LOCKOUT_MINUTES", "15")
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", symbols=["ETH/USDT:USDT"])

    client = TestClient(create_app(str(config_path)))

    health = client.get("/api/health")
    assert health.headers["x-content-type-options"] == "nosniff"
    assert health.headers["x-frame-options"] == "DENY"
    assert client.post("/api/auth/login", json={"username": "admin", "password": "bad"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": "bad"}).status_code == 401
    locked = client.post("/api/auth/login", json={"username": "admin", "password": "correct-password"})
    assert locked.status_code == 429
    assert locked.json()["detail"]["locked"] is True


def test_console_session_exposes_expiry_and_audits_expired_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_AUTH_DISABLED", "0")
    monkeypatch.setenv("CONSOLE_ADMIN_USER", "admin")
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "admin-secret")
    monkeypatch.setenv("CONSOLE_SESSION_HOURS", "1")
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    write_config(config_path, db_path, audit_path, symbols=["ETH/USDT:USDT"])
    app = create_app(str(config_path))
    client = TestClient(app)

    login = client.post("/api/auth/login", json={"username": "admin", "password": "admin-secret"})

    assert login.status_code == 200
    body = login.json()
    assert body["session_expires_at"]
    assert body["session_seconds_remaining"] > 3500
    assert body["session_expiring_soon"] is False

    token = login.cookies.get("aiq_session")
    assert token
    app.state.console_sessions[token]["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    expired_client = TestClient(app)
    expired_client.cookies.set("aiq_session", token)
    expired = expired_client.get("/api/status")

    assert expired.status_code == 401
    store = SQLiteStore(str(db_path), str(audit_path))
    try:
        latest = store.fetch_latest("security_events")
    finally:
        store.close()
    assert latest is not None
    payload = latest["payload"]
    assert payload["event"] == "session_expired"
    assert "aiq_session" in payload["payload"]["reason"]


def test_console_ip_allowlist_blocks_untrusted_clients(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_AUTH_DISABLED", "1")
    monkeypatch.setenv("CONSOLE_ALLOWED_IPS", "203.0.113.10,198.51.100.0/24")
    monkeypatch.setenv("CONSOLE_TRUST_PROXY_HEADERS", "1")
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", symbols=["ETH/USDT:USDT"])

    client = TestClient(create_app(str(config_path)))

    blocked = client.get("/api/health", headers={"x-forwarded-for": "192.0.2.9"})
    allowed = client.get("/api/health", headers={"x-forwarded-for": "198.51.100.7"})

    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "ip_not_allowed"
    assert allowed.status_code == 200


def test_console_ip_allowlist_ignores_forwarded_for_without_trusted_proxy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_AUTH_DISABLED", "1")
    monkeypatch.setenv("CONSOLE_ALLOWED_IPS", "198.51.100.0/24")
    monkeypatch.delenv("CONSOLE_TRUST_PROXY_HEADERS", raising=False)
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", symbols=["ETH/USDT:USDT"])

    client = TestClient(create_app(str(config_path)), client=("198.51.100.7", 12345))

    allowed = client.get("/api/health", headers={"x-forwarded-for": "192.0.2.9"})

    assert allowed.status_code == 200


def test_prometheus_metrics_requires_console_auth_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_AUTH_DISABLED", "0")
    monkeypatch.setenv("CONSOLE_ADMIN_USER", "admin")
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "admin-secret")
    monkeypatch.delenv("METRICS_PUBLIC_ENABLED", raising=False)
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", symbols=["ETH/USDT:USDT"])
    client = TestClient(create_app(str(config_path)))

    blocked = client.get("/metrics")
    assert blocked.status_code == 401

    assert client.post("/api/auth/login", json={"username": "admin", "password": "admin-secret"}).status_code == 200
    allowed = client.get("/metrics")
    assert allowed.status_code == 200
    assert "ai_quant_readiness_status" in allowed.text


def test_readiness_allows_loopback_watchdog_but_not_remote_without_auth(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_AUTH_DISABLED", "0")
    monkeypatch.setenv("CONSOLE_ADMIN_USER", "admin")
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD_SHA256", "8c6976e5b5410415bde908bd4dee15dfb167a9c873fc4bb8a81f6f2ab448a918")  # admin
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", symbols=["ETH/USDT:USDT"])

    local_client = TestClient(create_app(str(config_path)), client=("127.0.0.1", 12345))
    remote_client = TestClient(create_app(str(config_path)), client=("203.0.113.44", 12345))

    assert local_client.get("/api/system/readiness").status_code == 200
    assert remote_client.get("/api/system/readiness").status_code == 401


def test_public_probe_requests_are_blocked_and_audited(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_AUTH_DISABLED", "1")
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    write_config(config_path, db_path, audit_path, symbols=["ETH/USDT:USDT"])
    client = TestClient(create_app(str(config_path)), client=("203.0.113.44", 12345))

    normal = client.get("/")
    probe = client.get("/?document=..%2F..%2F.env.production")

    assert normal.status_code in {200, 404}
    assert probe.status_code == 404
    store = SQLiteStore(str(db_path), str(audit_path))
    try:
        latest = store.fetch_latest("security_events")
    finally:
        store.close()
    assert latest is not None
    payload = latest["payload"]
    assert payload["event"] == "suspicious_probe_blocked"
    assert payload["username"] == "anonymous"
    assert payload["payload"]["reason"] in {"env_file_probe", "path_traversal_probe"}


def test_console_alert_user_is_admin_credential(monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_ALERT_USER", "alert")
    monkeypatch.setenv("CONSOLE_ALERT_PASSWORD", "alert-secret")

    user = server._authenticate_console_user("alert", "alert-secret")

    assert user is not None
    assert user["role"] == "admin"


def test_security_events_api_is_admin_only_and_summarized(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_AUTH_DISABLED", "0")
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "admin-secret")
    monkeypatch.setenv("CONSOLE_ACCOUNT1_PASSWORD", "account-secret")
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    write_config(config_path, db_path, audit_path, ["ETH/USDT:USDT"])
    client = TestClient(create_app(str(config_path)), client=("203.0.113.77", 12345))

    assert client.post("/api/auth/login", json={"username": "account1", "password": "bad"}).status_code == 401
    account_login = client.post("/api/auth/login", json={"username": "account1", "password": "account-secret"})
    assert account_login.status_code == 200
    denied = client.get("/api/system/security-events")
    assert denied.status_code == 403

    client.post("/api/auth/logout", json={})
    admin_login = client.post("/api/auth/login", json={"username": "admin", "password": "admin-secret"})
    assert admin_login.status_code == 200
    client.post("/api/control/authorize", json={"operator_id": "admin", "symbols": ["ETH/USDT:USDT"]})
    events = client.get("/api/system/security-events?limit=20")
    assert events.status_code == 200
    body = events.json()
    assert body["summary"]["total"] >= 3
    assert body["summary"]["by_event"]["login_failed"] >= 1
    assert body["summary"]["by_event"]["login_success"] >= 1
    assert body["summary"]["by_event"]["admin_action_confirmation_missing"] >= 1
    first_payload = body["items"][0]["payload"]
    assert "password" not in str(first_payload).lower()
    assert "api_secret" not in str(first_payload).lower()


def test_readiness_exposes_runtime_alerts_for_unknown_order(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_AUTH_DISABLED", "1")
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    write_config(config_path, db_path, audit_path, symbols=["ETH/USDT:USDT"])
    store = SQLiteStore(str(db_path), str(audit_path))
    try:
        store.insert(
            "order_lifecycle",
            {
                "status": "unknown",
                "order_type": "market",
                "client_order_id": "entry-unknown",
                "symbol": "ETH/USDT:USDT",
                "reason": "exchange_timeout",
            },
            "ETH/USDT:USDT",
        )
    finally:
        store.close()

    client = TestClient(create_app(str(config_path)))
    body = client.get("/api/system/readiness").json()
    events = {item["event"] for item in body["runtime_alerts"]}

    assert "order_status_unknown" in events
    assert body["runtime_alert_summary"]["total"] >= 1
    alerts = client.get("/api/system/alerts").json()
    assert alerts["summary"]["total"] == body["runtime_alert_summary"]["total"]


def test_console_context_reload_does_not_close_inflight_store(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    write_config(config_path, db_path, audit_path, symbols=["ETH/USDT:USDT"])

    ctx = ConsoleContext(str(config_path))
    old_store = ctx.store
    assert old_store is not None
    old_store.insert("ai_decisions", {"symbol": "ETH/USDT:USDT", "message": "inflight"}, "ETH/USDT:USDT")

    try:
        ctx.reload(force=True)
        assert old_store.fetch_latest("ai_decisions", "ETH/USDT:USDT") is not None
    finally:
        ctx.close()


def test_console_context_reload_reuses_store_when_config_is_unchanged(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", symbols=["ETH/USDT:USDT"])

    ctx = ConsoleContext(str(config_path))
    try:
        first_store = ctx.store
        ctx.reload()
        assert ctx.store is first_store
    finally:
        ctx.close()


def test_order_lifecycle_endpoint_filters_account_slot(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    write_config(config_path, db_path, audit_path, symbols=["ETH/USDT:USDT"])
    store = SQLiteStore(str(db_path), str(audit_path))
    try:
        store.insert(
            "order_lifecycle",
            {
                "client_order_id": "trend_entry",
                "symbol": "ETH/USDT:USDT",
                "status": "filled",
                "account_slot": "trend",
                "order_type": "market",
                "side": "buy",
                "amount": 0.23,
                "reduce_only": False,
                "gateway_mode": "live",
                "metadata": {"risk_position_tier": "weak", "risk_position_scale": 0.25},
            },
            "ETH/USDT:USDT",
        )
        store.insert(
            "order_lifecycle",
            {
                "client_order_id": "follower_entry",
                "symbol": "ETH/USDT:USDT",
                "status": "filled",
                "account_slot": "follower",
                "order_type": "market",
                "side": "buy",
                "amount": 0.3,
                "reduce_only": False,
                "gateway_mode": "live",
                "metadata": {"risk_position_tier": "normal", "risk_position_scale": 0.5},
            },
            "ETH/USDT:USDT",
        )
    finally:
        store.close()

    client = TestClient(create_app(str(config_path)))
    response = client.get("/api/order-lifecycle?symbol=ETH/USDT:USDT&account_slot=trend")

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["payload"]["client_order_id"] == "trend_entry"
    assert items[0]["payload"]["metadata"]["risk_position_tier"] == "weak"


def test_ai_position_tier_audit_endpoint_returns_shadow_ledger(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    write_config(config_path, db_path, audit_path, symbols=["ETH/USDT:USDT"])
    store = SQLiteStore(str(db_path), str(audit_path))
    try:
        store.insert(
            "order_lifecycle",
            {
                "client_order_id": "entry_1",
                "symbol": "ETH/USDT:USDT",
                "status": "filled",
                "account_slot": "trend",
                "order_type": "market",
                "side": "buy",
                "amount": 2.5,
                "reduce_only": False,
                "gateway_mode": "mock",
                "reason": "weak_size_by_partial_consensus",
                "order": {"price": 100, "status": "closed"},
                "metadata": {
                    "risk_position_tier": "weak",
                    "risk_position_scale": 0.25,
                    "strategy_baseline_notional": 1000,
                    "ai_desired_notional": 250,
                },
            },
            "ETH/USDT:USDT",
        )
        store.insert(
            "order_lifecycle",
            {
                "client_order_id": "exit_1",
                "symbol": "ETH/USDT:USDT",
                "status": "filled",
                "account_slot": "trend",
                "order_type": "market",
                "side": "sell",
                "amount": 2.5,
                "reduce_only": True,
                "gateway_mode": "mock",
                "reason": "pytest_exit",
                "order": {"price": 110, "status": "closed"},
                "metadata": {},
            },
            "ETH/USDT:USDT",
        )
    finally:
        store.close()

    client = TestClient(create_app(str(config_path)))
    response = client.get("/api/audits/ai-position-tiers?symbol=ETH%2FUSDT%3AUSDT&account_slot=trend&min_sample_warning=1")
    body = response.json()

    assert response.status_code == 200
    assert body["ok"] is True
    assert body["overall"]["closed"] == 1
    assert body["by_tier"]["weak"]["winner_upside_missed_usdt"] == 75
    assert body["shadow_by_tier"]["weak"]["total_pnl_usdt"] == 25
    assert body["shadow_by_tier"]["full"]["total_pnl_usdt"] == 100


def test_walk_forward_proposal_is_needs_review_without_auto_apply(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    write_config(config_path, db_path, audit_path, symbols=["ETH/USDT:USDT"])
    ctx = ConsoleContext(str(config_path))
    body = server.BacktestOptimizeRequest(symbol="ETH/USDT:USDT", min_trades=2)
    result = {
        "baseline": {"total_return_pct": 8.0, "max_drawdown_pct": -12.0, "profit_factor": 1.25, "trade_count": 12},
        "baseline_params": {"kc_scalar": 2.8, "volume_multiple": 2.5},
        "best": {
            "params": {"kc_scalar": 3.0, "volume_multiple": 2.8},
            "validation": {"total_return_pct": 13.0, "max_drawdown_pct": -12.5, "profit_factor": 1.42, "trade_count": 8},
            "train": {"total_return_pct": 10.0, "max_drawdown_pct": -11.0, "profit_factor": 1.35, "trade_count": 18},
            "warnings": [],
        },
        "candidates": [],
        "data_split": {"train_rows": 300, "validation_rows": 120},
    }
    try:
        proposal_id = server._record_walk_forward_proposal(ctx, body, result, job_id="wf_ok")
        row = ctx.store.fetch_by_id("optimization_proposals", proposal_id)
        payload = row["payload"]
        assert payload["type"] == "walk_forward_parameter_proposal"
        assert payload["status"] == "needs_review"
        assert payload["auto_apply"] is False
        assert payload["changes"] == {}
        assert payload["proposed_changes"]["kc_scalar"]["new"] == 3.0
        assert payload["acceptance"]["accepted"] is True
        assert result["walk_forward_proposal_id"] == proposal_id
    finally:
        ctx.close()


def test_walk_forward_proposal_rejects_weak_validation_and_endpoint_filters(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    write_config(config_path, db_path, audit_path, symbols=["ETH/USDT:USDT", "BTC/USDT:USDT"])
    ctx = ConsoleContext(str(config_path))
    body = server.BacktestOptimizeRequest(symbol="ETH/USDT:USDT", min_trades=10)
    result = {
        "baseline": {"total_return_pct": 8.0, "max_drawdown_pct": -10.0, "profit_factor": 1.2, "trade_count": 15},
        "baseline_params": {"kc_scalar": 2.8},
        "best": {
            "params": {"kc_scalar": 2.4},
            "validation": {"total_return_pct": 6.0, "max_drawdown_pct": -14.0, "profit_factor": 0.9, "trade_count": 3},
            "warnings": ["too_few_trades"],
        },
        "candidates": [],
        "data_split": {"train_rows": 300, "validation_rows": 120},
    }
    try:
        proposal_id = server._record_walk_forward_proposal(ctx, body, result, job_id="wf_bad")
        client = TestClient(create_app(str(config_path)))
        response = client.get("/api/walk-forward/proposals?symbol=ETH/USDT:USDT&status=rejected")
        assert response.status_code == 200
        items = response.json()["items"]
        assert any(item["id"] == proposal_id for item in items)
        payload = next(item["payload"] for item in items if item["id"] == proposal_id)
        assert payload["status"] == "rejected"
        assert payload["acceptance"]["accepted"] is False
        assert payload["changes"] == {}
        assert payload["acceptance"]["risks"]
    finally:
        ctx.close()


def test_status_latest_decision_excludes_major_news_review_audit(tmp_path: Path, monkeypatch) -> None:
    for key in ["GATEIO_API_KEY", "GATEIO_API_SECRET", "GATEIO_TREND_API_KEY", "GATEIO_TREND_API_SECRET"]:
        monkeypatch.setenv(key, "")
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    write_config(config_path, db_path, audit_path, symbols=["ETH/USDT:USDT"])
    store = SQLiteStore(str(db_path), str(audit_path))
    try:
        trade_id = store.insert(
            "ai_decisions",
            {
                "symbol": "ETH/USDT:USDT",
                "regime": "trend",
                "direction": "short",
                "confidence": 0.72,
                "veto_action": "reduce",
            },
            "ETH/USDT:USDT",
        )
        store.insert(
            "ai_decisions",
            {
                "review_type": "major_news_risk_review",
                "no_order_submitted": True,
                "signal": {"action": "hold", "technical_evidence": {"original_strategy_action": "short"}},
                "ai": {"veto_action": "block"},
            },
            "ETH/USDT:USDT",
        )
    finally:
        store.close()

    client = TestClient(create_app(str(config_path)))
    body = client.get("/api/status").json()

    latest = body["latest_decisions"]["ETH/USDT:USDT"]
    assert latest["id"] == trade_id
    assert latest["payload"]["direction"] == "short"


def test_console_status_strategy_and_workbench(tmp_path: Path, monkeypatch) -> None:
    for key in ["GATEIO_API_KEY", "GATEIO_API_SECRET", "GATEIO_TREND_API_KEY", "GATEIO_TREND_API_SECRET"]:
        monkeypatch.setenv(key, "")
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl")
    client = TestClient(create_app(str(config_path)))

    status = client.get("/api/status")
    assert status.status_code == 200
    body = status.json()
    assert body["dry_run"] is True
    assert body["opening_paused"] is True
    assert body["ai"]["decision_model"] == "deepseek-v4-pro"

    strategy = client.get("/api/strategy/config")
    assert strategy.status_code == 200
    assert strategy.json()["range_strategy"]["status"] == "预留模块"

    workbench = client.get("/api/workbench")
    assert workbench.status_code == 200
    policy = workbench.json()["decision_policy"]
    assert policy["technical_weight"] is None
    assert policy["position_tiers"] == {"block": 0.0, "weak": 0.25, "normal": 0.5, "strong": 0.75, "full": 1.0}
    assert policy["sizing_policy"] == "legacy_factor_ranked"
    assert "legacy_factor_ranked" in policy["rollback_command"]
    assert "trend_confirmation_score" in policy["score_fields"]

    risk = client.get("/api/risk/summary")
    assert risk.status_code == 200
    assert risk.json()["ai_sizing_policy"] == "legacy_factor_ranked"
    assert "legacy_factor_ranked" in risk.json()["rollback_command"]

    platform = client.get("/api/platform/overview")
    assert platform.status_code == 200
    platform_body = platform.json()
    assert platform_body["platform"]["shell"] == "quantdinger_style"
    assert platform_body["platform"]["notification_channels"] == []
    assert platform_body["platform"]["agent_gateway"]["paper_only"] is True
    assert [item["id"] for item in platform_body["workspaces"]] == [
        "dashboard",
        "market",
        "strategy",
        "ai",
        "agent",
        "execution",
        "data",
    ]
    assert platform_body["strategy_profiles"][0]["strategy_type"] == "trend"
    assert platform_body["strategy_profiles"][0]["execution_contract"]["entry_fill"] == "next_tradeable_open"
    assert platform_body["strategy_profiles"][0]["backtest_defaults"]["leverage"] == 4.0

    readiness = client.get("/api/system/readiness")
    assert readiness.status_code == 200
    readiness_body = readiness.json()
    assert readiness_body["execution_mode"] == "mock"
    assert readiness_body["profile_count"] == 2
    assert {item["id"] for item in readiness_body["checks"]} >= {"strategy_profiles", "deepseek", "risk"}
    assert {item["id"] for item in readiness_body["checks"]} >= {
        "exchange_safety",
        "reconciliation",
        "order_lifecycle",
        "data_health",
        "ai_drift",
        "major_news_review",
        "position_review",
        "worker_heartbeat",
        "runtime_maintenance",
    }
    order_lifecycle_check = next(item for item in readiness_body["checks"] if item["id"] == "order_lifecycle")
    assert order_lifecycle_check["status"] == "ok"
    assert "exchange_safety" in readiness_body
    assert "latest_order_lifecycle" in readiness_body
    assert "latest_data_health" in readiness_body
    assert "latest_ai_drift" in readiness_body
    assert "latest_news_risk_review" in readiness_body
    assert "latest_position_review" in readiness_body
    assert "latest_ai_budget" in readiness_body
    assert "latest_worker_heartbeats" in readiness_body
    assert "worker_heartbeat_details" in readiness_body
    assert {item["worker"] for item in readiness_body["worker_heartbeat_details"]} >= {
        "trading_worker",
        "news_worker",
        "price_monitor_worker",
        "order_status_worker",
    }
    assert all("allowed_seconds" in item for item in readiness_body["worker_heartbeat_details"])
    assert "latest_maintenance" in readiness_body

    metrics = client.get("/api/system/metrics")
    assert metrics.status_code == 200
    metrics_body = metrics.json()
    assert "readiness" in metrics_body
    assert "worker_heartbeats" in metrics_body
    assert "storage" in metrics_body


def test_market_candles_supports_intrabar_display_without_changing_default(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl")
    calls: list[bool] = []

    class StubMarket:
        async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int, source: str, closed_only: bool = True):
            calls.append(closed_only)
            frame = pd.DataFrame(
                {
                    "timestamp": [datetime(2026, 5, 24, 1, 0, tzinfo=UTC)],
                    "open": [100.0],
                    "high": [105.0],
                    "low": [95.0],
                    "close": [102.0],
                    "volume": [10.0],
                }
            )
            frame.attrs["data_source"] = source
            return frame

        async def close(self) -> None:
            return None

    monkeypatch.setattr(server, "MarketDataClient", StubMarket)
    client = TestClient(create_app(str(config_path)))

    default_response = client.get("/api/market/candles?symbol=ETH/USDT:USDT&timeframe=1h&limit=120")
    intrabar_response = client.get("/api/market/candles?symbol=ETH/USDT:USDT&timeframe=1h&limit=120&closed_only=false")

    assert default_response.status_code == 200
    assert default_response.json()["closed_only"] is True
    assert intrabar_response.status_code == 200
    assert intrabar_response.json()["closed_only"] is False
    assert calls == [True, False]


def test_console_can_create_global_max_leverage_proposal(tmp_path: Path, monkeypatch) -> None:
    for key in ["GATEIO_API_KEY", "GATEIO_API_SECRET", "GATEIO_TREND_API_KEY", "GATEIO_TREND_API_SECRET"]:
        monkeypatch.setenv(key, "")
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    client = TestClient(create_app(str(config_path)))

    created = client.post(
        "/api/proposals/parameter",
        json={
            "operator_id": "console",
            "path": "risk.max_total_leverage",
            "value": 12.5,
            "symbols": [],
        },
    )
    assert created.status_code == 200
    proposal_id = created.json()["proposal_id"]

    missing_confirm = client.post(f"/api/proposals/{proposal_id}/approve", json={"operator_id": "console"})
    assert missing_confirm.status_code == 400
    assert "必须显式二次确认" in missing_confirm.json()["detail"]

    approved = client.post(
        f"/api/proposals/{proposal_id}/approve",
        json={"operator_id": "console", "confirm_admin_action": True},
    )
    assert approved.status_code == 200

    strategy = client.get("/api/strategy/config")
    assert strategy.status_code == 200
    assert strategy.json()["risk"]["max_total_leverage"] == 12.5

    prometheus = client.get("/metrics")
    assert prometheus.status_code == 200
    assert "ai_quant_readiness_status" in prometheus.text
    assert "ai_quant_maintenance_ok" in prometheus.text

    profiles = client.get("/api/strategy/profiles")
    assert profiles.status_code == 200
    assert profiles.json()["items"][0]["symbol"] == "ETH/USDT:USDT"
    assert "optimization_defaults" in profiles.json()["items"][0]

    accounts = client.get("/api/execution/accounts")
    assert accounts.status_code == 200
    account_body = accounts.json()
    assert [item["slot"] for item in account_body["items"]] == ["trend", "follower", "range"]
    assert account_body["items"][0]["live_routing"] == "blocked_missing_credentials"
    assert "max_leverage" in account_body["items"][0]

    channels = client.get("/api/strategy/channels")
    assert channels.status_code == 200
    channel_body = channels.json()
    assert [item["channel"] for item in channel_body["items"]] == ["trend", "follower", "range"]
    assert channel_body["items"][0]["account_slot"] == "trend"
    assert channel_body["items"][0]["executable"] is True
    assert channel_body["items"][1]["account_slot"] == "follower"
    assert channel_body["items"][1]["executable"] is False
    assert channel_body["items"][2]["account_slot"] == "range"
    assert channel_body["items"][2]["strategy_type"] == "range_reserved"
    assert channel_body["items"][2]["executable"] is False

    dense = client.get("/api/dense-zones/latest?symbol=ETH%2FUSDT%3AUSDT")
    assert dense.status_code == 200
    assert dense.json()["item"] is None


def test_account_balance_prefers_gate_readonly_when_mock_has_configured_account(tmp_path: Path, monkeypatch) -> None:
    for key in ["GATEIO_API_KEY", "GATEIO_API_SECRET", "GATEIO_TREND_API_KEY", "GATEIO_TREND_API_SECRET"]:
        monkeypatch.setenv(key, "")
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    created: list[tuple[str, str]] = []

    class FakeGateway:
        def __init__(self, mode: str, account_slot: str) -> None:
            self.mode = mode
            self.account_slot = account_slot

        async def fetch_balance_summary(self) -> dict[str, object]:
            return {
                "ok": True,
                "mode": self.mode,
                "usdt_total": 321.5,
                "usdt_free": 300.0,
                "usdt_used": 21.5,
            }

        async def close(self) -> None:
            return None

    def fake_factory(mode_or_config: object, account_slot: str = "default") -> FakeGateway:
        mode = str(mode_or_config)
        created.append((mode, account_slot))
        return FakeGateway(mode, account_slot)

    monkeypatch.setattr(server, "_account_slot_configured", lambda slot: slot == "trend")
    monkeypatch.setattr(server, "create_exchange_gateway", fake_factory)
    client = TestClient(create_app(str(config_path)))

    response = client.get("/api/account/balance?account_slot=trend")
    assert response.status_code == 200
    body = response.json()
    assert created == [("live", "trend")]
    assert body["dry_run"] is True
    assert body["execution_mode"] == "mock"
    assert body["balance_source"] == "gate_live_readonly"
    assert body["read_only_live_balance"] is True
    assert body["usdt_total"] == 321.5


def test_account_balance_keeps_range_slot_distinct(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GATEIO_RANGE_API_KEY", "range_key")
    monkeypatch.setenv("GATEIO_RANGE_API_SECRET", "range_secret")
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    created: list[tuple[str, str]] = []

    class FakeGateway:
        def __init__(self, mode: str, account_slot: str) -> None:
            self.mode = mode
            self.account_slot = account_slot

        async def fetch_balance_summary(self) -> dict[str, object]:
            return {"ok": True, "account_slot": self.account_slot, "usdt_total": 123.0}

        async def close(self) -> None:
            return None

    def fake_factory(mode_or_config: object, account_slot: str = "default") -> FakeGateway:
        mode = str(mode_or_config)
        created.append((mode, account_slot))
        return FakeGateway(mode, account_slot)

    monkeypatch.setattr(server, "create_exchange_gateway", fake_factory)
    client = TestClient(create_app(str(config_path)))

    response = client.get("/api/account/balance?account_slot=range")
    assert response.status_code == 200
    assert response.json()["account_slot"] == "range"
    assert created == [("live", "range")]


def test_account_balance_does_not_fallback_to_mock_when_gate_readonly_fails(tmp_path: Path, monkeypatch) -> None:
    for key in ["GATEIO_API_KEY", "GATEIO_API_SECRET", "GATEIO_TREND_API_KEY", "GATEIO_TREND_API_SECRET"]:
        monkeypatch.setenv(key, "")
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])

    class FailingGateway:
        async def fetch_balance_summary(self) -> dict[str, object]:
            raise TimeoutError("gate timeout")

        async def close(self) -> None:
            return None

    monkeypatch.setattr(server, "_account_slot_configured", lambda slot: slot == "trend")
    monkeypatch.setattr(server, "create_exchange_gateway", lambda mode_or_config, account_slot="default": FailingGateway())
    client = TestClient(create_app(str(config_path)))

    response = client.get("/api/account/balance?account_slot=trend")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["balance_source"] == "gate_live_readonly"
    assert body["usdt_total"] is None
    assert "10000" not in body["message"]


def test_account_balance_returns_cached_snapshot_when_gate_is_slow(tmp_path: Path, monkeypatch) -> None:
    for key in ["GATEIO_API_KEY", "GATEIO_API_SECRET", "GATEIO_TREND_API_KEY", "GATEIO_TREND_API_SECRET"]:
        monkeypatch.setenv(key, "")
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    write_config(config_path, db_path, tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])

    class SlowGateway:
        async def fetch_balance_summary(self) -> dict[str, object]:
            await asyncio.sleep(0.2)
            return {"ok": True, "usdt_total": 999.0}

        async def close(self) -> None:
            return None

    monkeypatch.setattr(server, "_account_slot_configured", lambda slot: slot == "trend")
    monkeypatch.setattr(server, "create_exchange_gateway", lambda mode_or_config, account_slot="default": SlowGateway())
    client = TestClient(create_app(str(config_path)))
    store = server._ctx(client.app).store
    assert store is not None
    store.insert(
        "account_balance_snapshots",
        {
            "ok": True,
            "execution_mode": "mock",
            "balance_source": "gate_live_readonly",
            "read_only_live_balance": True,
            "usdt_total": 456.0,
            "usdt_free": 400.0,
            "usdt_used": 56.0,
        },
        symbol="trend",
    )

    response = client.get("/api/account/balance?account_slot=trend&timeout_seconds=0.1&max_cache_age_seconds=0")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["cached"] is True
    assert body["stale"] is True
    assert body["balance_source"] == "cached_live_balance"
    assert body["usdt_total"] == 456.0
    assert body["cache_age_seconds"] >= 0
    assert "10000" not in body["message"]


def test_account_balance_uses_fresh_cached_snapshot_without_gate_call(tmp_path: Path, monkeypatch) -> None:
    for key in ["GATEIO_API_KEY", "GATEIO_API_SECRET", "GATEIO_TREND_API_KEY", "GATEIO_TREND_API_SECRET"]:
        monkeypatch.setenv(key, "")
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    write_config(config_path, db_path, tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])

    def fail_factory(mode_or_config: object, account_slot: str = "default"):
        raise AssertionError("fresh cached balance should not construct a gateway")

    monkeypatch.setattr(server, "_account_slot_configured", lambda slot: slot == "trend")
    monkeypatch.setattr(server, "create_exchange_gateway", fail_factory)
    client = TestClient(create_app(str(config_path)))
    store = server._ctx(client.app).store
    assert store is not None
    store.insert(
        "account_balance_snapshots",
        {
            "ok": True,
            "execution_mode": "mock",
            "balance_source": "gate_live_readonly",
            "read_only_live_balance": True,
            "usdt_total": 456.0,
            "usdt_free": 400.0,
            "usdt_used": 56.0,
        },
        symbol="trend",
    )

    response = client.get("/api/account/balance?account_slot=trend&max_cache_age_seconds=600")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["cached"] is True
    assert body["stale"] is False
    assert body["balance_source"] == "cached_live_balance"
    assert body["usdt_total"] == 456.0
    assert body["cache_age_seconds"] >= 0


def test_positions_prefer_gate_readonly_when_mock_has_configured_account(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    created: list[tuple[str, str]] = []

    class FakeGateway:
        def __init__(self, mode: str, account_slot: str) -> None:
            self.mode = mode
            self.account_slot = account_slot

        async def fetch_positions(self, symbols: list[str]) -> list[PositionSnapshot]:
            assert symbols == ["ETH/USDT:USDT"]
            return [
                PositionSnapshot(
                    symbol="ETH/USDT:USDT",
                    side=Side.LONG,
                    qty=0.23,
                    entry_price=1719.36,
                    mark_price=1727.0,
                    unrealized_pnl=1.75,
                )
            ]

        async def close(self) -> None:
            return None

    def fake_factory(mode_or_config: object, account_slot: str = "default") -> FakeGateway:
        mode = str(mode_or_config)
        created.append((mode, account_slot))
        return FakeGateway(mode, account_slot)

    monkeypatch.setattr(server, "_account_slot_configured", lambda slot: slot == "trend")
    monkeypatch.setattr(server, "create_exchange_gateway", fake_factory)
    client = TestClient(create_app(str(config_path)))

    response = client.get("/api/positions?account_slot=trend")
    assert response.status_code == 200
    body = response.json()
    assert created == [("live", "trend")]
    assert body["source"] == "gate_live_readonly"
    assert body["account_slot"] == "trend"
    assert body["items"][0]["payload"]["side"] == "long"
    assert body["items"][0]["payload"]["qty"] == 0.23
    assert body["items"][0]["payload"]["account_slot"] == "trend"


def test_positions_keep_follower_snapshot_separate_from_trend(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    write_config(config_path, db_path, tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    client = TestClient(create_app(str(config_path)))
    store = server._ctx(client.app).store
    assert store is not None
    store.insert(
        "positions_snapshot",
        {"symbol": "ETH/USDT:USDT", "side": "long", "qty": 0.5, "account_slot": "trend"},
        symbol="ETH/USDT:USDT",
    )
    store.insert(
        "positions_snapshot",
        {"symbol": "ETH/USDT:USDT", "side": "short", "qty": 0.2, "account_slot": "follower"},
        symbol="ETH/USDT:USDT",
    )

    response = client.get("/api/positions?account_slot=follower&prefer_live=false")
    assert response.status_code == 200
    body = response.json()
    assert body["account_slot"] == "follower"
    assert len(body["items"]) == 1
    assert body["items"][0]["payload"]["side"] == "short"
    assert body["items"][0]["payload"]["account_slot"] == "follower"


def test_live_account_balance_failure_returns_unavailable_not_mock(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    config_text = config_path.read_text(encoding="utf-8")
    config_path.write_text(config_text.replace("  dry_run: true", "  dry_run: false\n  execution_mode: live"), encoding="utf-8")

    class FailingGateway:
        async def fetch_balance_summary(self) -> dict[str, object]:
            raise TimeoutError("gate timeout")

        async def close(self) -> None:
            return None

    monkeypatch.setattr(server, "create_exchange_gateway", lambda mode_or_config, account_slot="default": FailingGateway())
    client = TestClient(create_app(str(config_path)))

    response = client.get("/api/account/balance?account_slot=trend")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["execution_mode"] == "live"
    assert body["balance_source"] == "live"
    assert body["usdt_total"] is None
    assert "10000" not in body["message"]


def test_readiness_blocks_failed_backup_integrity(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GATEIO_API_KEY", "")
    monkeypatch.setenv("GATEIO_API_SECRET", "")
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    config_path = tmp_path / "config.yaml"
    write_config(config_path, db_path, audit_path)
    store = SQLiteStore(str(db_path), str(audit_path))
    try:
        store.insert(
            "maintenance_runs",
            {
                "checked_at": datetime.now(UTC).isoformat(),
                "sqlite_backup_path": str(tmp_path / "broken.sqlite3.gz"),
                "sqlite_backup_bytes": 128,
                "sqlite_backup_integrity": "failed",
                "rotated_logs": [],
                "retained_backups": [],
                "pruned_backups": [],
                "disk_free_bytes": 10_000_000_000,
                "disk_free_ratio": 0.9,
                "disk_status": "ok",
                "warnings": ["sqlite_backup_integrity:failed"],
            },
        )
    finally:
        store.close()

    client = TestClient(create_app(str(config_path)))
    body = client.get("/api/system/readiness").json()
    runtime_check = next(item for item in body["checks"] if item["id"] == "runtime_maintenance")

    assert runtime_check["status"] == "block"
    assert body["overall"] == "block"


def test_live_readiness_blocks_recent_deepseek_error_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured-placeholder")
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    config_path = tmp_path / "config.yaml"
    write_config(config_path, db_path, audit_path)
    config_text = config_path.read_text(encoding="utf-8").replace(
        "runtime:\n  dry_run: true",
        "runtime:\n  dry_run: false\n  execution_mode: live",
    )
    config_path.write_text(config_text, encoding="utf-8")
    store = SQLiteStore(str(db_path), str(audit_path))
    try:
        store.insert(
            "ai_decisions",
            {
                "symbol": "ETH/USDT:USDT",
                "reason_codes": ["deepseek_error:HTTPError", "fallback_conservative"],
                "brief_reason": "DeepSeek unavailable; using fallback.",
            },
            "ETH/USDT:USDT",
        )
    finally:
        store.close()

    client = TestClient(create_app(str(config_path)))
    body = client.get("/api/system/readiness").json()
    deepseek_check = next(item for item in body["checks"] if item["id"] == "deepseek")

    assert deepseek_check["status"] == "block"
    assert body["overall"] == "block"


def test_live_readiness_blocks_recent_deepseek_budget_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured-placeholder")
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    config_path = tmp_path / "config.yaml"
    write_config(config_path, db_path, audit_path)
    config_text = config_path.read_text(encoding="utf-8").replace(
        "runtime:\n  dry_run: true",
        "runtime:\n  dry_run: false\n  execution_mode: live",
    )
    config_path.write_text(config_text, encoding="utf-8")
    store = SQLiteStore(str(db_path), str(audit_path))
    try:
        store.insert(
            "ai_call_budget_events",
            {
                "symbol": "ETH/USDT:USDT",
                "call_type": "trading_cycle",
                "status": "failure",
                "reason": "deepseek_error:HTTPError",
            },
            "ETH/USDT:USDT",
        )
    finally:
        store.close()

    client = TestClient(create_app(str(config_path)))
    body = client.get("/api/system/readiness").json()
    budget_check = next(item for item in body["checks"] if item["id"] == "deepseek_budget")

    assert budget_check["status"] == "block"
    assert body["overall"] == "block"


def test_live_readiness_blocks_unconfirmed_console_password_strength(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_AUTH_DISABLED", "0")
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "admin-secret")
    monkeypatch.delenv("CONSOLE_PASSWORD_STRENGTH_CONFIRMED", raising=False)
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    config_path = tmp_path / "config.yaml"
    write_config(config_path, db_path, audit_path)
    config_text = config_path.read_text(encoding="utf-8").replace(
        "runtime:\n  dry_run: true",
        "runtime:\n  dry_run: false\n  execution_mode: live",
    )
    config_path.write_text(config_text, encoding="utf-8")

    client = TestClient(create_app(str(config_path)))
    login = client.post("/api/auth/login", json={"username": "admin", "password": "admin-secret"})
    assert login.status_code == 200
    body = client.get("/api/system/readiness").json()
    console_check = next(item for item in body["checks"] if item["id"] == "console_auth")

    assert console_check["status"] == "block"
    assert "password strength" in console_check["detail"]


def test_live_readiness_accepts_confirmed_console_auth_policy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_AUTH_DISABLED", "0")
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "admin-secret")
    monkeypatch.setenv("CONSOLE_PASSWORD_STRENGTH_CONFIRMED", "1")
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    config_path = tmp_path / "config.yaml"
    write_config(config_path, db_path, audit_path)
    config_text = config_path.read_text(encoding="utf-8").replace(
        "runtime:\n  dry_run: true",
        "runtime:\n  dry_run: false\n  execution_mode: live",
    )
    config_path.write_text(config_text, encoding="utf-8")

    client = TestClient(create_app(str(config_path)))
    login = client.post("/api/auth/login", json={"username": "admin", "password": "admin-secret"})
    assert login.status_code == 200
    body = client.get("/api/system/readiness").json()
    console_check = next(item for item in body["checks"] if item["id"] == "console_auth")

    assert console_check["status"] == "ok"


def test_console_basic_auth_blocks_when_configured(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_AUTH_DISABLED", "0")
    monkeypatch.setenv("CONSOLE_BASIC_USER", "admin")
    monkeypatch.setenv("CONSOLE_BASIC_PASSWORD", "secret")
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    client = TestClient(create_app(str(config_path)))

    blocked = client.get("/api/status")
    assert blocked.status_code == 401
    assert blocked.json()["detail"] == "auth_required"

    health = client.get("/api/health")
    assert health.status_code == 200


def test_console_basic_auth_accepts_valid_credentials(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_AUTH_DISABLED", "0")
    monkeypatch.setenv("CONSOLE_BASIC_USER", "admin")
    monkeypatch.setenv("CONSOLE_BASIC_PASSWORD", "secret")
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    token = base64.b64encode(b"admin:secret").decode("ascii")
    client = TestClient(create_app(str(config_path)))

    response = client.get("/api/status", headers={"Authorization": f"Basic {token}"})

    assert response.status_code == 200
    assert response.json()["execution_mode"] == "mock"


def test_console_account_rbac_replaces_operation_code_for_mutating_requests(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_AUTH_DISABLED", "0")
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "admin-secret")
    monkeypatch.setenv("CONSOLE_ACCOUNT1_PASSWORD", "account-secret")
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    client = TestClient(create_app(str(config_path)))

    body = {"operator_id": "tester", "symbols": ["ETH/USDT:USDT"]}
    blocked = client.post("/api/control/authorize", json=body)
    assert blocked.status_code == 401
    assert blocked.json()["detail"] == "auth_required"

    login = client.post("/api/auth/login", json={"username": "account1", "password": "account-secret"})
    assert login.status_code == 200
    wrong = client.post("/api/control/authorize", json=body)
    assert wrong.status_code == 403
    assert wrong.json()["detail"] == "permission_denied"

    leverage = client.post(
        "/api/execution/accounts/leverage",
        json={"operator_id": "tester", "account_slot": "trend", "max_leverage": 3.5},
    )
    assert leverage.status_code == 200
    assert leverage.json()["max_leverage"] == 3.5

    client.post("/api/auth/logout", json={})
    admin_login = client.post("/api/auth/login", json={"username": "admin", "password": "admin-secret"})
    assert admin_login.status_code == 200
    missing_confirm = client.post("/api/control/authorize", json=body)
    assert missing_confirm.status_code == 400
    assert "必须显式二次确认" in missing_confirm.json()["detail"]

    ok = client.post("/api/control/authorize", json={**body, "confirm_admin_action": True})
    assert ok.status_code == 200


def test_console_readonly_accounts_cannot_query_other_account_slots(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_AUTH_DISABLED", "0")
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "admin-secret")
    monkeypatch.setenv("CONSOLE_ACCOUNT1_PASSWORD", "account-secret")
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    write_config(config_path, db_path, audit_path, ["ETH/USDT:USDT"])
    store = SQLiteStore(str(db_path), str(audit_path))
    store.insert("orders", {"account_slot": "trend", "status": "filled"}, "ETH/USDT:USDT")
    store.insert("orders", {"account_slot": "follower", "status": "filled"}, "ETH/USDT:USDT")
    store.close()
    client = TestClient(create_app(str(config_path)))

    login = client.post("/api/auth/login", json={"username": "account1", "password": "account-secret"})
    assert login.status_code == 200

    assert client.get("/api/account/balance?account_slot=follower").status_code == 403
    assert client.get("/api/positions?account_slot=follower").status_code == 403
    assert client.get("/api/order-lifecycle?account_slot=follower").status_code == 403
    assert client.get("/api/audits/ai-position-tiers?account_slot=follower").status_code == 403

    own_orders = client.get("/api/orders").json()["items"]
    assert len(own_orders) == 1
    assert own_orders[0]["payload"]["account_slot"] == "trend"


def test_position_review_mode_control_is_admin_only_and_audited(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_AUTH_DISABLED", "0")
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "admin-secret")
    monkeypatch.setenv("CONSOLE_ACCOUNT1_PASSWORD", "account-secret")
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    write_config(config_path, db_path, audit_path, ["ETH/USDT:USDT"])
    client = TestClient(create_app(str(config_path)))

    account_login = client.post("/api/auth/login", json={"username": "account1", "password": "account-secret"})
    assert account_login.status_code == 200
    account_blocked = client.post(
        "/api/control/position-review",
        json={"operator_id": "account1", "mode": "shadow"},
    )
    assert account_blocked.status_code == 403
    assert account_blocked.json()["detail"] == "permission_denied"

    client.post("/api/auth/logout", json={})
    admin_login = client.post("/api/auth/login", json={"username": "admin", "password": "admin-secret"})
    assert admin_login.status_code == 200
    unsafe_live = client.post(
        "/api/control/position-review",
        json={"operator_id": "admin", "mode": "live_addon"},
    )
    assert unsafe_live.status_code == 400
    assert "必须显式确认" in unsafe_live.json()["detail"]

    shadow = client.post(
        "/api/control/position-review",
        json={"operator_id": "admin", "mode": "shadow"},
    )
    assert shadow.status_code == 200
    assert shadow.json()["enabled"] is True
    assert shadow.json()["mode"] == "shadow"
    status = client.get("/api/status").json()
    review_cfg = status["risk"]["position_review"]
    assert review_cfg["enabled"] is True
    assert review_cfg["mode"] == "shadow"

    live = client.post(
        "/api/control/position-review",
        json={"operator_id": "admin", "mode": "live_addon", "confirm_live_addon": True},
    )
    assert live.status_code == 200
    assert live.json()["mode"] == "live_addon"
    latest = SQLiteStore(str(db_path), str(audit_path)).fetch_latest("runtime_state", "position_review_control")
    assert latest is not None
    assert latest["payload"]["old_mode"] == "shadow"
    assert latest["payload"]["new_mode"] == "live_addon"
    assert latest["payload"]["confirm_live_addon"] is True


def test_console_auth_fails_closed_when_enabled_without_users(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_AUTH_DISABLED", "0")
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    client = TestClient(create_app(str(config_path)))

    session = client.get("/api/auth/session")
    assert session.status_code == 200
    assert session.json()["auth_required"] is True
    assert session.json()["auth_configured"] is False
    assert session.json()["authenticated"] is False

    blocked = client.get("/api/status")
    assert blocked.status_code == 503
    assert blocked.json()["detail"] == "console_auth_not_configured"

    login = client.post("/api/auth/login", json={"username": "admin", "password": "secret"})
    assert login.status_code == 503
    assert login.json()["detail"] == "console_auth_not_configured"


def test_agent_gateway_requires_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AGENT_GATEWAY_TOKEN", raising=False)
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    client = TestClient(create_app(str(config_path)))

    response = client.get("/api/agent/v1/health")

    assert response.status_code == 503
    assert "AGENT_GATEWAY_TOKEN" in response.json()["detail"]


def test_agent_gateway_strategy_profiles_and_paper_backtest(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_GATEWAY_TOKEN", "test-agent-token")
    monkeypatch.setenv("AGENT_GATEWAY_AGENT_ID", "test-agent")
    monkeypatch.setenv("AGENT_GATEWAY_SCOPES", "R,B")

    async def fake_backtest_candles(_market, body):
        prices = [100.0 + idx * 0.5 for idx in range(body.limit)]
        return pd.DataFrame(
            {
                "timestamp": pd.date_range("2026-01-01", periods=body.limit, freq="h"),
                "open": prices,
                "high": [price + 2.0 for price in prices],
                "low": [price - 2.0 for price in prices],
                "close": prices,
                "volume": [1000.0] * body.limit,
            }
        )

    monkeypatch.setattr(server, "_fetch_backtest_candles", fake_backtest_candles)
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    client = TestClient(create_app(str(config_path)))
    headers = {"Authorization": "Bearer test-agent-token"}

    health = client.get("/api/agent/v1/health", headers=headers)
    assert health.status_code == 200
    assert health.json()["paper_only"] is True

    profiles = client.get("/api/agent/v1/strategy-profiles", headers=headers)
    assert profiles.status_code == 200
    assert profiles.json()["items"][0]["execution_contract"]["same_direction_add"] == "blocked"

    missing_key = client.post(
        "/api/agent/v1/backtests",
        headers=headers,
        json={"symbol": "ETH/USDT:USDT", "limit": 140},
    )
    assert missing_key.status_code == 400

    started = client.post(
        "/api/agent/v1/backtests",
        headers={**headers, "Idempotency-Key": "stable-agent-backtest-1"},
        json={"symbol": "ETH/USDT:USDT", "limit": 140, "data_source": "binance", "leverage": 4},
    )
    assert started.status_code == 200
    job_id = started.json()["job_id"]
    assert job_id.startswith("agt_")

    replay = client.post(
        "/api/agent/v1/backtests",
        headers={**headers, "Idempotency-Key": "stable-agent-backtest-1"},
        json={"symbol": "ETH/USDT:USDT", "limit": 140, "data_source": "binance", "leverage": 4},
    )
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True

    job = client.get(f"/api/agent/v1/backtests/{job_id}", headers=headers)
    assert job.status_code == 200
    assert job.json()["source"] == "agent_gateway"


def test_console_report_switch(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    client = TestClient(create_app(str(config_path)))

    response = client.post(
        "/api/control/enable-report",
        json={"operator_id": "tester", "symbols": ["ETH/USDT:USDT"]},
    )
    assert response.status_code == 200
    status = client.get("/api/status").json()
    assert status["report_symbols"] == ["ETH/USDT:USDT"]


def test_market_symbols_only_returns_configured_symbols(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    client = TestClient(create_app(str(config_path)))

    response = client.get("/api/markets/symbols")
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["symbol"] for item in items] == ["ETH/USDT:USDT"]
    assert "ARB/USDT:USDT" not in {item["symbol"] for item in items}


def test_market_ticker_returns_realtime_price_payload(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])

    async def fake_fetch_ticker(self, symbol: str, source: str = "auto") -> dict[str, object]:
        assert symbol == "ETH/USDT:USDT"
        assert source == "binance"
        return {
            "symbol": symbol,
            "source": "binance",
            "last": 2013.35,
            "bid": 2013.30,
            "ask": 2013.40,
            "timestamp": "2026-05-31T00:00:00+00:00",
            "warning": "",
        }

    monkeypatch.setattr(server.MarketDataClient, "fetch_ticker", fake_fetch_ticker)
    client = TestClient(create_app(str(config_path)))

    response = client.get("/api/market/ticker?symbol=ETH/USDT:USDT&source=binance")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "binance"
    assert body["last"] == 2013.35


def test_market_ticker_accepts_bybit_source(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])

    async def fake_fetch_ticker(self, symbol: str, source: str = "auto") -> dict[str, object]:
        assert symbol == "ETH/USDT:USDT"
        assert source == "bybit"
        return {
            "symbol": symbol,
            "source": "bybit",
            "last": 2014.35,
            "bid": 2014.30,
            "ask": 2014.40,
            "timestamp": "2026-05-31T00:00:00+00:00",
            "warning": "",
        }

    monkeypatch.setattr(server.MarketDataClient, "fetch_ticker", fake_fetch_ticker)
    client = TestClient(create_app(str(config_path)))

    response = client.get("/api/market/ticker?symbol=ETH/USDT:USDT&source=bybit")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "bybit"
    assert body["last"] == 2014.35


def test_console_cors_rejects_non_local_origins(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    client = TestClient(create_app(str(config_path)))

    local = client.options(
        "/api/status",
        headers={"Origin": "http://127.0.0.1:5173", "Access-Control-Request-Method": "GET"},
    )
    hostile = client.options(
        "/api/status",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )

    assert local.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    assert "access-control-allow-origin" not in hostile.headers


def test_news_latest_auto_refreshes_when_cache_is_empty(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])

    async def fake_collect(config_path_arg: str) -> NewsDigest:
        assert config_path_arg == str(config_path)
        return NewsDigest(
            generated_at=datetime.now(UTC),
            items=[
                NewsItem(
                    title="美联储表示利率路径仍取决于通胀数据",
                    source="美联储",
                    summary="美联储表示，后续利率路径仍取决于通胀数据和就业市场变化。",
                    credibility=0.95,
                    category="macro",
                )
            ],
        )

    monkeypatch.setattr(server, "_collect_news_digest", fake_collect)
    client = TestClient(create_app(str(config_path)))

    response = client.get("/api/news/latest?limit=4&auto_refresh=true")

    assert response.status_code == 200
    body = response.json()
    assert body["stale"] is False
    assert body["timeline"][0]["source"] == "美联储"
    assert "通胀" in body["timeline"][0]["summary"]


def test_readiness_warns_when_news_cache_exceeds_soft_window(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    write_config(config_path, db_path, audit_path, ["ETH/USDT:USDT"])
    store = SQLiteStore(str(db_path), str(audit_path))
    row_id = store.insert(
        "news_summaries",
        {
            "summary": "old but not hard-expired news",
            "items": [{"title": "old headline", "source": "test"}],
            "warnings": [],
        },
    )
    stale_at = (datetime.now(UTC) - timedelta(minutes=90)).isoformat()
    with store._lock:  # noqa: SLF001
        store.conn.execute("UPDATE news_summaries SET created_at = ? WHERE id = ?", (stale_at, row_id))
        store.conn.commit()

    client = TestClient(create_app(str(config_path)))
    body = client.get("/api/system/readiness").json()
    news_check = next(item for item in body["checks"] if item["id"] == "news")

    assert news_check["status"] == "warn"
    assert news_check["age_minutes"] >= 89
    assert any(alert["event"] == "news_stale" for alert in body["runtime_alerts"])


def test_live_readiness_blocks_unresolved_order_lifecycle_even_after_newer_ok_event(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    write_config(config_path, db_path, audit_path, ["ETH/USDT:USDT"])
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace("  dry_run: true", "  dry_run: false\n  execution_mode: live"),
        encoding="utf-8",
    )
    store = SQLiteStore(str(db_path), str(audit_path))
    store.insert(
        "order_lifecycle",
        {
            "client_order_id": "unknown_entry_1",
            "symbol": "ETH/USDT:USDT",
            "status": "unknown",
            "order_type": "market",
            "account_slot": "trend",
            "reason": "exchange_submit_exception_state_unknown",
        },
        "ETH/USDT:USDT",
    )
    store.insert(
        "order_lifecycle",
        {
            "client_order_id": "healthy_order_2",
            "symbol": "ETH/USDT:USDT",
            "status": "filled",
            "order_type": "market",
            "account_slot": "trend",
            "reason": "later_normal_event",
        },
        "ETH/USDT:USDT",
    )

    body = TestClient(create_app(str(config_path))).get("/api/system/readiness").json()
    order_check = next(item for item in body["checks"] if item["id"] == "order_lifecycle")

    assert order_check["status"] == "block"
    assert body["unresolved_order_lifecycle"][0]["client_order_id"] == "unknown_entry_1"
    assert any(alert["event"] == "order_lifecycle_problem" for alert in body["runtime_alerts"])


def test_order_lifecycle_issue_is_resolved_by_newer_same_client_terminal_event(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    write_config(config_path, db_path, audit_path, ["ETH/USDT:USDT"])
    store = SQLiteStore(str(db_path), str(audit_path))
    for status in ["unknown", "filled"]:
        store.insert(
            "order_lifecycle",
            {
                "client_order_id": "recoverable_order_1",
                "symbol": "ETH/USDT:USDT",
                "status": status,
                "order_type": "market",
                "account_slot": "trend",
                "reason": f"pytest_{status}",
            },
            "ETH/USDT:USDT",
        )

    body = TestClient(create_app(str(config_path))).get("/api/system/readiness").json()
    order_check = next(item for item in body["checks"] if item["id"] == "order_lifecycle")

    assert order_check["status"] == "ok"
    assert body["unresolved_order_lifecycle"] == []


def test_live_readiness_blocks_when_news_cache_exceeds_hard_window(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    write_config(config_path, db_path, audit_path, ["ETH/USDT:USDT"])
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace("  dry_run: true", "  dry_run: false\n  execution_mode: live"),
        encoding="utf-8",
    )
    store = SQLiteStore(str(db_path), str(audit_path))
    row_id = store.insert(
        "news_summaries",
        {
            "summary": "hard-expired news",
            "items": [{"title": "stale headline", "source": "test"}],
            "warnings": [],
        },
    )
    stale_at = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
    with store._lock:  # noqa: SLF001
        store.conn.execute("UPDATE news_summaries SET created_at = ? WHERE id = ?", (stale_at, row_id))
        store.conn.commit()

    client = TestClient(create_app(str(config_path)))
    body = client.get("/api/system/readiness").json()
    news_check = next(item for item in body["checks"] if item["id"] == "news")

    assert news_check["status"] == "block"
    assert body["overall"] == "block"
    assert any(alert["event"] == "news_stale" and alert["level"] == "critical" for alert in body["runtime_alerts"])


def test_news_latest_compact_omits_heavy_rows(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])

    async def fake_collect(config_path_arg: str) -> NewsDigest:
        assert config_path_arg == str(config_path)
        return NewsDigest(
            generated_at=datetime.now(UTC),
            items=[
                NewsItem(
                    title="Fed says policy path remains data dependent",
                    source="Fed",
                    summary="Fresh macro update for compact console rendering.",
                    credibility=0.95,
                    important=True,
                    category="macro",
                )
            ],
        )

    monkeypatch.setattr(server, "_collect_news_digest", fake_collect)
    client = TestClient(create_app(str(config_path)))

    response = client.get("/api/news/latest?limit=4&auto_refresh=true&compact=true&max_age_minutes=2")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["source"] == "fresh_refresh"
    assert body["source_status"] == "fresh"
    assert body["refreshed"] is True
    assert body["items_count"] == 1
    assert body["items"] == []
    assert body["latest_digest"] == {}
    assert body["timeline"][0]["title"] == "Fed says policy path remains data dependent"
    assert body["timeline"][0]["important"] is True


def test_news_latest_defaults_to_lightweight_rows(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    write_config(config_path, db_path, tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    client = TestClient(create_app(str(config_path)))
    store = server._ctx(client.app).store
    assert store is not None
    heavy_summary = "macro context " * 500
    store.insert(
        "news_summaries",
        {
            "generated_at": datetime.now(UTC).isoformat(),
            "summary": heavy_summary,
            "items": [
                {
                    "title": "Fed keeps policy restrictive",
                    "summary": "Large cached detail " * 1000,
                    "source": "Fed",
                }
            ],
            "warnings": [],
        },
    )

    response = client.get("/api/news/latest?limit=1&auto_refresh=false")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["source"] == "news_cache"
    assert body["source_status"] == "fresh"
    assert body["refreshed"] is False
    assert body["items_count"] == 1
    assert body["digest_summary"].startswith("macro context")
    assert body["timeline"][0]["title"] == "Fed keeps policy restrictive"
    assert "items" not in body["latest_digest"]
    assert body["latest_digest"]["item_count"] == 1
    assert len(body["latest_digest"]["summary"]) <= 1500
    assert len(body["summary"]) <= 1500
    assert "Large cached detail" not in json.dumps(body["items"], ensure_ascii=False)


def test_news_latest_can_include_full_payload_when_explicit(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    write_config(config_path, db_path, tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    client = TestClient(create_app(str(config_path)))
    store = server._ctx(client.app).store
    assert store is not None
    store.insert(
        "news_summaries",
        {
            "generated_at": datetime.now(UTC).isoformat(),
            "summary": "full payload test",
            "items": [{"title": "Detailed row", "summary": "Full payload detail", "source": "test"}],
            "warnings": [],
        },
    )

    response = client.get("/api/news/latest?limit=1&auto_refresh=false&include_payload=true")

    assert response.status_code == 200
    body = response.json()
    assert body["latest_digest"]["items"][0]["summary"] == "Full payload detail"
    assert body["items"][0]["payload"]["items"][0]["title"] == "Detailed row"


def test_news_latest_limits_timeline_payload_size(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trader.sqlite3"
    write_config(config_path, db_path, tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    client = TestClient(create_app(str(config_path)))
    store = server._ctx(client.app).store
    assert store is not None
    store.insert(
        "news_summaries",
        {
            "generated_at": datetime.now(UTC).isoformat(),
            "summary": "timeline trim test",
            "items": [
                {"title": f"Headline {idx}", "summary": "Long detailed news " * 200, "source": "test"}
                for idx in range(10)
            ],
            "warnings": [],
        },
    )

    response = client.get("/api/news/latest?limit=3&auto_refresh=false")

    assert response.status_code == 200
    body = response.json()
    assert len(body["timeline"]) == 3
    assert len(body["timeline"][0]["summary"]) <= 900
    assert "raw_summary" not in body["timeline"][0]


def test_news_row_age_detects_stale_cache() -> None:
    old = (datetime.now(UTC) - timedelta(minutes=90)).isoformat()
    row = {"created_at": old, "payload": {"generated_at": old}}

    assert server._news_row_is_stale(row, max_age_minutes=65) is True


def test_news_row_detects_old_template_payload_even_when_recent() -> None:
    now = datetime.now(UTC).isoformat()
    row = {
        "created_at": now,
        "payload": {
            "generated_at": now,
            "items": [
                {
                    "title": "美联储发布宏观金融相关快讯",
                    "summary": "美联储消息涉及利率、通胀、美元、美债或经济数据变化",
                }
            ],
        },
    }

    assert server._news_row_is_stale(row, max_age_minutes=65) is True


def test_news_row_detects_payload_without_raw_trace_fields() -> None:
    now = datetime.now(UTC).isoformat()
    row = {
        "created_at": now,
        "payload": {
            "generated_at": now,
            "items": [{"title": "美联储利率消息", "summary": "细节充分但缺少原文追溯字段"}],
        },
    }

    assert server._news_row_is_stale(row, max_age_minutes=65) is True


def test_news_response_repairs_cached_mojibake() -> None:
    target = b"\xe9\x87\x91\xe5\x8d\x81\xe6\x95\xb0\xe6\x8d\xae".decode("utf-8")
    broken = target.encode("utf-8").decode("latin1")
    double_broken = broken.encode("utf-8").decode("latin1")
    row = {
        "created_at": datetime.now(UTC).isoformat(),
        "payload": {
            "generated_at": datetime.now(UTC).isoformat(),
            "items": [{"title": double_broken, "summary": double_broken, "source": double_broken}],
            "warnings": ["rss_error:HTTPError"],
        },
    }

    response = server._news_latest_response([row], None, max_age_minutes=65)

    assert response["timeline"][0]["title"] == target
    assert response["warnings"] == []


def test_news_response_hides_internal_context_warnings() -> None:
    row = {
        "created_at": datetime.now(UTC).isoformat(),
        "payload": {
            "generated_at": datetime.now(UTC).isoformat(),
            "items": [{"title": "真实快讯", "summary": "市场新闻", "source": "jin10"}],
            "warnings": [
                "daily_news_flash_context_attached",
                "market_background_uses_decayed_events",
                "realtime_news_window_attached",
                "market_background_attached",
                "news_context_48h_attached",
                "jin10_error:temporary timeout",
            ],
        },
    }

    response = server._news_latest_response([row], None, max_age_minutes=65)

    assert response["warnings"] == ["jin10_error:temporary timeout"]
    assert response["latest_digest"]["warnings"] == ["jin10_error:temporary timeout"]


def test_console_strategy_lab_activate_and_custom_backtest(tmp_path: Path, monkeypatch) -> None:
    from ai_quant_trader.strategy import lab

    monkeypatch.setattr(lab, "STRATEGY_LAB_DIR", tmp_path / "strategy_lab")
    monkeypatch.setattr(lab, "ACTIVE_STRATEGY_PATH", tmp_path / "strategy_lab" / "active.json")
    async def fake_backtest_candles(_market, body):
        prices = [100.0 + idx * 0.1 for idx in range(body.limit)]
        return pd.DataFrame(
            {
                "timestamp": list(range(body.limit)),
                "open": prices,
                "high": [price + 1.0 for price in prices],
                "low": [price - 1.0 for price in prices],
                "close": prices,
                "volume": [1000.0] * body.limit,
            }
        )

    monkeypatch.setattr(server, "_fetch_backtest_candles", fake_backtest_candles)
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    client = TestClient(create_app(str(config_path)))
    code = """
def generate_signal(candles, position, context):
    close = float(candles["close"].iloc[-1])
    prev = float(candles["close"].iloc[-2])
    if close > prev:
        return {"action": "LONG", "reason": "上涨", "signal_strength": 0.8}
    return {"action": "HOLD", "reason": "等待"}
"""

    saved = client.post("/api/strategy-lab/save", json={"name": "网页策略", "code": code, "description": "测试"})
    assert saved.status_code == 200
    strategy_id = saved.json()["item"]["id"]

    activated = client.post(
        "/api/strategy-lab/activate",
        json={"strategy_id": strategy_id, "symbols": ["ETH/USDT:USDT"], "operator_id": "tester"},
    )
    assert activated.status_code == 200
    assert activated.json()["active"]["id"] == strategy_id

    versions = client.get("/api/strategy-lab/versions")
    assert versions.status_code == 200
    assert versions.json()["active"]["id"] == strategy_id

    backtest = client.post(
        "/api/backtest/custom",
        json={"strategy_id": strategy_id, "symbol": "ETH/USDT:USDT", "limit": 150, "warmup": 2},
    )
    assert backtest.status_code == 200
    assert backtest.json()["result"]["strategy_id"] == strategy_id


def test_strategy_lab_preserves_risk_filter_category(tmp_path: Path, monkeypatch) -> None:
    from ai_quant_trader.strategy import lab

    monkeypatch.setattr(lab, "STRATEGY_LAB_DIR", tmp_path / "strategy_lab")
    monkeypatch.setattr(lab, "ACTIVE_STRATEGY_PATH", tmp_path / "strategy_lab" / "active.json")
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    client = TestClient(create_app(str(config_path)))
    code = """
def generate_signal(candles, position, context):
    return {"action": "HOLD", "reason": "risk gate", "signal_strength": 0.0}
"""

    saved = client.post(
        "/api/strategy-lab/save",
        json={"name": "risk gate", "category": "risk_filter", "code": code},
    )

    assert saved.status_code == 200
    item = saved.json()["item"]
    assert item["category"] == "risk_filter"
    assert item["category_label"] == "风控过滤器"
