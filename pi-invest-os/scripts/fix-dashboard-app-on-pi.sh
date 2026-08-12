#!/usr/bin/env bash
# Run ON the Raspberry Pi to install Pi Invest as a local desktop app (no URL).
set -euo pipefail

BRANCH="${PI_INVEST_UPDATE_BRANCH:-cursor/pi-invest-os-0b6b}"
BASE="https://raw.githubusercontent.com/GutFarms/Japan-central/${BRANCH}"

if [[ "$(id -u)" -ne 0 ]]; then
  exec sudo bash "$0" "$@"
fi

TARGET_USER="${SUDO_USER:-${USER:-pi}}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
AGENT_ROOT=/opt/pi-invest-agent

echo "==> Installing desktop GUI deps (webview / webkit)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y >/dev/null 2>&1 || true
apt-get install -y --no-install-recommends \
  python3-webview gir1.2-webkit2-4.1 \
  || apt-get install -y --no-install-recommends gir1.2-webkit2-4.0 || true

if [[ -x "${AGENT_ROOT}/.venv/bin/pip" ]]; then
  sudo -u "$TARGET_USER" "${AGENT_ROOT}/.venv/bin/pip" install -q 'pywebview>=5' || true
  # Refresh agent code if git update helper exists
  if [[ -x /usr/local/sbin/pi-invest-update.sh ]]; then
    PI_INVEST_AUTO_UPDATE=true /usr/local/sbin/pi-invest-update.sh --force || true
  fi
fi

curl -fsSL "${BASE}/pi-invest-os/overlay/usr/local/bin/pi-invest-dashboard" \
  -o /usr/local/bin/pi-invest-dashboard
chmod 755 /usr/local/bin/pi-invest-dashboard

curl -fsSL "${BASE}/pi-invest-os/overlay/usr/share/applications/pi-invest-dashboard.desktop" \
  -o /usr/share/applications/pi-invest-dashboard.desktop

# Ensure loopback desktop trust in .env
if [[ -f "${AGENT_ROOT}/.env" ]]; then
  if ! grep -q '^PI_INVEST_DESKTOP_TRUST_LOOPBACK=' "${AGENT_ROOT}/.env"; then
    echo 'PI_INVEST_DESKTOP_TRUST_LOOPBACK=true' >>"${AGENT_ROOT}/.env"
  fi
fi

install -d -m 755 \
  "$TARGET_HOME/Desktop" \
  "$TARGET_HOME/.local/share/applications" \
  "$TARGET_HOME/.config/autostart"
install -m 644 /usr/share/applications/pi-invest-dashboard.desktop \
  "$TARGET_HOME/.local/share/applications/pi-invest-dashboard.desktop"
install -m 755 /usr/share/applications/pi-invest-dashboard.desktop \
  "$TARGET_HOME/Desktop/pi-invest-dashboard.desktop"
install -m 644 /usr/share/applications/pi-invest-dashboard.desktop \
  "$TARGET_HOME/.config/autostart/pi-invest-dashboard.desktop"

if command -v gio >/dev/null 2>&1; then
  sudo -u "$TARGET_USER" gio set \
    "$TARGET_HOME/Desktop/pi-invest-dashboard.desktop" \
    metadata::trusted true 2>/dev/null || true
fi
chown -R "$TARGET_USER:$TARGET_USER" \
  "$TARGET_HOME/Desktop/pi-invest-dashboard.desktop" \
  "$TARGET_HOME/.local" "$TARGET_HOME/.config" 2>/dev/null || true

cat >/etc/sudoers.d/pi-invest-dashboard <<EOF
${TARGET_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl start pi-invest-dashboard.service, /usr/bin/systemctl restart pi-invest-dashboard.service, /bin/systemctl start pi-invest-dashboard.service, /bin/systemctl restart pi-invest-dashboard.service
EOF
chmod 440 /etc/sudoers.d/pi-invest-dashboard

systemctl enable --now pi-invest-dashboard.service 2>/dev/null || true
update-desktop-database /usr/share/applications 2>/dev/null || true

echo "Done. Open the app from the menu or Desktop icon:  Pi Invest"
echo "  (not a web address — local window on this Pi)"
echo "CLI:  pi-invest-dashboard   or   ${AGENT_ROOT}/.venv/bin/pi-invest app"
