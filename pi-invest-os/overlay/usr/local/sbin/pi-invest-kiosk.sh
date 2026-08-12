#!/usr/bin/env bash
# Launch Chromium in kiosk mode against the local dashboard.
set -euo pipefail

DASH_URL="${PI_INVEST_KIOSK_URL:-http://127.0.0.1:8787}"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
mkdir -p "$RUNTIME_DIR"
export XDG_RUNTIME_DIR="$RUNTIME_DIR"
export XDG_SESSION_TYPE="${XDG_SESSION_TYPE:-wayland}"

# Wait for dashboard HTTP
for _ in $(seq 1 90); do
  if curl -fsS -o /dev/null --max-time 2 "$DASH_URL/api/health" 2>/dev/null \
    || curl -fsS -o /dev/null --max-time 2 "$DASH_URL" 2>/dev/null; then
    break
  fi
  sleep 2
done

CHROMIUM="$(command -v chromium || command -v chromium-browser || true)"
if [[ -z "$CHROMIUM" ]]; then
  echo "chromium not installed" >&2
  exit 1
fi

ARGS=(
  --kiosk
  --noerrdialogs
  --disable-infobars
  --disable-session-crashed-bubble
  --check-for-update-interval=31536000
  --password-store=basic
  --no-first-run
  --ozone-platform=wayland
  "$DASH_URL"
)

if command -v cage >/dev/null 2>&1; then
  exec cage -s -- "$CHROMIUM" "${ARGS[@]}"
fi

# Fallback: X11 + openbox
if command -v startx >/dev/null 2>&1 && command -v openbox >/dev/null 2>&1; then
  export XDG_SESSION_TYPE=x11
  cat >"$RUNTIME_DIR/pi-invest-xinitrc" <<EOF
#!/bin/bash
unclutter -idle 3 -root &
openbox &
exec $CHROMIUM --kiosk --noerrdialogs --disable-infobars \\
  --check-for-update-interval=31536000 --password-store=basic --no-first-run \\
  $DASH_URL
EOF
  chmod +x "$RUNTIME_DIR/pi-invest-xinitrc"
  exec startx "$RUNTIME_DIR/pi-invest-xinitrc" -- :0 vt7 -nocursor
fi

echo "No kiosk compositor (cage) or X11 stack available" >&2
exit 1
