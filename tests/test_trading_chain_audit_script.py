from __future__ import annotations

import json
import subprocess
import sys

from scripts import trading_chain_audit


def test_trading_chain_audit_core_cases_cover_required_links() -> None:
    case_ids = {case.id for case in trading_chain_audit.CORE_CASES}

    assert "strategy_ai_risk_order_stop_exit" in case_ids
    assert "live_addon_replaces_net_position_stop" in case_ids
    assert "software_atr_stop_mirrors_follower" in case_ids
    assert "native_stop_fill_mirrors_follower" in case_ids
    assert "unknown_submit_blocks_blind_retry" in case_ids
    assert "unresolved_order_lifecycle_blocks_readiness" in case_ids
    assert "native_stop_unknown_requires_manual_gate" in case_ids
    assert "native_stop_exchange_verification_required" in case_ids
    assert "native_stop_amount_covers_net_position" in case_ids
    assert "native_stop_trigger_price_matches_atr_stop" in case_ids
    assert "stale_trend_state_repair_requires_terminal_stop" in case_ids

    extended_ids = {case.id for case in trading_chain_audit.selected_cases("extended")}
    assert "follower_entry_failure_fail_closed" in extended_ids
    assert "follower_exit_failure_fail_closed" in extended_ids
    assert "follower_order_status_refresh_failure_fail_closed" in extended_ids


def test_trading_chain_audit_invokes_pytest_and_writes_json(tmp_path, monkeypatch, capsys) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="11 passed\n", stderr="")

    monkeypatch.setattr(trading_chain_audit.subprocess, "run", fake_run)
    report_path = tmp_path / "audit" / "report.json"

    code = trading_chain_audit.main(["--mode", "core", "--json-out", str(report_path), "--", "-vv"])

    assert code == 0
    assert calls
    command = calls[0]
    assert command[:3] == [sys.executable, "-m", "pytest"]
    assert "-vv" in command
    assert any("test_trading_cycle_opens_places_stop_then_exits_and_cancels_stop" in item for item in command)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["case_count"] == len(trading_chain_audit.CORE_CASES)
    assert payload["stdout_tail"] == "11 passed\n"
    console_payload = json.loads(capsys.readouterr().out)
    assert console_payload["ok"] is True
