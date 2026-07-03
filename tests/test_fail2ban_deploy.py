from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_fail2ban_probe_filter_and_jail_are_present() -> None:
    filter_file = ROOT / "deploy" / "fail2ban" / "aiquant-nginx-probes.conf"
    jail_file = ROOT / "deploy" / "fail2ban" / "aiquant-nginx-probes.local"
    script_file = ROOT / "scripts" / "install_fail2ban_protection.sh"

    assert filter_file.exists()
    assert jail_file.exists()
    assert script_file.exists()
    filter_text = filter_file.read_text(encoding="utf-8")
    jail_text = jail_file.read_text(encoding="utf-8")
    script_text = script_file.read_text(encoding="utf-8")

    for needle in [".env", "/\\.git", "php://filter", "wp-admin", "/etc/passwd"]:
        assert needle in filter_text
    assert "logpath = /var/log/nginx/access.log" in jail_text
    assert "maxretry = 8" in jail_text
    assert "bantime = 3600" in jail_text
    assert "fail2ban-client status aiquant-nginx-probes" in script_text
