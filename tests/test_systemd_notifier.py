from __future__ import annotations

from ai_quant_trader.ops.systemd import SystemdNotifier


def test_systemd_notifier_is_noop_without_notify_socket() -> None:
    notifier = SystemdNotifier(notify_socket=None)

    assert notifier.enabled is False
    assert notifier.watchdog_enabled is False
    assert notifier.ready("ready") is False
    assert notifier.watchdog("alive") is False


def test_systemd_notifier_calculates_half_watchdog_interval() -> None:
    notifier = SystemdNotifier(notify_socket="/tmp/notify.sock", watchdog_usec=60_000_000)

    assert notifier.enabled is True
    assert notifier.watchdog_enabled is True
    assert notifier.watchdog_interval_seconds() == 30.0
