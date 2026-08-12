#!/usr/bin/env bash
# Run ON an already-flashed Pi to switch to non-cloud / local-only AI.
# Usage (pipe into sudo — do not use plain `| bash`):
#   curl -fsSL https://raw.githubusercontent.com/GutFarms/Japan-central/cursor/pi-invest-os-0b6b/pi-invest-os/scripts/enable-local-ai-on-pi.sh | sudo bash
set -euo pipefail

BRANCH="${PI_INVEST_UPDATE_BRANCH:-cursor/pi-invest-os-0b6b}"
BASE="https://raw.githubusercontent.com/GutFarms/Japan-central/${BRANCH}/pi-invest-os"

if [[ "$(id -u)" -ne 0 ]]; then
  # When installed as a file, re-exec with sudo. When piped (curl | bash),
  # $0 is /usr/bin/bash — re-execing that causes: cannot execute binary file.
  if [[ -f "$0" && -r "$0" && "$0" != bash && "$0" != */bash ]]; then
    exec sudo -E bash "$0" "$@"
  fi
  echo "Run as root. Prefer:" >&2
  echo "  curl -fsSL ${BASE}/scripts/enable-local-ai-on-pi.sh | sudo bash" >&2
  exit 1
fi

echo "==> Fetching local-AI setup script"
tmp="$(mktemp)"
curl -fsSL "${BASE}/scripts/setup-local-ai.sh" -o "$tmp"
install -m 755 "$tmp" /usr/local/sbin/pi-invest-setup-local-ai.sh
rm -f "$tmp"

# Refresh OS-tuned config if agent tree is present
if [[ -d /opt/pi-invest-agent/config ]]; then
  curl -fsSL "${BASE}/config/config.os.yaml" \
    -o /opt/pi-invest-agent/config/config.os.yaml || true
  chown "$(stat -c '%U' /opt/pi-invest-agent):$(stat -c '%G' /opt/pi-invest-agent)" \
    /opt/pi-invest-agent/config/config.os.yaml 2>/dev/null || true
fi

export PI_INVEST_OLLAMA_MODEL="${PI_INVEST_OLLAMA_MODEL:-llama3.2:3b}"
/usr/local/sbin/pi-invest-setup-local-ai.sh

echo
echo "Done. Open the Pi Invest Dashboard (menu or http://127.0.0.1:8787)."
echo "Check:  cd /opt/pi-invest-agent && . .venv/bin/activate && pi-invest status"
