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
    assert "Restart=always" in order_status
    assert "main.py --order-status-worker" in order_status
    assert "scripts/http_readiness_check.py" in watchdog
    assert "--mode readiness" in watchdog
    assert "scripts/runtime_maintenance.py" in maintenance
    assert "--backup-keep" in maintenance
