#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:-/root/ai-quant-trader/current}"
SITE_NAME="${AIQUANT_NGINX_SITE_NAME:-aiquant}"
SERVER_NAMES="${AIQUANT_NGINX_SERVER_NAMES:-8.209.200.19.nip.io 8.209.200.19 _}"
TLS_SERVER_NAME="${AIQUANT_NGINX_TLS_SERVER_NAME:-8.209.200.19.nip.io}"
CERT_FULLCHAIN="${AIQUANT_NGINX_CERT_FULLCHAIN:-/etc/letsencrypt/live/${TLS_SERVER_NAME}/fullchain.pem}"
CERT_PRIVKEY="${AIQUANT_NGINX_CERT_PRIVKEY:-/etc/letsencrypt/live/${TLS_SERVER_NAME}/privkey.pem}"
TEMPLATE="${REPO_DIR}/deploy/nginx/aiquant.conf.template"
SITE_AVAILABLE="/etc/nginx/sites-available/${SITE_NAME}"
SITE_ENABLED="/etc/nginx/sites-enabled/${SITE_NAME}"

if [ "$(id -u)" -ne 0 ]; then
  echo "install_nginx_public_hardening_requires_root" >&2
  exit 1
fi

if [ ! -f "$TEMPLATE" ]; then
  echo "nginx_template_missing:${TEMPLATE}" >&2
  exit 2
fi

if [ ! -f "$CERT_FULLCHAIN" ] || [ ! -f "$CERT_PRIVKEY" ]; then
  echo "nginx_tls_certificate_missing:${TLS_SERVER_NAME}" >&2
  exit 3
fi

mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled
if [ -e "$SITE_AVAILABLE" ]; then
  cp -a "$SITE_AVAILABLE" "${SITE_AVAILABLE}.bak.$(date +%Y%m%d%H%M%S)"
fi

export TEMPLATE SITE_AVAILABLE SERVER_NAMES TLS_SERVER_NAME CERT_FULLCHAIN CERT_PRIVKEY
python3 - <<'PY'
import os
from pathlib import Path

template = Path(os.environ["TEMPLATE"]).read_text(encoding="utf-8")
replacements = {
    "__SERVER_NAMES__": os.environ["SERVER_NAMES"],
    "__TLS_SERVER_NAME__": os.environ["TLS_SERVER_NAME"],
    "__CERT_FULLCHAIN__": os.environ["CERT_FULLCHAIN"],
    "__CERT_PRIVKEY__": os.environ["CERT_PRIVKEY"],
}
for key, value in replacements.items():
    template = template.replace(key, value)
Path(os.environ["SITE_AVAILABLE"]).write_text(template, encoding="utf-8")
PY

ln -sfn "$SITE_AVAILABLE" "$SITE_ENABLED"
nginx -t
systemctl reload nginx
curl -fsS -o /dev/null --max-time 5 http://127.0.0.1/api/health
echo "nginx_public_hardening_installed:${SITE_NAME}"
