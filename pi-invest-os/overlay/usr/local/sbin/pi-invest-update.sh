#!/usr/bin/env bash
# Pull the latest pi-invest-agent from GitHub and restart services.
# Preserves .env, config/config.yaml, data/, and .venv.
set -euo pipefail

LOG=/var/log/pi-invest-update.log
AGENT_ROOT=/opt/pi-invest-agent
ENV_FILE="${AGENT_ROOT}/.env"
STATE_DIR=/var/lib/pi-invest

mkdir -p "$STATE_DIR"
exec >>"$LOG" 2>&1
echo "==== Pi Invest update $(date -Is) ===="

# Load optional overrides from agent .env
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

LOCAL_ONLY="${PI_INVEST_LOCAL_ONLY:-false}"
AUTO="${PI_INVEST_AUTO_UPDATE:-false}"
if [[ "${1:-}" != "--force" ]]; then
  if [[ "${LOCAL_ONLY,,}" == "true" || "${LOCAL_ONLY}" == "1" ]]; then
    echo "Local-only mode (PI_INVEST_LOCAL_ONLY=${LOCAL_ONLY}); skipping GitHub update"
    exit 0
  fi
  if [[ "${AUTO,,}" != "true" && "${AUTO}" != "1" ]]; then
    echo "Auto-update disabled (PI_INVEST_AUTO_UPDATE=${AUTO}); exiting"
    exit 0
  fi
fi

REPO_URL="${PI_INVEST_UPDATE_URL:-https://github.com/GutFarms/Japan-central.git}"
if [[ -z "${PI_INVEST_UPDATE_BRANCH:-}" && -f /etc/pi-invest-os-update-branch ]]; then
  PI_INVEST_UPDATE_BRANCH="$(tr -d '[:space:]' </etc/pi-invest-os-update-branch)"
fi
BRANCH="${PI_INVEST_UPDATE_BRANCH:-master}"
TARGET_USER="$(stat -c '%U' "$AGENT_ROOT" 2>/dev/null || echo pi)"

if [[ ! -d "$AGENT_ROOT" ]]; then
  echo "ERROR: $AGENT_ROOT missing"
  exit 1
fi

echo "==> Fetching ${REPO_URL} @ ${BRANCH}"
TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

if ! git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$TMP/repo"; then
  echo "WARN: branch ${BRANCH} failed; trying default branch"
  git clone --depth 1 "$REPO_URL" "$TMP/repo"
fi

if [[ ! -d "$TMP/repo/pi-invest-agent" ]]; then
  echo "ERROR: pi-invest-agent/ not found in repo"
  exit 1
fi

echo "==> Syncing agent sources (preserving .env, config.yaml, data, .venv)"
rsync -a \
  --delete \
  --exclude '.venv/' \
  --exclude 'data/' \
  --exclude '.env' \
  --exclude 'config/config.yaml' \
  --exclude '.pytest_cache/' \
  --exclude '__pycache__/' \
  --exclude '*.egg-info/' \
  "$TMP/repo/pi-invest-agent/" "$AGENT_ROOT/"

# Refresh OS-tuned example if present in repo
if [[ -f "$TMP/repo/pi-invest-os/config/config.os.yaml" ]]; then
  install -m 644 "$TMP/repo/pi-invest-os/config/config.os.yaml" \
    "$AGENT_ROOT/config/config.os.yaml"
fi

# Refresh firstboot/update helpers when shipped in repo
if [[ -f "$TMP/repo/pi-invest-os/overlay/usr/local/sbin/pi-invest-update.sh" ]]; then
  install -m 755 "$TMP/repo/pi-invest-os/overlay/usr/local/sbin/pi-invest-update.sh" \
    /usr/local/sbin/pi-invest-update.sh
fi
if [[ -f "$TMP/repo/pi-invest-os/overlay/usr/local/sbin/pi-invest-kiosk.sh" ]]; then
  install -m 755 "$TMP/repo/pi-invest-os/overlay/usr/local/sbin/pi-invest-kiosk.sh" \
    /usr/local/sbin/pi-invest-kiosk.sh
fi
if [[ -f "$TMP/repo/pi-invest-os/overlay/usr/local/bin/pi-invest-dashboard" ]]; then
  install -d -m 755 /usr/local/bin /usr/share/applications
  install -m 755 "$TMP/repo/pi-invest-os/overlay/usr/local/bin/pi-invest-dashboard" \
    /usr/local/bin/pi-invest-dashboard
fi
if [[ -f "$TMP/repo/pi-invest-os/overlay/usr/share/applications/pi-invest-dashboard.desktop" ]]; then
  install -m 644 \
    "$TMP/repo/pi-invest-os/overlay/usr/share/applications/pi-invest-dashboard.desktop" \
    /usr/share/applications/pi-invest-dashboard.desktop
  update-desktop-database /usr/share/applications 2>/dev/null || true
fi
if [[ -f "$TMP/repo/pi-invest-os/scripts/install-desktop-app.sh" ]]; then
  install -m 755 "$TMP/repo/pi-invest-os/scripts/install-desktop-app.sh" \
    /usr/local/sbin/pi-invest-install-desktop-app.sh
  /usr/local/sbin/pi-invest-install-desktop-app.sh || true
fi

chown -R "${TARGET_USER}:${TARGET_USER}" "$AGENT_ROOT"

echo "==> Reinstalling Python package"
sudo -u "$TARGET_USER" bash -lc "
  set -euo pipefail
  cd '$AGENT_ROOT'
  if [[ ! -d .venv ]]; then
    python3 -m venv .venv
  fi
  . .venv/bin/activate
  pip install --upgrade pip wheel
  pip install -e '.[desktop]' || { pip install -e .; pip install 'pywebview>=5'; }
"

echo "==> Restarting services"
systemctl daemon-reload || true
systemctl try-restart pi-invest.service || true
systemctl try-restart pi-invest-dashboard.service || true
systemctl try-restart pi-invest-kiosk.service || true

date -Is > "$STATE_DIR/last-update"
echo "==== Update complete ===="
