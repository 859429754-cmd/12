from __future__ import annotations

import json
import subprocess
import sys

from scripts import project_audit_board


def test_project_audit_board_core_groups_cover_required_gates() -> None:
    group_ids = {group.id for group in project_audit_board.selected_groups("core")}

    assert "public_repo_preflight" in group_ids
    assert "compileall" in group_ids
    assert "trading_chain_extended" in group_ids
    assert "frontend_build" in group_ids
    assert "frontend_e2e" in group_ids
    assert "backend_full_pytest" not in group_ids
    assert "cloud_readonly_e2e" not in group_ids


def test_project_audit_board_full_groups_include_cloud_and_full_pytest() -> None:
    group_ids = {group.id for group in project_audit_board.selected_groups("full")}

    assert "backend_full_pytest" in group_ids
    assert "cloud_readonly_e2e" in group_ids


def test_project_audit_board_writes_json_and_returns_success(tmp_path, monkeypatch, capsys) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="passed sk-1234567890abcdef\n", stderr="")

    monkeypatch.setattr(project_audit_board.subprocess, "run", fake_run)
    report_path = tmp_path / "audit" / "board.json"

    code = project_audit_board.main(["--mode", "core", "--json-out", str(report_path)])

    assert code == 0
    assert calls
    assert calls[0] == [sys.executable, "scripts/public_repo_preflight.py"]
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["mode"] == "core"
    assert payload["failed_count"] == 0
    assert "sk-[REDACTED]" in json.dumps(payload, ensure_ascii=False)
    assert "sk-1234567890abcdef" not in json.dumps(payload, ensure_ascii=False)
    console_payload = json.loads(capsys.readouterr().out)
    assert console_payload["ok"] is True


def test_project_audit_board_forces_utf8_subprocess_decoding(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        calls.append(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=None, stderr=None)

    monkeypatch.setattr(project_audit_board.subprocess, "run", fake_run)

    result = project_audit_board.run_group(project_audit_board.selected_groups("core")[0])

    assert result.status == "passed"
    assert calls[0]["encoding"] == "utf-8"
    assert calls[0]["errors"] == "replace"
    assert result.stdout_tail == ""


def test_project_audit_board_fails_when_required_group_fails(monkeypatch) -> None:
    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="boom")

    monkeypatch.setattr(project_audit_board.subprocess, "run", fake_run)

    assert project_audit_board.main(["--mode", "core"]) == 1


def test_project_audit_board_console_print_is_encoding_safe(monkeypatch) -> None:
    printed: list[str] = []

    class FakeStdout:
        encoding = "ascii"

    monkeypatch.setattr(project_audit_board.sys, "stdout", FakeStdout())
    monkeypatch.setattr("builtins.print", lambda text: printed.append(text))

    project_audit_board.print_report('{"status":"✓"}')

    assert printed == ['{"status":"?"}']


def test_project_audit_board_skips_cloud_when_optional_env_missing(monkeypatch) -> None:
    for name in (
        "CONSOLE_URL",
        "AIQUANT_E2E_ACCOUNT1_PASSWORD",
        "AIQUANT_E2E_ACCOUNT2_PASSWORD",
        "AIQUANT_E2E_ADMIN_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)

    group = next(item for item in project_audit_board.audit_groups() if item.id == "cloud_readonly_e2e")
    result = project_audit_board.run_group(group)

    assert result.status == "skipped"
    assert result.skipped_reason is not None
    assert "CONSOLE_URL" in result.skipped_reason
