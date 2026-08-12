#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR=/etc/systemd/system

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo"
  exit 1
fi

# Detect the real user who invoked sudo
TARGET_USER="${SUDO_USER:-pi}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"

# Rewrite WorkingDirectory / ExecStart to this checkout
for unit in pi-invest.service pi-invest-dashboard.service; do
  sed \
    -e "s|User=pi|User=${TARGET_USER}|g" \
    -e "s|/home/pi/Japan-central/pi-invest-agent|${ROOT}|g" \
    "$ROOT/systemd/$unit" > "$UNIT_DIR/$unit"
done

systemctl daemon-reload
systemctl enable pi-invest.service
systemctl restart pi-invest.service

echo "Enabled pi-invest.service for user ${TARGET_USER}"
echo "Optional dashboard: systemctl enable --now pi-invest-dashboard.service"
echo "Logs: journalctl -u pi-invest -f"
