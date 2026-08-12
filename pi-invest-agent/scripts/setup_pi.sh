#!/usr/bin/env bash
# Install / refresh the Pi Invest Agent on Raspberry Pi OS.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> Creating venv"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Installing package"
pip install --upgrade pip
pip install -e ".[dev]"

if [[ ! -f config/config.yaml ]]; then
  cp config/config.example.yaml config/config.yaml
  echo "Created config/config.yaml"
fi
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env"
fi

mkdir -p data
echo "==> Smoke test (simulator dry-run)"
pi-invest once --dry-run --simulator

echo
echo "Done. Next:"
echo "  source .venv/bin/activate"
echo "  pi-invest run --simulator          # continuous paper loop"
echo "  sudo ./scripts/install_service.sh  # systemd"
