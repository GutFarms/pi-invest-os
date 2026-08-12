#!/usr/bin/env bash
# First-boot provisioning for Pi Invest OS (Raspberry Pi 5).
# Runs once after networking is up, then disables itself.
set -euo pipefail

LOG=/var/log/pi-invest-firstboot.log
DONE=/var/lib/pi-invest/firstboot-done
AGENT_ROOT=/opt/pi-invest-agent
BOOT_FIRMWARE=/boot/firmware
[[ -d "$BOOT_FIRMWARE" ]] || BOOT_FIRMWARE=/boot
BOOT_ENV="$BOOT_FIRMWARE/pi-invest.env"

exec > >(tee -a "$LOG") 2>&1
echo "==== Pi Invest first-boot $(date -Is) ===="

if [[ -f "$DONE" ]]; then
  echo "Already provisioned ($DONE); exiting"
  systemctl disable pi-invest-firstboot.service || true
  exit 0
fi

mkdir -p /var/lib/pi-invest /etc/systemd/system

detect_user() {
  if getent passwd pi >/dev/null 2>&1; then
    echo pi
    return
  fi
  local home_user
  home_user="$(ls -1 /home 2>/dev/null | head -1 || true)"
  if [[ -n "${home_user:-}" ]] && getent passwd "$home_user" >/dev/null 2>&1; then
    echo "$home_user"
    return
  fi
  useradd -m -s /bin/bash -G sudo,gpio,i2c,spi,video,render,input pi 2>/dev/null \
    || useradd -m -s /bin/bash -G sudo,video,render,input pi
  echo 'pi:change-me' | chpasswd
  echo pi
}

TARGET_USER="$(detect_user)"
TARGET_UID="$(id -u "$TARGET_USER")"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
echo "==> Operator user: $TARGET_USER ($TARGET_HOME) uid=$TARGET_UID"

# Ensure video/input groups for kiosk
usermod -aG video,render,input,sudo "$TARGET_USER" 2>/dev/null || true

echo "==> Waiting for network"
for _ in $(seq 1 90); do
  if getent hosts deb.debian.org >/dev/null 2>&1 || ping -c1 -W2 1.1.1.1 >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

export DEBIAN_FRONTEND=noninteractive
echo "==> Installing system packages (agent + desktop + browser + auto-update)"
apt-get update -y
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip python3-dev \
  git curl ca-certificates rsync build-essential libffi-dev \
  avahi-daemon \
  unattended-upgrades apt-listchanges \
  chromium \
  fonts-liberation fonts-dejavu-core \
  gvfs mousepad
# Native desktop app window (WebKit) — optional if packages missing
apt-get install -y --no-install-recommends \
  python3-webview gir1.2-webkit2-4.1 \
  || apt-get install -y --no-install-recommends gir1.2-webkit2-4.0 || true

echo "==> Installing Raspberry Pi desktop (Wayland / labwc)"
# Trixie metapackages (replaces legacy raspberrypi-ui-mods)
if ! apt-get install -y --no-install-recommends \
  rpd-wayland-core rpd-theme rpd-preferences rpd-utilities; then
  echo "WARN: rpd-wayland-core failed; trying XFCE fallback"
  apt-get install -y --no-install-recommends \
    xfce4 xfce4-terminal lightdm chromium || true
fi

# Optional extras (ignore failures on Lite repos)
apt-get install -y --no-install-recommends \
  rpd-applications rpd-graphics || true

# Keep cage available as optional fullscreen kiosk (disabled when desktop is on)
apt-get install -y --no-install-recommends cage seatd || true

# Enable unattended security updates
if [[ -f /etc/apt/apt.conf.d/50unattended-upgrades ]]; then
  cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
fi

if [[ ! -d "$AGENT_ROOT" ]]; then
  echo "ERROR: $AGENT_ROOT missing from image"
  exit 1
fi

# Import secrets / overrides from the boot partition (editable on any PC)
if [[ -f "$BOOT_ENV" ]]; then
  echo "==> Importing $BOOT_ENV"
  install -o "$TARGET_USER" -g "$TARGET_USER" -m 600 "$BOOT_ENV" "$AGENT_ROOT/.env"
else
  echo "==> No boot env found; using packaged defaults"
  if [[ ! -f "$AGENT_ROOT/.env" ]]; then
    install -o "$TARGET_USER" -g "$TARGET_USER" -m 600 \
      "$AGENT_ROOT/.env.example" "$AGENT_ROOT/.env"
  fi
fi

# Ensure OS feature flags exist in .env
ensure_env() {
  local key="$1" val="$2"
  if ! grep -q "^${key}=" "$AGENT_ROOT/.env" 2>/dev/null; then
    printf '\n%s=%s\n' "$key" "$val" >>"$AGENT_ROOT/.env"
  fi
}
# Default: self-contained on-device agent (no GitHub pull, local Ollama AI)
ensure_env PI_INVEST_LOCAL_ONLY true
ensure_env PI_INVEST_AUTO_UPDATE false
ensure_env PI_INVEST_UPDATE_URL https://github.com/GutFarms/Japan-central.git
DEFAULT_BRANCH=master
if [[ -f /etc/pi-invest-os-update-branch ]]; then
  DEFAULT_BRANCH="$(tr -d '[:space:]' </etc/pi-invest-os-update-branch)"
fi
ensure_env PI_INVEST_UPDATE_BRANCH "$DEFAULT_BRANCH"
ensure_env PI_INVEST_KIOSK_URL http://127.0.0.1:8787
ensure_env PI_INVEST_DESKTOP true
ensure_env PI_INVEST_KIOSK false
ensure_env OLLAMA_BASE_URL http://127.0.0.1:11434
ensure_env OLLAMA_MODEL llama3.2:3b
ensure_env PI_INVEST_OLLAMA_MODEL llama3.2:3b
ensure_env PI_INVEST_DESKTOP_TRUST_LOOPBACK true
chown "$TARGET_USER:$TARGET_USER" "$AGENT_ROOT/.env"
chmod 600 "$AGENT_ROOT/.env"

if [[ ! -f "$AGENT_ROOT/config/config.yaml" ]]; then
  if [[ -f "$AGENT_ROOT/config/config.os.yaml" ]]; then
    install -o "$TARGET_USER" -g "$TARGET_USER" -m 644 \
      "$AGENT_ROOT/config/config.os.yaml" "$AGENT_ROOT/config/config.yaml"
  else
    install -o "$TARGET_USER" -g "$TARGET_USER" -m 644 \
      "$AGENT_ROOT/config/config.example.yaml" "$AGENT_ROOT/config/config.yaml"
  fi
fi

chown -R "$TARGET_USER:$TARGET_USER" "$AGENT_ROOT"
mkdir -p "$AGENT_ROOT/data"
chown "$TARGET_USER:$TARGET_USER" "$AGENT_ROOT/data"

# Local desktop application (native window — not a web address)
install -d -m 755 /usr/local/bin /usr/share/applications
if [[ ! -x /usr/local/bin/pi-invest-dashboard ]]; then
  cat >/usr/local/bin/pi-invest-dashboard <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
AGENT_ROOT=/opt/pi-invest-agent
sudo -n systemctl start pi-invest-dashboard.service 2>/dev/null || true
if [[ -x "$AGENT_ROOT/.venv/bin/pi-invest" ]]; then
  cd "$AGENT_ROOT"
  exec "$AGENT_ROOT/.venv/bin/pi-invest" app
fi
exec chromium --class=PiInvest --app=http://127.0.0.1:8787 --no-first-run
EOF
  chmod 755 /usr/local/bin/pi-invest-dashboard
fi
if [[ ! -f /usr/share/applications/pi-invest-dashboard.desktop ]]; then
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
mkdir -p \
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
chown -R "$TARGET_USER:$TARGET_USER" "$TARGET_HOME/Desktop" \
  "$TARGET_HOME/.local" "$TARGET_HOME/.config" 2>/dev/null || true
update-desktop-database /usr/share/applications 2>/dev/null || true

# Allow the desktop app to start the dashboard service without a password prompt
cat >/etc/sudoers.d/pi-invest-dashboard <<EOF
# Managed by Pi Invest OS — start local dashboard from the menu app
${TARGET_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl start pi-invest-dashboard.service, /usr/bin/systemctl restart pi-invest-dashboard.service, /bin/systemctl start pi-invest-dashboard.service, /bin/systemctl restart pi-invest-dashboard.service
EOF
chmod 440 /etc/sudoers.d/pi-invest-dashboard

cat > /etc/systemd/system/pi-invest.service <<EOF
[Unit]
Description=Pi Invest Agent — local on-device income trader
After=network-online.target pi-invest-firstboot.service ollama.service
Wants=network-online.target ollama.service
ConditionPathExists=/var/lib/pi-invest/firstboot-done

[Service]
Type=simple
User=${TARGET_USER}
WorkingDirectory=${AGENT_ROOT}
Environment=PYTHONUNBUFFERED=1
Environment=PI_INVEST_LOCAL_ONLY=true
EnvironmentFile=-${AGENT_ROOT}/.env
ExecStart=${AGENT_ROOT}/.venv/bin/pi-invest run
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/pi-invest-dashboard.service <<EOF
[Unit]
Description=Pi Invest Agent dashboard
After=network-online.target pi-invest.service pi-invest-firstboot.service
ConditionPathExists=/var/lib/pi-invest/firstboot-done

[Service]
Type=simple
User=${TARGET_USER}
WorkingDirectory=${AGENT_ROOT}
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-${AGENT_ROOT}/.env
ExecStart=${AGENT_ROOT}/.venv/bin/pi-invest dashboard
Restart=on-failure
RestartSec=20

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/pi-invest-kiosk.service <<EOF
[Unit]
Description=Pi Invest dashboard kiosk (Chromium fullscreen)
After=pi-invest-dashboard.service network-online.target
Wants=pi-invest-dashboard.service
ConditionPathExists=/var/lib/pi-invest/firstboot-done
ConditionPathExists=/dev/dri/card0

[Service]
Type=simple
User=${TARGET_USER}
Group=${TARGET_USER}
SupplementaryGroups=video render input
PAMName=login
TTYPath=/dev/tty7
TTYReset=yes
TTYVHangup=yes
Environment=PYTHONUNBUFFERED=1
Environment=XDG_RUNTIME_DIR=/run/user/${TARGET_UID}
EnvironmentFile=-${AGENT_ROOT}/.env
ExecStartPre=+mkdir -p /run/user/${TARGET_UID}
ExecStartPre=+chown ${TARGET_UID}:${TARGET_UID} /run/user/${TARGET_UID}
ExecStartPre=+chmod 700 /run/user/${TARGET_UID}
ExecStart=/usr/local/sbin/pi-invest-kiosk.sh
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

echo "==> Creating Python venv + installing agent (+ desktop UI)"
sudo -u "$TARGET_USER" bash -lc "
  set -euo pipefail
  cd '$AGENT_ROOT'
  python3 -m venv .venv
  . .venv/bin/activate
  pip install --upgrade pip wheel
  pip install -e '.[desktop]' || pip install -e . && pip install 'pywebview>=5'
"

echo "==> Smoke test (simulator preview)"
sudo -u "$TARGET_USER" bash -lc "
  cd '$AGENT_ROOT'
  . .venv/bin/activate
  pi-invest once --preview --simulator || true
"

echo "==> Installing on-device AI (Ollama) — no cloud LLM"
if [[ -x /usr/local/sbin/pi-invest-setup-local-ai.sh ]]; then
  PI_INVEST_USER="$TARGET_USER" PI_INVEST_AGENT_ROOT="$AGENT_ROOT" \
    /usr/local/sbin/pi-invest-setup-local-ai.sh || {
      echo "WARN: local AI setup failed; agent will use rules-only until Ollama is installed"
      echo "      Retry: sudo pi-invest-setup-local-ai.sh"
    }
else
  echo "WARN: pi-invest-setup-local-ai.sh missing from image"
fi

echo "==> Enabling services (agent, dashboard, desktop)"
systemctl daemon-reload
systemctl enable pi-invest.service
systemctl enable pi-invest-dashboard.service
# Local-only default: do not enable GitHub auto-update timer
if grep -qiE '^PI_INVEST_AUTO_UPDATE=(true|1)' "$AGENT_ROOT/.env" 2>/dev/null \
  && ! grep -qiE '^PI_INVEST_LOCAL_ONLY=(true|1)' "$AGENT_ROOT/.env" 2>/dev/null; then
  systemctl enable pi-invest-update.timer
else
  systemctl disable pi-invest-update.timer 2>/dev/null || true
fi
systemctl enable unattended-upgrades.service 2>/dev/null || true
systemctl enable seatd.service 2>/dev/null || true

# Boot to graphical desktop with autologin (B4)
systemctl set-default graphical.target || true
if command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_boot_behaviour B4 || true
fi

# LightDM autologin fallback / reinforcement
if [[ -d /etc/lightdm ]]; then
  mkdir -p /etc/lightdm/lightdm.conf.d
  cat >/etc/lightdm/lightdm.conf.d/90-pi-invest.conf <<EOF
[Seat:*]
autologin-user=${TARGET_USER}
autologin-user-timeout=0
user-session=rpd-labwc
greeter-hide-users=false
EOF
  # If XFCE was the fallback, prefer that session when present
  if [[ -f /usr/share/wayland-sessions/xfce-wayland.desktop ]] \
    || [[ -f /usr/share/xsessions/xfce.desktop ]]; then
    sed -i 's/^user-session=.*/user-session=xfce-wayland/' \
      /etc/lightdm/lightdm.conf.d/90-pi-invest.conf || true
  fi
  systemctl enable lightdm.service 2>/dev/null || true
fi

# Desktop owns the display — keep cage kiosk off unless explicitly enabled
if grep -qiE '^PI_INVEST_KIOSK=(true|1)' "$AGENT_ROOT/.env" 2>/dev/null; then
  systemctl enable pi-invest-kiosk.service || true
else
  systemctl disable pi-invest-kiosk.service 2>/dev/null || true
  systemctl mask pi-invest-kiosk.service 2>/dev/null || true
fi

systemctl restart pi-invest.service || true
systemctl restart pi-invest-dashboard.service || true
if systemctl is-enabled pi-invest-update.timer >/dev/null 2>&1; then
  systemctl start pi-invest-update.timer || true
fi

hostnamectl set-hostname pi-invest || true
if ! grep -q 'pi-invest' /etc/hosts 2>/dev/null; then
  echo '127.0.1.1 pi-invest' >> /etc/hosts
fi

date -Is > "$DONE"
chmod 644 "$DONE"
systemctl disable pi-invest-firstboot.service || true

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "==== First-boot complete ===="
echo "Mode:      LOCAL-ONLY AI on this Pi (Ollama + paper + simulator)"
echo "Desktop:   Raspberry Pi Wayland desktop with autologin as ${TARGET_USER}"
echo "App:       Pi Invest (menu / Desktop icon) — local window, not a web URL"
echo "CLI app:   pi-invest app   or   pi-invest-dashboard"
echo "Remote:    ssh -L 8787:127.0.0.1:8787 ${TARGET_USER}@${IP:-pi-invest.local}"
echo "Local AI:  sudo systemctl status ollama ; ollama list"
echo "GitHub auto-update: disabled (PI_INVEST_LOCAL_ONLY=true)"
echo "Manual update:      sudo pi-invest-update.sh --force  (optional)"
echo "Optional kiosk:     set PI_INVEST_KIOSK=true in .env then unmask/enable pi-invest-kiosk"
echo "Agent dir: $AGENT_ROOT"
echo "Secrets:   $AGENT_ROOT/.env"
