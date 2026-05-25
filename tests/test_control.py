from __future__ import annotations

import yaml

from ai_quant_trader.core.control import RuntimeControlManager
from ai_quant_trader.storage.sqlite import SQLiteStore


def test_symbol_specific_parameter_proposal(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "strategy": {"trend": {"ema_length": 89, "kc_length": 20, "kc_scalar": 2.8, "vma_length": 20, "atr_length": 14, "volume_multiple": 1.5}},
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
                "strategy": {"trend": {"ema_length": 89, "kc_length": 20, "kc_scalar": 2.8, "vma_length": 20, "atr_length": 14, "volume_multiple": 1.5}},
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
