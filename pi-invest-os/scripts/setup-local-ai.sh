#!/usr/bin/env bash
# Install and configure on-device AI (Ollama) for Pi Invest — no cloud APIs.
# Safe to re-run. Paths match Pi Invest OS (/opt/pi-invest-agent).
set -euo pipefail

MODEL="${PI_INVEST_OLLAMA_MODEL:-llama3.2:3b}"
AGENT_ROOT="${PI_INVEST_AGENT_ROOT:-/opt/pi-invest-agent}"
ENV_FILE="${AGENT_ROOT}/.env"
CONFIG="${AGENT_ROOT}/config/config.yaml"
OS_CONFIG="${AGENT_ROOT}/config/config.os.yaml"

log() { echo "[pi-invest-local-ai] $*"; }

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

if [[ ! -d "$AGENT_ROOT" ]]; then
  echo "ERROR: agent root missing: $AGENT_ROOT" >&2
  exit 1
fi

TARGET_USER="$(stat -c '%U' "$AGENT_ROOT" 2>/dev/null || echo pi)"
export DEBIAN_FRONTEND=noninteractive

if ! command -v ollama >/dev/null 2>&1; then
  log "Installing Ollama (local LLM runtime)…"
  if curl -fsSL https://ollama.com/install.sh | sh; then
    log "Ollama installed via official installer."
  elif apt-get update -y && apt-get install -y ollama; then
    log "Ollama installed via apt."
  else
    log "ERROR: could not install Ollama. Check network and retry."
    exit 1
  fi
fi

systemctl enable ollama.service 2>/dev/null || true
systemctl start ollama.service 2>/dev/null || true

for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

log "Pulling local model: ${MODEL} (one-time download; needs network)…"
if id ollama >/dev/null 2>&1; then
  sudo -u ollama ollama pull "${MODEL}" || ollama pull "${MODEL}"
else
  ollama pull "${MODEL}"
fi

# Prefer OS-tuned local config
if [[ -f "$OS_CONFIG" ]]; then
  install -o "$TARGET_USER" -g "$TARGET_USER" -m 644 "$OS_CONFIG" "$CONFIG"
fi

# Ensure yaml reflects local stack (preserve universe / risk if present)
python3 - <<PY
from pathlib import Path
import yaml

p = Path("${CONFIG}")
data = yaml.safe_load(p.read_text()) if p.exists() else {}
if not isinstance(data, dict):
    data = {}
data.setdefault("llm", {})
data["llm"]["provider"] = "ollama"
data["llm"]["temperature"] = data["llm"].get("temperature", 0.2)
data["llm"]["timeout_seconds"] = max(int(data["llm"].get("timeout_seconds") or 0), 120)
data.setdefault("market", {})
data["market"]["provider"] = "simulator"
data.setdefault("broker", {})
data["broker"]["backend"] = "paper"
data.setdefault("wallet", {})
data["wallet"]["backend"] = "paper"
data.setdefault("alerts", {})
data["alerts"]["enabled"] = False
data.setdefault("agent", {})
data["agent"]["mode"] = "paper"
data["agent"]["allow_simulator_fallback"] = True
data["agent"]["name"] = data["agent"].get("name") or "pi-income-agent-local"
data.setdefault("schedule", {})
data["schedule"]["prefer_market_hours"] = False
data.setdefault("safety", {})
data["safety"]["journal_enabled"] = True
data["safety"]["audit_enabled"] = True
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(yaml.safe_dump(data, sort_keys=False))
print("wrote", p)
PY
chown "$TARGET_USER:$TARGET_USER" "$CONFIG"
chmod 644 "$CONFIG"

touch "$ENV_FILE"
chown "$TARGET_USER:$TARGET_USER" "$ENV_FILE"
chmod 600 "$ENV_FILE"

set_env() {
  local key="$1" val="$2"
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$val" >>"$ENV_FILE"
  fi
}

set_env PI_INVEST_LOCAL_ONLY true
set_env PI_INVEST_AUTO_UPDATE false
set_env OLLAMA_BASE_URL http://127.0.0.1:11434
set_env OLLAMA_MODEL "${MODEL}"
set_env PI_INVEST_OLLAMA_MODEL "${MODEL}"
set_env ALLOW_LIVE_TRADING false
set_env ALLOW_LIVE_TRANSFERS false

# Neutralize cloud API keys (comment out active assignments)
for key in OPENAI_API_KEY ALPACA_API_KEY ALPACA_SECRET_KEY COINBASE_API_KEY COINBASE_API_SECRET NTFY_TOPIC; do
  if grep -q "^${key}=.\+" "$ENV_FILE" 2>/dev/null; then
    sed -i "s/^${key}=/# ${key}= (disabled — local-only mode)/" "$ENV_FILE" || true
  fi
done

mkdir -p /etc/systemd/system/pi-invest.service.d
cat >/etc/systemd/system/pi-invest.service.d/local-ai.conf <<'EOF'
[Unit]
After=network-online.target ollama.service pi-invest-firstboot.service
Wants=ollama.service

[Service]
Environment=PI_INVEST_LOCAL_ONLY=true
EOF

systemctl daemon-reload
systemctl disable --now pi-invest-update.timer 2>/dev/null || true

if systemctl is-enabled pi-invest.service >/dev/null 2>&1; then
  systemctl restart pi-invest.service 2>/dev/null || true
fi
if systemctl is-enabled pi-invest-dashboard.service >/dev/null 2>&1 \
  || systemctl is-active pi-invest-dashboard.service >/dev/null 2>&1; then
  systemctl restart pi-invest-dashboard.service 2>/dev/null || true
fi

log "Local AI ready on this Pi."
log "  LLM:     Ollama @ 127.0.0.1:11434  model=${MODEL}"
log "  Market:  on-device simulator (offline quotes)"
log "  Broker:  paper  ·  Wallet: paper"
log "  Cloud:   OpenAI / Alpaca / Coinbase / ntfy disabled"
log "  Updates: GitHub auto-update disabled"
log "Dashboard: http://127.0.0.1:8787"
