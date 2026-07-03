#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:-/root/ai-quant-trader/current}"
FILTER_SRC="$REPO_DIR/deploy/fail2ban/aiquant-nginx-probes.conf"
JAIL_SRC="$REPO_DIR/deploy/fail2ban/aiquant-nginx-probes.local"

if [ "$(id -u)" -ne 0 ]; then
  echo "install_fail2ban_requires_root" >&2
  exit 2
fi

if [ ! -f "$FILTER_SRC" ] || [ ! -f "$JAIL_SRC" ]; then
  echo "fail2ban_templates_missing" >&2
  exit 3
fi

apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq fail2ban

install -m 0644 "$FILTER_SRC" /etc/fail2ban/filter.d/aiquant-nginx-probes.conf
install -m 0644 "$JAIL_SRC" /etc/fail2ban/jail.d/aiquant-nginx-probes.local

systemctl enable fail2ban >/dev/null
systemctl restart fail2ban
fail2ban-client status aiquant-nginx-probes
