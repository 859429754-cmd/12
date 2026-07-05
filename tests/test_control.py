from __future__ import annotations

import asyncio
from typing import Any

import yaml

from ai_quant_trader.core.control import RuntimeControlManager
from ai_quant_trader.storage.sqlite import SQLiteStore
from ai_quant_trader.optimizer.proposals import StrategyOptimizer


class FakeOptimizationBrain:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def propose_optimization(self, snapshot: dict[str, Any], days: int) -> dict[str, Any]:
        self.calls.append({"snapshot": snapshot, "days": days})
        return {
            "summary": "保持参数，等待更多样本。",
            "logic_suggestions": [],
            "parameter_changes": [],
            "expected_effect": "无",
            "risk_note": "",
        }


def test_symbol_specific_parameter_proposal(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "strategy": {"trend": {"kc_length": 20, "kc_scalar": 2.8, "vma_length": 20, "atr_length": 14, "volume_multiple": 1.5}},
                "risk": {"min_confidence_to_trade": 0.55, "ai_full_size_confidence": 0.75},
                "runtime": {"dry_run": True},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    store = SQLiteStore(str(tmp_path / "test.sqlite3"), str(tmp_path / "audit.jsonl"))
    try:
        manager = RuntimeControlManager(store, str(config_path))
        proposal_id = manager.create_param_proposal("把BTC的kc倍数调到2.5", "admin", ["BTC/USDT:USDT"])
        row = store.fetch_by_id("optimization_proposals", proposal_id)
        assert row is not None
        changes = row["payload"]["changes"]
        assert "symbol_params.BTC/USDT:USDT.kc_scalar" in changes
        assert changes["symbol_params.BTC/USDT:USDT.kc_scalar"]["new"] == 2.5
    finally:
        store.close()


def test_atr_stop_multiple_defaults_and_can_be_proposed(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "strategy": {"trend": {"kc_length": 20, "kc_scalar": 2.8, "vma_length": 20, "atr_length": 14, "volume_multiple": 1.5}},
                "risk": {"min_confidence_to_trade": 0.55, "ai_full_size_confidence": 0.75},
                "runtime": {"dry_run": True},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    store = SQLiteStore(str(tmp_path / "test.sqlite3"), str(tmp_path / "audit.jsonl"))
    try:
        manager = RuntimeControlManager(store, str(config_path))
        assert manager.effective_symbol_params(manager.read_config(), "")["atr_stop_multiple"] == 1.5
        proposal_id = manager.create_param_proposal("ETH atr_stop_multiple调到3.0", "admin", ["ETH/USDT:USDT"])
        row = store.fetch_by_id("optimization_proposals", proposal_id)
        assert row is not None
        changes = row["payload"]["changes"]
        assert changes["symbol_params.ETH/USDT:USDT.atr_stop_multiple"]["new"] == 3.0
    finally:
        store.close()


def test_runtime_control_defaults_match_live_trend_contract(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("strategy:\n  trend: {}\n", encoding="utf-8")
    store = SQLiteStore(str(tmp_path / "test.sqlite3"), str(tmp_path / "audit.jsonl"))
    try:
        manager = RuntimeControlManager(store, str(config_path))
        params = manager.effective_symbol_params(manager.read_config(), "ETH/USDT:USDT")
        assert params["kc_length"] == 20
        assert params["kc_scalar"] == 2.8
        assert params["vma_length"] == 20
        assert params["atr_length"] == 14
        assert params["atr_stop_multiple"] == 1.5
        assert params["volume_multiple"] == 2.5
        assert params["position_fraction"] == 1.0
        assert params["momentum_filter"] == "kdj"
        assert params["kdj_length"] == 9
        assert params["kdj_k_smooth"] == 3
        assert params["kdj_d_smooth"] == 3
    finally:
        store.close()


def test_symbol_strategy_profile_can_be_disabled(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "strategy": {"trend": {"kc_length": 20, "kc_scalar": 2.8}},
                "symbol_params": {
                    "ETH/USDT:USDT": {"enabled": True, "profile_name": "eth_profile"},
                    "BTC/USDT:USDT": {"enabled": False, "profile_name": "btc_research_only"},
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    store = SQLiteStore(str(tmp_path / "test.sqlite3"), str(tmp_path / "audit.jsonl"))
    try:
        manager = RuntimeControlManager(store, str(config_path))
        config = manager.read_config()
        eth = manager.trend_config_for_symbol(config, "ETH/USDT:USDT")
        btc = manager.trend_config_for_symbol(config, "BTC/USDT:USDT")

        assert eth.enabled is True
        assert eth.profile_name == "eth_profile"
        assert btc.enabled is False
        assert btc.profile_name == "btc_research_only"
    finally:
        store.close()


def test_major_news_only_state_is_persisted(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "test.sqlite3"), str(tmp_path / "audit.jsonl"))
    try:
        manager = RuntimeControlManager(store, str(tmp_path / "config.yaml"))
        state = manager.load_state(["ETH/USDT:USDT"])
        manager.set_major_news_only(state, True, "admin")
        loaded = manager.load_state(["ETH/USDT:USDT"])
        assert loaded.major_news_only is True
    finally:
        store.close()


def test_runtime_state_ignores_deepseek_credential_rows(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "test.sqlite3"), str(tmp_path / "audit.jsonl"))
    try:
        manager = RuntimeControlManager(store, str(tmp_path / "config.yaml"))
        state = manager.load_state(["ETH/USDT:USDT"])
        manager.authorize_opening(state, ["ETH/USDT:USDT"], "admin", dry_run=False)

        store.insert(
            "runtime_state",
            {"active_label": "backup", "credentials": {"backup": {"status": "available"}}, "reason": "backup_success"},
            symbol="deepseek_credentials",
        )

        loaded = manager.load_state(["ETH/USDT:USDT"])
        assert loaded.opening_paused is False
        assert loaded.enabled_symbols == {"ETH/USDT:USDT"}
    finally:
        store.close()


def test_ai_optimization_snapshot_is_compact_and_budget_tagged(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "ai": {"call_budget_enabled": True, "max_calls_per_hour": 10, "max_calls_per_day": 100},
                "strategy": {"trend": {"kc_length": 20, "kc_scalar": 2.8}},
                "risk": {"min_confidence_to_trade": 0.55},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    store = SQLiteStore(str(tmp_path / "test.sqlite3"), str(tmp_path / "audit.jsonl"))
    try:
        for index in range(40):
            store.insert(
                "orders",
                {
                    "symbol": "ETH/USDT:USDT",
                    "side": "buy",
                    "amount": index + 1,
                    "price": 1000 + index,
                    "status": "closed",
                    "raw": {"huge": "x" * 10000},
                    "metadata": {"position_tier": "strong", "position_scale": 0.75, "account_slot": "trend"},
                },
                symbol="ETH/USDT:USDT",
            )
            store.insert(
                "ai_decisions",
                {
                    "symbol": "ETH/USDT:USDT",
                    "regime": "trend",
                    "direction": "long",
                    "confidence": 0.7,
                    "position_tier": "strong",
                    "position_scale": 0.75,
                    "brief_reason": "a" * 1000,
                    "full_prompt": "y" * 10000,
                    "reason_codes": [f"code_{n}" for n in range(20)],
                },
                symbol="ETH/USDT:USDT",
            )
        brain = FakeOptimizationBrain()
        optimizer = StrategyOptimizer(store, brain, RuntimeControlManager(store, str(config_path)))  # type: ignore[arg-type]
        optimizer_id, proposal = asyncio.run(optimizer.create_ai_proposal(30, "admin"))
        snapshot = brain.calls[0]["snapshot"]
        budget = store.fetch_payloads("ai_call_budget_events", symbol="ai_optimization", limit=1)[0]["payload"]

        assert optimizer_id > 0
        assert proposal["source"] == "deepseek"
        assert budget["call_type"] == "optimization_proposal"
        assert budget["status"] == "success"
        assert len(snapshot["recent_orders"]) == 30
        assert len(snapshot["recent_ai_decisions"]) == 30
        assert "raw" not in snapshot["recent_orders"][0]
        assert "full_prompt" not in snapshot["recent_ai_decisions"][0]
        assert len(snapshot["recent_ai_decisions"][0]["brief_reason"]) <= 240
        assert len(snapshot["recent_ai_decisions"][0]["reason_codes"]) == 8
    finally:
        store.close()


def test_ai_optimization_budget_block_avoids_deepseek_call(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {"ai": {"call_budget_enabled": True, "max_calls_per_hour": 1, "max_calls_per_day": 100}},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    store = SQLiteStore(str(tmp_path / "test.sqlite3"), str(tmp_path / "audit.jsonl"))
    try:
        store.insert(
            "ai_call_budget_events",
            {"symbol": "ETH/USDT:USDT", "call_type": "trading_cycle", "status": "attempt", "reason": "reserved"},
            symbol="ETH/USDT:USDT",
        )
        brain = FakeOptimizationBrain()
        optimizer = StrategyOptimizer(store, brain, RuntimeControlManager(store, str(config_path)))  # type: ignore[arg-type]
        _, proposal = asyncio.run(optimizer.create_ai_proposal(30, "admin"))

        assert brain.calls == []
        assert proposal["changes"] == {}
        assert proposal["risk_note"] == "deepseek_budget_blocked:hourly_limit_exceeded"
    finally:
        store.close()
