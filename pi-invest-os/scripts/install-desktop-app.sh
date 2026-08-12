#!/usr/bin/env bash
# Install / refresh Pi Invest as a local desktop application (no browser URL).
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  exec sudo -E bash "$0" "$@"
fi

TARGET_USER="${SUDO_USER:-pi}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
[[ -n "$TARGET_HOME" ]] || TARGET_HOME="/home/$TARGET_USER"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OVERLAY_BIN="$ROOT/overlay/usr/local/bin/pi-invest-dashboard"
OVERLAY_DESKTOP="$ROOT/overlay/usr/share/applications/pi-invest-dashboard.desktop"
AGENT_ROOT=/opt/pi-invest-agent

install -d -m 755 /usr/local/bin /usr/share/applications

if [[ -f "$OVERLAY_BIN" ]]; then
  install -m 755 "$OVERLAY_BIN" /usr/local/bin/pi-invest-dashboard
else
  install -m 755 /dev/stdin /usr/local/bin/pi-invest-dashboard <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
AGENT_ROOT=/opt/pi-invest-agent
sudo -n systemctl start pi-invest-dashboard.service 2>/dev/null || true
exec "$AGENT_ROOT/.venv/bin/pi-invest" app
EOF
fi

if [[ -f "$OVERLAY_DESKTOP" ]]; then
  install -m 644 "$OVERLAY_DESKTOP" /usr/share/applications/pi-invest-dashboard.desktop
else
  cat >/usr/share/applications/pi-invest-dashboard.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Pi Invest
Comment=On-device income agent — local app on this Pi
Exec=/usr/local/bin/pi-invest-dashboard
Icon=utilities-system-monitor
Terminal=false
Categories=Finance;Office;Utility;
StartupNotify=true
StartupWMClass=PiInvest
EOF
fi

apt-get install -y --no-install-recommends python3-webview gir1.2-webkit2-4.1 2>/dev/null || true
if [[ -x "${AGENT_ROOT}/.venv/bin/pip" ]]; then
  sudo -u "$TARGET_USER" "${AGENT_ROOT}/.venv/bin/pip" install -q 'pywebview>=5' || true
fi

install -d -m 755 \
  "$TARGET_HOME/.local/share/applications" \
  "$TARGET_HOME/Desktop" \
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
  "$TARGET_HOME/.local/share/applications" \
  "$TARGET_HOME/Desktop/pi-invest-dashboard.desktop" \
  "$TARGET_HOME/.config/autostart" 2>/dev/null || true

cat >/etc/sudoers.d/pi-invest-dashboard <<EOF
${TARGET_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl start pi-invest-dashboard.service, /usr/bin/systemctl restart pi-invest-dashboard.service, /bin/systemctl start pi-invest-dashboard.service, /bin/systemctl restart pi-invest-dashboard.service
EOF
chmod 440 /etc/sudoers.d/pi-invest-dashboard
update-desktop-database /usr/share/applications 2>/dev/null || true

echo "Installed local app: Pi Invest"
echo "  Menu / Desktop icon — opens a native window (no web address)"
echo "  CLI: pi-invest-dashboard  or  pi-invest app"
