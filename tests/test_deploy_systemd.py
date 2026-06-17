from __future__ import annotations

from pathlib import Path


def test_systemd_units_use_restart_and_watchdogs() -> None:
    root = Path("deploy/systemd")
    console = (root / "ai-quant-console.service").read_text(encoding="utf-8")
    trader = (root / "ai-quant-trader.service").read_text(encoding="utf-8")
    order_status = (root / "ai-quant-order-status-worker.service").read_text(encoding="utf-8")
    watchdog = (root / "ai-quant-health-watchdog.service").read_text(encoding="utf-8")
    maintenance = (root / "ai-quant-maintenance.service").read_text(encoding="utf-8")

    assert "Restart=always" in console
    assert "Restart=always" in trader
    assert "WatchdogSec" not in console
    assert "Type=notify" in trader
    assert "WatchdogSec=60" in trader
    assert "NotifyAccess=main" in trader
    assert "MALLOC_ARENA_MAX=2" in console
    assert "MALLOC_ARENA_MAX=2" in trader
    assert "Restart=always" in order_status
    assert "main.py --order-status-worker" in order_status
    assert "scripts/http_readiness_check.py" in watchdog
    assert "--mode health" in watchdog
    assert "--mode readiness" not in watchdog
    assert "scripts/runtime_maintenance.py" in maintenance
    assert "--backup-keep" in maintenance


def test_systemd_readme_prefers_consolidated_worker_for_low_memory_cloud() -> None:
    readme = Path("deploy/systemd/README.md").read_text(encoding="utf-8")

    assert "sudo systemctl enable --now ai-quant-trader.service" in readme
    install_block = readme.split("```bash", 1)[1].split("```", 1)[0]
    assert "ai-quant-order-status-worker.service" not in install_block
    assert "consolidated worker" in readme
