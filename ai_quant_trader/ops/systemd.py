from __future__ import annotations

import os
import socket
from dataclasses import dataclass


@dataclass(frozen=True)
class SystemdNotifier:
    """Minimal sd_notify client with a no-op fallback outside systemd."""

    notify_socket: str | None
    watchdog_usec: int | None = None

    @classmethod
    def from_environment(cls) -> "SystemdNotifier":
        raw_watchdog = os.environ.get("WATCHDOG_USEC")
        try:
            watchdog_usec = int(raw_watchdog) if raw_watchdog else None
        except ValueError:
            watchdog_usec = None
        return cls(notify_socket=os.environ.get("NOTIFY_SOCKET"), watchdog_usec=watchdog_usec)

    @property
    def enabled(self) -> bool:
        return bool(self.notify_socket)

    @property
    def watchdog_enabled(self) -> bool:
        return self.enabled and bool(self.watchdog_usec and self.watchdog_usec > 0)

    def watchdog_interval_seconds(self, fallback: float = 30.0) -> float:
        if not self.watchdog_usec or self.watchdog_usec <= 0:
            return fallback
        return max(float(self.watchdog_usec) / 2_000_000.0, 1.0)

    def ready(self, status: str = "ready") -> bool:
        return self.notify(f"READY=1\nSTATUS={status}")

    def watchdog(self, status: str = "watchdog") -> bool:
        return self.notify(f"WATCHDOG=1\nSTATUS={status}")

    def stopping(self, status: str = "stopping") -> bool:
        return self.notify(f"STOPPING=1\nSTATUS={status}")

    def notify(self, message: str) -> bool:
        if not self.notify_socket:
            return False
        if not hasattr(socket, "AF_UNIX"):
            return False
        address = self.notify_socket
        if address.startswith("@"):
            address = "\0" + address[1:]
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.connect(address)
                sock.sendall(message.encode("utf-8"))
            return True
        except OSError:
            return False
