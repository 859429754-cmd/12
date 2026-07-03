from __future__ import annotations

from pathlib import Path


def test_systemd_units_use_restart_and_watchdogs() -> None:
    root = Path("deploy/systemd")
    console = (root / "ai-quant-console.service").read_text(encoding="utf-8")
    trader = (root / "ai-quant-trader.service").read_text(encoding="utf-8")
    order_status = (root / "ai-quant-order-status-worker.service").read_text(encoding="utf-8")
    watchdog = (root / "ai-quant-health-watchdog.service").read_text(encoding="utf-8")
    maintenance = (root / "ai-quant-maintenance.service").read_text(encoding="utf-8")
    alerts = (root / "ai-quant-alerts.service").read_text(encoding="utf-8")
    alerts_timer = (root / "ai-quant-alerts.timer").read_text(encoding="utf-8")

    assert "Restart=always" in console
    assert "Restart=always" in trader
    assert "WatchdogSec" not in console
    assert "Type=notify" in trader
    assert "WatchdogSec=60" in trader
    assert "NotifyAccess=main" in trader
    assert "MALLOC_ARENA_MAX=2" in console
    assert "MALLOC_ARENA_MAX=2" in trader
    assert "PYTHONPATH=/root/ai-quant-trader/current" in console
    assert "PYTHONPATH=/root/ai-quant-trader/current" in trader
    assert "current/main.py" in trader
    assert "--app-dir /root/ai-quant-trader/current" in console
    assert "Restart=always" in order_status
    assert "main.py --order-status-worker" in order_status
    assert "PYTHONPATH=/root/ai-quant-trader/current" in order_status
    assert "current/scripts/http_readiness_check.py" in watchdog
    assert "--mode readiness" in watchdog
    assert "--allow-warn" in watchdog
    assert "current/scripts/runtime_maintenance.py" in maintenance
    assert "--backup-keep" in maintenance
    assert "--restore-drill-dir" in maintenance
    assert "--restore-drill-keep 3" in maintenance
    assert "current/scripts/runtime_alerts.py" in alerts
    assert "OnUnitActiveSec=60" in alerts_timer


def test_release_deploy_script_uses_current_symlink_and_rollback() -> None:
    script = Path("scripts/cloud_release_deploy_v2.py").read_text(encoding="utf-8")

    assert "releases/<git_sha>" in script or "release/<git_sha>" in script
    assert "current_link" in script
    assert "previous_target" in script
    assert 'cd "$remote_dir"' in script
    assert "release_health_check_failed_rolled_back" in script
    assert "release_readiness_check_failed_rolled_back" in script
    assert "--mode readiness" in script
    assert "http_readiness_check.py" in script


def test_systemd_readme_prefers_consolidated_worker_for_low_memory_cloud() -> None:
    readme = Path("deploy/systemd/README.md").read_text(encoding="utf-8")

    assert "sudo systemctl enable --now ai-quant-trader.service" in readme
    assert "sudo systemctl enable --now ai-quant-alerts.timer" in readme
    install_block = readme.split("```bash", 1)[1].split("```", 1)[0]
    assert "ai-quant-order-status-worker.service" not in install_block
    assert "consolidated worker" in readme
