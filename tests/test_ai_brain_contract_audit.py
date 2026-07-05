from __future__ import annotations

import json
import subprocess
import sys

from scripts import ai_brain_contract_audit


def test_ai_brain_contract_core_cases_cover_required_contracts() -> None:
    case_ids = {case.id for case in ai_brain_contract_audit.selected_cases("core")}

    assert "stable_prompt_contract" in case_ids
    assert "five_score_normalization" in case_ids
    assert "quota_failure_sticks_to_backup" in case_ids
    assert "budget_hourly_limit_blocks" in case_ids
    assert "runtime_state_ignores_credentials" in case_ids
    assert "hybrid_subjective_lift_guard" in case_ids
    assert "major_news_conflict_hard_block" in case_ids


def test_ai_brain_contract_extended_cases_include_market_leader_and_caps() -> None:
    core_ids = {case.id for case in ai_brain_contract_audit.selected_cases("core")}
    extended_ids = {case.id for case in ai_brain_contract_audit.selected_cases("extended")}

    assert core_ids < extended_ids
    assert "btc_bearish_aligns_eth_short" in extended_ids
    assert "eth_rotation_not_false_conflict" in extended_ids
    assert "aligned_extreme_event_caps_weak" in extended_ids


def test_ai_brain_contract_writes_json_and_returns_success(tmp_path, monkeypatch, capsys) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok sk-1234567890abcdef\n", stderr="")

    monkeypatch.setattr(ai_brain_contract_audit.subprocess, "run", fake_run)
    report_path = tmp_path / "audit" / "ai-brain.json"

    code = ai_brain_contract_audit.main(["--mode", "core", "--json-out", str(report_path)])

    assert code == 0
    assert calls
    assert calls[0][:3] == [sys.executable, "-m", "pytest"]
    assert "tests/test_deepseek_order_json.py::test_deepseek_request_messages_keep_stable_contract_before_dynamic_context" in calls[0]
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["mode"] == "ai_brain_contract_audit"
    assert payload["case_count"] == len(ai_brain_contract_audit.CORE_CASES)
    assert "sk-[REDACTED]" in json.dumps(payload, ensure_ascii=False)
    assert "sk-1234567890abcdef" not in json.dumps(payload, ensure_ascii=False)
    console_output = capsys.readouterr().out
    assert all(ord(ch) < 128 for ch in console_output)
    assert "sk-[REDACTED]" in console_output


def test_ai_brain_contract_fails_when_pytest_fails(monkeypatch) -> None:
    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="boom")

    monkeypatch.setattr(ai_brain_contract_audit.subprocess, "run", fake_run)

    assert ai_brain_contract_audit.main(["--mode", "core"]) == 1


def test_ai_brain_contract_forces_utf8_subprocess_decoding(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        calls.append(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=None, stderr=None)

    monkeypatch.setattr(ai_brain_contract_audit.subprocess, "run", fake_run)

    result = ai_brain_contract_audit.run_pytest(["tests/test_risk.py::test_ai_veto_blocks_entry"], [])

    assert result.returncode == 0
    assert calls[0]["encoding"] == "utf-8"
    assert calls[0]["errors"] == "replace"
