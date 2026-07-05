from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts import cloud_runtime_audit


def test_cloud_runtime_audit_passes_expected_release(tmp_path: Path, monkeypatch) -> None:
    key = tmp_path / "ssh-key"
    key.write_text("placeholder", encoding="utf-8")
    commands: list[str] = []

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        script = command[-1]
        commands.append(script)
        if "readlink -f" in script:
            return subprocess.CompletedProcess(command, 0, stdout="/root/ai-quant-trader/releases/abc123\n", stderr="")
        if ".last_successful_release" in script:
            return subprocess.CompletedProcess(command, 0, stdout="abc123\n", stderr="")
        if "systemctl is-active" in script:
            return subprocess.CompletedProcess(command, 0, stdout="active\nactive\nactive\nactive\n", stderr="")
        if "http_readiness_check.py" in script:
            return subprocess.CompletedProcess(command, 0, stdout='{"ok": true, "overall": "ok", "blocking": []}\n', stderr="")
        if "select created_at,payload from release_runs" in script:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"rows": [{"created_at": "now", "release_id": "abc123", "status": "success"}]}) + "\n",
                stderr="",
            )
        if "journalctl" in script:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(script)

    monkeypatch.setattr(cloud_runtime_audit.subprocess, "run", fake_run)

    report = cloud_runtime_audit.run_audit(
        host="root@example",
        key=key,
        remote_dir="/root/ai-quant-trader",
        expected_release="abc123",
    )

    assert report.ok is True
    assert report.failures == []
    assert report.last_successful_release == "abc123"
    assert report.latest_release_runs[0]["status"] == "success"
    assert any("journalctl" in item for item in commands)


def test_cloud_runtime_audit_fails_on_release_mismatch(tmp_path: Path, monkeypatch) -> None:
    key = tmp_path / "ssh-key"
    key.write_text("placeholder", encoding="utf-8")

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        script = command[-1]
        if "readlink -f" in script:
            return subprocess.CompletedProcess(command, 0, stdout="/root/ai-quant-trader/releases/old\n", stderr="")
        if ".last_successful_release" in script:
            return subprocess.CompletedProcess(command, 0, stdout="old\n", stderr="")
        if "systemctl is-active" in script:
            return subprocess.CompletedProcess(command, 0, stdout="active\nactive\nactive\nactive\n", stderr="")
        if "http_readiness_check.py" in script:
            return subprocess.CompletedProcess(command, 0, stdout='{"ok": true, "overall": "ok", "blocking": []}\n', stderr="")
        if "select created_at,payload from release_runs" in script:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"rows": [{"created_at": "now", "release_id": "old", "status": "success"}]}) + "\n",
                stderr="",
            )
        if "journalctl" in script:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(script)

    monkeypatch.setattr(cloud_runtime_audit.subprocess, "run", fake_run)

    report = cloud_runtime_audit.run_audit(
        host="root@example",
        key=key,
        remote_dir="/root/ai-quant-trader",
        expected_release="new",
    )

    assert report.ok is False
    assert "last_successful_release_mismatch:old!=new" in report.failures
    assert "current_target_mismatch:/root/ai-quant-trader/releases/old!=new" in report.failures
    assert "latest_release_run_mismatch:old!=new" in report.failures


def test_cloud_runtime_audit_fails_when_current_target_missing(tmp_path: Path, monkeypatch) -> None:
    key = tmp_path / "ssh-key"
    key.write_text("placeholder", encoding="utf-8")

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        script = command[-1]
        if "readlink -f" in script:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if ".last_successful_release" in script:
            return subprocess.CompletedProcess(command, 0, stdout="abc123\n", stderr="")
        if "systemctl is-active" in script:
            return subprocess.CompletedProcess(command, 0, stdout="active\nactive\nactive\nactive\n", stderr="")
        if "http_readiness_check.py" in script:
            return subprocess.CompletedProcess(command, 0, stdout='{"ok": true, "overall": "ok", "blocking": []}\n', stderr="")
        if "select created_at,payload from release_runs" in script:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"rows": [{"created_at": "now", "release_id": "abc123", "status": "success"}]}) + "\n",
                stderr="",
            )
        if "journalctl" in script:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(script)

    monkeypatch.setattr(cloud_runtime_audit.subprocess, "run", fake_run)

    report = cloud_runtime_audit.run_audit(
        host="root@example",
        key=key,
        remote_dir="/root/ai-quant-trader",
        expected_release="abc123",
    )

    assert report.ok is False
    assert "current_target_missing" in report.failures


def test_cloud_runtime_audit_includes_readiness_stdout_on_failure(tmp_path: Path, monkeypatch) -> None:
    key = tmp_path / "ssh-key"
    key.write_text("placeholder", encoding="utf-8")

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        script = command[-1]
        if "readlink -f" in script:
            return subprocess.CompletedProcess(command, 0, stdout="/root/ai-quant-trader/releases/abc123\n", stderr="")
        if ".last_successful_release" in script:
            return subprocess.CompletedProcess(command, 0, stdout="abc123\n", stderr="")
        if "systemctl is-active" in script:
            return subprocess.CompletedProcess(command, 0, stdout="active\nactive\nactive\nactive\n", stderr="")
        if "http_readiness_check.py" in script:
            return subprocess.CompletedProcess(command, 1, stdout='{"overall":"block","blocking":["exchange"]}\n', stderr="")
        if "select created_at,payload from release_runs" in script:
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"rows": []}) + "\n", stderr="")
        if "journalctl" in script:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(script)

    monkeypatch.setattr(cloud_runtime_audit.subprocess, "run", fake_run)

    report = cloud_runtime_audit.run_audit(host="root@example", key=key, remote_dir="/root/ai-quant-trader")

    assert report.ok is False
    assert any("readiness_check_failed:1:" in item and "exchange" in item for item in report.failures)


def test_cloud_runtime_audit_fails_on_recent_service_errors(tmp_path: Path, monkeypatch) -> None:
    key = tmp_path / "ssh-key"
    key.write_text("placeholder", encoding="utf-8")

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        script = command[-1]
        if "readlink -f" in script:
            return subprocess.CompletedProcess(command, 0, stdout="/root/ai-quant-trader/releases/abc123\n", stderr="")
        if ".last_successful_release" in script:
            return subprocess.CompletedProcess(command, 0, stdout="abc123\n", stderr="")
        if "systemctl is-active" in script:
            return subprocess.CompletedProcess(command, 0, stdout="active\nactive\nactive\nactive\n", stderr="")
        if "http_readiness_check.py" in script:
            return subprocess.CompletedProcess(command, 0, stdout='{"ok": true, "overall": "ok", "blocking": []}\n', stderr="")
        if "select created_at,payload from release_runs" in script:
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"rows": []}) + "\n", stderr="")
        if "journalctl" in script:
            return subprocess.CompletedProcess(command, 0, stdout="RuntimeError: exchange down\n", stderr="")
        raise AssertionError(script)

    monkeypatch.setattr(cloud_runtime_audit.subprocess, "run", fake_run)

    report = cloud_runtime_audit.run_audit(host="root@example", key=key, remote_dir="/root/ai-quant-trader")

    assert report.ok is False
    assert "recent_service_errors_detected" in report.failures
    assert "RuntimeError" in report.recent_error_log_tail


def test_cloud_runtime_audit_main_writes_utf8_file_but_ascii_console(tmp_path: Path, monkeypatch, capsys) -> None:
    key = tmp_path / "ssh-key"
    key.write_text("placeholder", encoding="utf-8")

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        script = command[-1]
        if "readlink -f" in script:
            return subprocess.CompletedProcess(command, 0, stdout="/root/ai-quant-trader/releases/abc123\n", stderr="")
        if ".last_successful_release" in script:
            return subprocess.CompletedProcess(command, 0, stdout="abc123\n", stderr="")
        if "systemctl is-active" in script:
            return subprocess.CompletedProcess(command, 0, stdout="active\nactive\nactive\nactive\n", stderr="")
        if "http_readiness_check.py" in script:
            return subprocess.CompletedProcess(command, 0, stdout='{"ok": true, "overall": "ok", "blocking": []}\n', stderr="")
        if "select created_at,payload from release_runs" in script:
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"rows": []}) + "\n", stderr="")
        if "journalctl" in script:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(script)

    monkeypatch.setattr(cloud_runtime_audit.subprocess, "run", fake_run)
    report_path = tmp_path / "cloud-runtime.json"

    code = cloud_runtime_audit.main(
        [
            "--host",
            "root@example",
            "--key",
            str(key),
            "--remote-dir",
            "/root/ai-quant-trader",
            "--json-out",
            str(report_path),
        ]
    )

    assert code == 0
    console_output = capsys.readouterr().out
    assert all(ord(ch) < 128 for ch in console_output)
    assert json.loads(report_path.read_text(encoding="utf-8"))["ok"] is True
