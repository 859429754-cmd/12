from __future__ import annotations

import sys
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest

from scripts.cloud_release_deploy import (
    REMOTE_RSYNC_EXCLUDES,
    iter_tracked_source_files,
    remote_sync_script,
    should_include_source,
)


def test_source_bundle_excludes_runtime_state_and_secret_files() -> None:
    root = Path.cwd()

    assert not should_include_source(root / ".env.runtime")
    assert not should_include_source(root / ".venv" / "bin" / "python")
    assert not should_include_source(root / "data" / "trader.sqlite3")
    assert not should_include_source(root / "logs" / "trader.log")
    assert not should_include_source(root / "output" / "preview.png")
    assert not should_include_source(root / "console" / "node_modules" / "x" / "index.js")
    assert not should_include_source(root / "console" / "dist" / "index.html")
    assert should_include_source(root / "ai_quant_trader" / "data" / "market.py")


def test_source_bundle_uses_git_tracked_files_only() -> None:
    root = Path.cwd()
    tracked = {path.relative_to(root).as_posix() for path in iter_tracked_source_files()}

    assert not any("/.scratch/" in path or path.startswith(".scratch/") for path in tracked)
    assert "ai_quant_trader/data/market.py" in tracked
    assert "console/src/App.tsx" in tracked


def test_remote_sync_preserves_runtime_directories() -> None:
    script = remote_sync_script("/root/ai-quant-trader", restart=True, install_deps=False)

    for required in (
        ".env.runtime",
        ".venv/",
        "/data/",
        "/logs/",
        "/output/",
        "/backups/",
        "config/config.yaml",
    ):
        assert required in REMOTE_RSYNC_EXCLUDES
        assert f"--exclude '{required}'" in script
    assert "data/" not in REMOTE_RSYNC_EXCLUDES
    assert "logs/" not in REMOTE_RSYNC_EXCLUDES
    assert 'cp "$remote_dir"/deploy/systemd/*.service "$remote_dir"/deploy/systemd/*.timer /etc/systemd/system/' in script
    assert "systemctl daemon-reload" in script
    assert "systemctl restart ai-quant-console.service ai-quant-trader.service" in script


def test_release_v2_script_is_directly_executable() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/cloud_release_deploy_v2.py", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--run-console-e2e" in result.stdout


def test_release_v2_can_run_console_e2e_before_remote_upload(monkeypatch, tmp_path) -> None:
    import scripts.cloud_release_deploy_v2 as deploy_v2

    key = tmp_path / "ssh-key"
    key.write_text("placeholder", encoding="utf-8")
    calls: list[tuple[str, list[str], dict[str, object]]] = []

    def fake_subprocess_run(command, **kwargs):
        calls.append(("subprocess", list(command), kwargs))
        return SimpleNamespace(stdout="")

    def fake_run(command: list[str]) -> None:
        calls.append(("run", command, {}))

    monkeypatch.setattr(deploy_v2.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(deploy_v2, "run", fake_run)
    monkeypatch.setattr(deploy_v2, "build_source_tar", lambda target: target.write_text("src", encoding="utf-8"))
    monkeypatch.setattr(deploy_v2, "build_console_dist_tar", lambda target: target.write_text("console", encoding="utf-8"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cloud_release_deploy_v2.py",
            "--host",
            "root@example",
            "--key",
            str(key),
            "--remote-dir",
            "/srv/ai-quant",
            "--release-id",
            "test-release",
            "--run-console-e2e",
        ],
    )

    assert deploy_v2.main() == 0

    assert calls[0][0] == "subprocess"
    assert calls[0][1][-2:] == ["run", "test:e2e"]
    assert calls[0][2]["cwd"] == deploy_v2.REPO_ROOT / "console"
    assert str(calls[0][2]["env"]["PLAYWRIGHT_OUTPUT_DIR"]).endswith("aiquant-playwright-results")
    assert calls[1][0] == "run"
    assert calls[1][1][0] == "scp"


def test_release_v2_can_run_cloud_readonly_e2e_after_remote_release(monkeypatch, tmp_path) -> None:
    import scripts.cloud_release_deploy_v2 as deploy_v2
    from scripts.cloud_runtime_audit import CloudRuntimeAudit

    key = tmp_path / "ssh-key"
    key.write_text("placeholder", encoding="utf-8")
    monkeypatch.setenv("AIQUANT_E2E_ACCOUNT1_PASSWORD", "account1-password")
    monkeypatch.setenv("AIQUANT_E2E_ACCOUNT2_PASSWORD", "account2-password")
    monkeypatch.setenv("AIQUANT_E2E_ADMIN_PASSWORD", "admin-password")
    calls: list[tuple[str, list[str], dict[str, object]]] = []

    def fake_subprocess_run(command, **kwargs):
        calls.append(("subprocess", list(command), kwargs))
        return SimpleNamespace(stdout="")

    def fake_run(command: list[str]) -> None:
        calls.append(("run", command, {}))

    def fake_cloud_audit(**kwargs):  # noqa: ANN003
        calls.append(("cloud_runtime_audit", [], kwargs))
        return CloudRuntimeAudit(
            ok=True,
            host=kwargs["host"],
            remote_dir=kwargs["remote_dir"],
            expected_release=kwargs["expected_release"],
            current_target=f'{kwargs["remote_dir"]}/releases/{kwargs["expected_release"]}',
            last_successful_release=kwargs["expected_release"],
            service_statuses={"ai-quant-trader.service": "active"},
            readiness={"overall": "ok"},
            latest_release_runs=[],
            recent_error_log_tail="",
            failures=[],
            duration_seconds=0.1,
        )

    monkeypatch.setattr(deploy_v2.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(deploy_v2, "run", fake_run)
    monkeypatch.setattr(deploy_v2, "run_cloud_audit", fake_cloud_audit)
    monkeypatch.setattr(deploy_v2, "build_source_tar", lambda target: target.write_text("src", encoding="utf-8"))
    monkeypatch.setattr(deploy_v2, "build_console_dist_tar", lambda target: target.write_text("console", encoding="utf-8"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cloud_release_deploy_v2.py",
            "--host",
            "root@example",
            "--key",
            str(key),
            "--remote-dir",
            "/srv/ai-quant",
            "--release-id",
            "test-release",
            "--cloud-console-readonly-e2e-url",
            "https://example.test",
        ],
    )

    assert deploy_v2.main() == 0

    assert any(call[0] == "subprocess" and "readlink -f '/srv/ai-quant/current'" in call[1][-1] for call in calls)
    cloud_e2e_calls = [call for call in calls if call[0] == "subprocess" and call[1][-2:] == ["run", "test:e2e:cloud"]]
    assert len(cloud_e2e_calls) == 1
    assert cloud_e2e_calls[0][2]["env"]["CONSOLE_URL"] == "https://example.test"
    assert calls[-2][0] == "run"
    assert ".last_successful_release" in calls[-2][1][-1]
    assert calls[-1][0] == "cloud_runtime_audit"
    assert calls[-1][2]["expected_release"] == "test-release"


def test_release_v2_fails_when_post_release_cloud_runtime_audit_fails(monkeypatch, tmp_path) -> None:
    import scripts.cloud_release_deploy_v2 as deploy_v2
    from scripts.cloud_runtime_audit import CloudRuntimeAudit

    key = tmp_path / "ssh-key"
    key.write_text("placeholder", encoding="utf-8")
    monkeypatch.setenv("AIQUANT_E2E_ACCOUNT1_PASSWORD", "account1-password")
    monkeypatch.setenv("AIQUANT_E2E_ACCOUNT2_PASSWORD", "account2-password")
    monkeypatch.setenv("AIQUANT_E2E_ADMIN_PASSWORD", "admin-password")
    calls: list[list[str]] = []

    def fake_subprocess_run(command, **kwargs):
        if "readlink -f '/srv/ai-quant/current'" in list(command)[-1]:
            return SimpleNamespace(stdout="/srv/ai-quant/releases/previous\n")
        return SimpleNamespace(stdout="")

    def fake_cloud_audit(**kwargs):  # noqa: ANN003
        return CloudRuntimeAudit(
            ok=False,
            host=kwargs["host"],
            remote_dir=kwargs["remote_dir"],
            expected_release=kwargs["expected_release"],
            current_target="/srv/ai-quant/releases/old",
            last_successful_release="old",
            service_statuses={"ai-quant-trader.service": "active"},
            readiness={"overall": "ok"},
            latest_release_runs=[],
            recent_error_log_tail="",
            failures=["current_target_mismatch:/srv/ai-quant/releases/old!=test-release"],
            duration_seconds=0.1,
        )

    def fake_run(command: list[str]) -> None:
        calls.append(command)

    monkeypatch.setattr(deploy_v2.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(deploy_v2, "run", fake_run)
    monkeypatch.setattr(deploy_v2, "run_cloud_audit", fake_cloud_audit)
    monkeypatch.setattr(deploy_v2, "build_source_tar", lambda target: target.write_text("src", encoding="utf-8"))
    monkeypatch.setattr(deploy_v2, "build_console_dist_tar", lambda target: target.write_text("console", encoding="utf-8"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cloud_release_deploy_v2.py",
            "--host",
            "root@example",
            "--key",
            str(key),
            "--remote-dir",
            "/srv/ai-quant",
            "--release-id",
            "test-release",
            "--cloud-console-readonly-e2e-url",
            "https://example.test",
        ],
    )

    with pytest.raises(RuntimeError, match="Post-release cloud runtime audit failed"):
        deploy_v2.main()

    rollback_calls = [call for call in calls if "cloud_console_e2e_failed_rolled_back" in call[-1]]
    assert len(rollback_calls) == 1
    assert "/srv/ai-quant/releases/previous" in rollback_calls[0][-1]
    assert ".last_successful_release" in rollback_calls[0][-1]


def test_release_v2_rolls_back_when_cloud_readonly_e2e_fails(monkeypatch, tmp_path) -> None:
    import scripts.cloud_release_deploy_v2 as deploy_v2

    key = tmp_path / "ssh-key"
    key.write_text("placeholder", encoding="utf-8")
    monkeypatch.setenv("AIQUANT_E2E_ACCOUNT1_PASSWORD", "account1-password")
    monkeypatch.setenv("AIQUANT_E2E_ACCOUNT2_PASSWORD", "account2-password")
    monkeypatch.setenv("AIQUANT_E2E_ADMIN_PASSWORD", "admin-password")
    calls: list[tuple[str, list[str], dict[str, object]]] = []

    def fake_subprocess_run(command, **kwargs):
        calls.append(("subprocess", list(command), kwargs))
        if "readlink -f '/srv/ai-quant/current'" in list(command)[-1]:
            return SimpleNamespace(stdout="/srv/ai-quant/releases/previous\n")
        if list(command)[-2:] == ["run", "test:e2e:cloud"]:
            raise subprocess.CalledProcessError(1, command)
        return SimpleNamespace(stdout="")

    def fake_run(command: list[str]) -> None:
        calls.append(("run", command, {}))

    monkeypatch.setattr(deploy_v2.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(deploy_v2, "run", fake_run)
    monkeypatch.setattr(deploy_v2, "build_source_tar", lambda target: target.write_text("src", encoding="utf-8"))
    monkeypatch.setattr(deploy_v2, "build_console_dist_tar", lambda target: target.write_text("console", encoding="utf-8"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cloud_release_deploy_v2.py",
            "--host",
            "root@example",
            "--key",
            str(key),
            "--remote-dir",
            "/srv/ai-quant",
            "--release-id",
            "test-release",
            "--restart",
            "--cloud-console-readonly-e2e-url",
            "https://example.test",
        ],
    )

    with pytest.raises(subprocess.CalledProcessError):
        deploy_v2.main()

    rollback_calls = [call for call in calls if call[0] == "run" and "cloud_console_e2e_failed_rolled_back" in call[1][-1]]
    assert len(rollback_calls) == 1
    assert "/srv/ai-quant/releases/previous" in rollback_calls[0][1][-1]


def test_release_v2_full_local_validation_runs_all_gates_before_remote_upload(monkeypatch, tmp_path) -> None:
    import scripts.cloud_release_deploy_v2 as deploy_v2

    key = tmp_path / "ssh-key"
    key.write_text("placeholder", encoding="utf-8")
    calls: list[tuple[str, list[str], dict[str, object]]] = []

    def fake_subprocess_run(command, **kwargs):
        calls.append(("subprocess", list(command), kwargs))
        return SimpleNamespace(stdout="")

    def fake_run(command: list[str]) -> None:
        calls.append(("run", command, {}))

    monkeypatch.setattr(deploy_v2.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(deploy_v2, "run", fake_run)
    monkeypatch.setattr(deploy_v2, "build_source_tar", lambda target: target.write_text("src", encoding="utf-8"))
    monkeypatch.setattr(deploy_v2, "build_console_dist_tar", lambda target: target.write_text("console", encoding="utf-8"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cloud_release_deploy_v2.py",
            "--host",
            "root@example",
            "--key",
            str(key),
            "--remote-dir",
            "/srv/ai-quant",
            "--release-id",
            "test-release",
            "--full-local-validation",
        ],
    )

    assert deploy_v2.main() == 0

    validation_calls = calls[:1]
    assert [call[0] for call in validation_calls] == ["subprocess"]
    assert validation_calls[0][1] == [
        sys.executable,
        "scripts/project_audit_board.py",
        "--mode",
        "full",
        "--skip-cloud",
        "--json-out",
        "output/audit/project_audit_board_release_gate.json",
    ]
    assert validation_calls[0][2]["cwd"] == deploy_v2.REPO_ROOT
    assert calls[1][0] == "run"
    assert calls[1][1][0] == "scp"


def test_release_v2_skips_console_e2e_by_default(monkeypatch, tmp_path) -> None:
    import scripts.cloud_release_deploy_v2 as deploy_v2

    key = tmp_path / "ssh-key"
    key.write_text("placeholder", encoding="utf-8")
    subprocess_calls: list[list[str]] = []

    def fake_subprocess_run(command, **kwargs):
        subprocess_calls.append(list(command))
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(deploy_v2.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(deploy_v2, "run", lambda command: None)
    monkeypatch.setattr(deploy_v2, "build_source_tar", lambda target: target.write_text("src", encoding="utf-8"))
    monkeypatch.setattr(deploy_v2, "build_console_dist_tar", lambda target: target.write_text("console", encoding="utf-8"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cloud_release_deploy_v2.py",
            "--host",
            "root@example",
            "--key",
            str(key),
            "--remote-dir",
            "/srv/ai-quant",
            "--release-id",
            "test-release",
        ],
    )

    assert deploy_v2.main() == 0

    assert subprocess_calls == []


def test_release_v2_records_health_and_readiness_in_release_runs() -> None:
    import scripts.cloud_release_deploy_v2 as deploy_v2

    script = deploy_v2.remote_release_script(
        "/srv/ai-quant",
        "abc123",
        restart=True,
        install_deps=False,
        health_timeout=1,
    )

    assert "health_json=" in script
    assert "readiness_json=" in script
    assert "current/scripts/record_release_run.py" in script
    assert "AIQUANT_RELEASE_ID=\"$release_id\"" in script
    assert "AIQUANT_HEALTH_JSON=\"$health_json\"" in script
    assert "AIQUANT_READINESS_JSON=\"$readiness_json\"" in script
    assert "release_audit_record_failed_rolled_back" in script
