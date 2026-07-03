from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_nginx_hardening_template_contains_public_controls() -> None:
    template = (ROOT / "deploy" / "nginx" / "aiquant.conf.template").read_text(encoding="utf-8")

    assert "limit_req_zone $binary_remote_addr zone=aiquant_console" in template
    assert "limit_req_zone $binary_remote_addr zone=aiquant_auth" in template
    assert "limit_conn_zone $binary_remote_addr zone=aiquant_conn" in template
    assert "location = /api/auth/login" in template
    assert "limit_req zone=aiquant_auth burst=3 nodelay;" in template
    assert "add_header X-Frame-Options \"DENY\" always;" in template
    assert "add_header X-Content-Type-Options \"nosniff\" always;" in template
    assert "add_header Referrer-Policy \"no-referrer\" always;" in template
    assert "add_header Strict-Transport-Security" in template
    assert "include /etc/letsencrypt/options-ssl-nginx.conf;" in template
    assert "ssl_protocols TLSv1.2 TLSv1.3;" not in template
    assert "proxy_pass http://127.0.0.1:8090;" in template
    assert "__CERT_PRIVKEY__" in template


def test_nginx_hardening_installer_is_safe_to_run_on_cloud() -> None:
    script = (ROOT / "scripts" / "install_nginx_public_hardening.sh").read_text(encoding="utf-8")

    assert "install_nginx_public_hardening_requires_root" in script
    assert "nginx -t" in script
    assert "systemctl reload nginx" in script
    assert "curl -fsS -o /dev/null --max-time 5 http://127.0.0.1/api/health" in script
    assert ".bak.$(date +%Y%m%d%H%M%S)" in script
    assert ".env.runtime" not in script
