from __future__ import annotations

import json
import subprocess
import sys

from scripts import project_audit_board


def test_project_audit_board_core_groups_cover_required_gates() -> None:
    group_ids = {group.id for group in project_audit_board.selected_groups("core")}

    assert "public_repo_preflight" in group_ids
    assert "compileall" in group_ids
    assert "ai_brain_contract" in group_ids
    assert "trading_chain_extended" in group_ids
    assert "frontend_build" in group_ids
    assert "frontend_e2e" in group_ids
    assert "backend_full_pytest" not in group_ids
    assert "cloud_readonly_e2e" not in group_ids


def test_project_audit_board_full_groups_include_cloud_and_full_pytest() -> None:
    group_ids = {group.id for group in project_audit_board.selected_groups("full")}

    assert "backend_full_pytest" in group_ids
    assert "cloud_readonly_e2e" in group_ids


def test_project_audit_board_skip_cloud_removes_cloud_group() -> None:
    group_ids = {group.id for group in project_audit_board.selected_groups("full", skip_cloud=True)}

    assert "backend_full_pytest" in group_ids
    assert "cloud_readonly_e2e" not in group_ids
    assert "cloud_runtime_audit" not in group_ids


def test_project_audit_board_cloud_runtime_is_opt_in() -> None:
    default_group_ids = {group.id for group in project_audit_board.selected_groups("full")}
    included_group_ids = {
        group.id for group in project_audit_board.selected_groups("full", include_cloud_runtime=True)
    }
    skipped_group_ids = {
        group.id
        for group in project_audit_board.selected_groups(
            "full",
            include_cloud_runtime=True,
            skip_cloud=True,
        )
    }

    assert "cloud_runtime_audit" not in default_group_ids
    assert "cloud_runtime_audit" in included_group_ids
    assert "cloud_runtime_audit" not in skipped_group_ids


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
    assert payload["groups"][0]["title"] == "公开仓库防泄露检查"
    assert "sk-[REDACTED]" in json.dumps(payload, ensure_ascii=False)
    assert "sk-1234567890abcdef" not in json.dumps(payload, ensure_ascii=False)
    console_output = capsys.readouterr().out
    assert all(ord(ch) < 128 for ch in console_output)
    assert "\\u516c\\u5f00\\u4ed3\\u5e93" in console_output
    console_payload = json.loads(console_output)
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


def test_project_audit_board_local_e2e_does_not_inherit_cloud_url(monkeypatch) -> None:
    seen_env: list[dict[str, str]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        seen_env.append(kwargs["env"])
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setenv("CONSOLE_URL", "http://8.209.200.19")
    monkeypatch.setattr(project_audit_board.subprocess, "run", fake_run)
    group = next(item for item in project_audit_board.audit_groups() if item.id == "frontend_e2e")

    result = project_audit_board.run_group(group)

    assert result.status == "passed"
    assert "CONSOLE_URL" not in seen_env[0]


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


def test_project_audit_board_can_fail_on_skipped_cloud_group(monkeypatch) -> None:
    for name in (
        "CONSOLE_URL",
        "AIQUANT_E2E_ACCOUNT1_PASSWORD",
        "AIQUANT_E2E_ACCOUNT2_PASSWORD",
        "AIQUANT_E2E_ADMIN_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(project_audit_board.subprocess, "run", fake_run)

    assert project_audit_board.main(["--mode", "full", "--fail-on-skipped"]) == 1


def test_project_audit_board_passes_explicit_expected_release_to_cloud_runtime(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(project_audit_board.subprocess, "run", fake_run)

    assert (
        project_audit_board.main(
            [
                "--mode",
                "full",
                "--skip-cloud",
                "--include-cloud-runtime",
                "--expected-cloud-release",
                "abc1234",
            ]
        )
        == 0
    )
    assert not any("--expected-release" in call for call in calls)

    calls.clear()
    assert (
        project_audit_board.main(
            [
                "--mode",
                "full",
                "--include-cloud-runtime",
                "--expected-cloud-release",
                "abc1234",
            ]
        )
        == 0
    )
    cloud_call = next(call for call in calls if call[:2] == [sys.executable, "scripts/cloud_runtime_audit.py"])
    assert cloud_call[-2:] == ["--expected-release", "abc1234"]


def test_project_audit_board_final_gate_defaults_cloud_expected_release_to_git_head(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(project_audit_board.subprocess, "run", fake_run)
    monkeypatch.setattr(project_audit_board, "_git_short_head", lambda: "head999")
    monkeypatch.setenv("CONSOLE_URL", "http://127.0.0.1")
    monkeypatch.setenv("AIQUANT_E2E_ACCOUNT1_PASSWORD", "x")
    monkeypatch.setenv("AIQUANT_E2E_ACCOUNT2_PASSWORD", "x")
    monkeypatch.setenv("AIQUANT_E2E_ADMIN_PASSWORD", "x")

    assert (
        project_audit_board.main(
            [
                "--mode",
                "full",
                "--include-cloud-runtime",
                "--fail-on-skipped",
            ]
        )
        == 0
    )
    cloud_call = next(call for call in calls if call[:2] == [sys.executable, "scripts/cloud_runtime_audit.py"])
    assert cloud_call[-2:] == ["--expected-release", "head999"]


def test_project_audit_board_can_require_cloud_live_ready(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(project_audit_board.subprocess, "run", fake_run)
    monkeypatch.setenv("CONSOLE_URL", "http://127.0.0.1")
    monkeypatch.setenv("AIQUANT_E2E_ACCOUNT1_PASSWORD", "x")
    monkeypatch.setenv("AIQUANT_E2E_ACCOUNT2_PASSWORD", "x")
    monkeypatch.setenv("AIQUANT_E2E_ADMIN_PASSWORD", "x")

    assert (
        project_audit_board.main(
            [
                "--mode",
                "full",
                "--include-cloud-runtime",
                "--expected-cloud-release",
                "abc1234",
                "--expect-cloud-live-ready",
            ]
        )
        == 0
    )
    cloud_call = next(call for call in calls if call[:2] == [sys.executable, "scripts/cloud_runtime_audit.py"])
    assert cloud_call[-5:] == ["--expected-release", "abc1234", "--expect-live-ready", "--runtime-env-mode", "cloud-live"]


def test_project_audit_board_can_pass_trend_live_env_mode_to_cloud_runtime(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(project_audit_board.subprocess, "run", fake_run)
    monkeypatch.setenv("CONSOLE_URL", "http://127.0.0.1")
    monkeypatch.setenv("AIQUANT_E2E_ACCOUNT1_PASSWORD", "x")
    monkeypatch.setenv("AIQUANT_E2E_ACCOUNT2_PASSWORD", "x")
    monkeypatch.setenv("AIQUANT_E2E_ADMIN_PASSWORD", "x")

    assert (
        project_audit_board.main(
            [
                "--mode",
                "full",
                "--include-cloud-runtime",
                "--expected-cloud-release",
                "abc1234",
                "--expect-cloud-live-ready",
                "--cloud-runtime-env-mode",
                "trend-live",
            ]
        )
        == 0
    )
    cloud_call = next(call for call in calls if call[:2] == [sys.executable, "scripts/cloud_runtime_audit.py"])
    assert cloud_call[-5:] == ["--expected-release", "abc1234", "--expect-live-ready", "--runtime-env-mode", "trend-live"]


def test_project_audit_board_can_target_explicit_cloud_host(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(project_audit_board.subprocess, "run", fake_run)
    monkeypatch.setenv("CONSOLE_URL", "http://47.84.92.81")
    monkeypatch.setenv("AIQUANT_E2E_ACCOUNT1_PASSWORD", "x")
    monkeypatch.setenv("AIQUANT_E2E_ACCOUNT2_PASSWORD", "x")
    monkeypatch.setenv("AIQUANT_E2E_ADMIN_PASSWORD", "x")

    assert (
        project_audit_board.main(
            [
                "--mode",
                "full",
                "--include-cloud-runtime",
                "--expected-cloud-release",
                "abc1234",
                "--cloud-host",
                "root@47.84.92.81",
            ]
        )
        == 0
    )
    cloud_call = next(call for call in calls if call[:2] == [sys.executable, "scripts/cloud_runtime_audit.py"])
    assert "--host" in cloud_call
    assert cloud_call[cloud_call.index("--host") + 1] == "root@47.84.92.81"
