from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

from ai_quant_trader.api import server
from ai_quant_trader.api.server import create_app
from ai_quant_trader.core.models import NewsDigest, NewsItem
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
    ema_length: 89
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
    assert "trend_confirmation_score" in policy["score_fields"]

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
        "worker_heartbeat",
        "runtime_maintenance",
    }
    assert "exchange_safety" in readiness_body
    assert "latest_order_lifecycle" in readiness_body
    assert "latest_data_health" in readiness_body
    assert "latest_ai_drift" in readiness_body
    assert "latest_news_risk_review" in readiness_body
    assert "latest_ai_budget" in readiness_body
    assert "latest_worker_heartbeats" in readiness_body
    assert "latest_maintenance" in readiness_body

    metrics = client.get("/api/system/metrics")
    assert metrics.status_code == 200
    metrics_body = metrics.json()
    assert "readiness" in metrics_body
    assert "worker_heartbeats" in metrics_body
    assert "storage" in metrics_body

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
    assert [item["slot"] for item in account_body["items"]] == ["trend", "range"]
    assert account_body["items"][0]["live_routing"] == "blocked_missing_credentials"

    channels = client.get("/api/strategy/channels")
    assert channels.status_code == 200
    channel_body = channels.json()
    assert [item["channel"] for item in channel_body["items"]] == ["trend", "range"]
    assert channel_body["items"][0]["account_slot"] == "trend"
    assert channel_body["items"][0]["executable"] is True
    assert channel_body["items"][1]["account_slot"] == "range"
    assert channel_body["items"][1]["executable"] is False

    dense = client.get("/api/dense-zones/latest?symbol=ETH%2FUSDT%3AUSDT")
    assert dense.status_code == 200
    assert dense.json()["item"] is None


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


def test_console_basic_auth_blocks_when_configured(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_BASIC_USER", "admin")
    monkeypatch.setenv("CONSOLE_BASIC_PASSWORD", "secret")
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    client = TestClient(create_app(str(config_path)))

    blocked = client.get("/api/status")
    assert blocked.status_code == 401
    assert blocked.headers["www-authenticate"] == 'Basic realm="AI Quant Console"'

    health = client.get("/api/health")
    assert health.status_code == 200


def test_console_basic_auth_accepts_valid_credentials(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_BASIC_USER", "admin")
    monkeypatch.setenv("CONSOLE_BASIC_PASSWORD", "secret")
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    token = base64.b64encode(b"admin:secret").decode("ascii")
    client = TestClient(create_app(str(config_path)))

    response = client.get("/api/status", headers={"Authorization": f"Basic {token}"})

    assert response.status_code == 200
    assert response.json()["execution_mode"] == "mock"


def test_console_operation_code_blocks_mutating_requests_when_enabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_REQUIRE_OPERATION_CODE", "1")
    monkeypatch.setenv("CONSOLE_OPERATION_CODE", "yx")
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    client = TestClient(create_app(str(config_path)))

    body = {"operator_id": "tester", "symbols": ["ETH/USDT:USDT"]}
    blocked = client.post("/api/control/authorize", json=body)
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "operation_code_required"

    wrong = client.post("/api/control/authorize", json=body, headers={"X-Operation-Code": "wrong"})
    assert wrong.status_code == 403

    ok = client.post("/api/control/authorize", json=body, headers={"X-Operation-Code": "yx"})
    assert ok.status_code == 200


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
                    category="macro",
                )
            ],
        )

    monkeypatch.setattr(server, "_collect_news_digest", fake_collect)
    client = TestClient(create_app(str(config_path)))

    response = client.get("/api/news/latest?limit=4&auto_refresh=true&compact=true")

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["latest_digest"] == {}
    assert body["timeline"][0]["title"] == "Fed says policy path remains data dependent"


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
