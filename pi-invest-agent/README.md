# Pi Invest Agent

Autonomous income-seeking investment agent designed for **Raspberry Pi 5**.

It scores tickers for expected income (dividends + capital appreciation signals), asks an optional local/cloud LLM to refine the plan, then routes every order through hard risk limits. **Paper trading is the default.** Live brokerage requires an explicit unlock.

## What it does

1. Pulls market snapshots (Yahoo Finance public quotes; works offline with a built-in simulator).
2. Scores candidates on momentum, dividend yield proxy, volatility risk, and trend.
3. Optionally asks **Ollama** (recommended on Pi 5) or OpenAI to pick/weight ideas.
4. **Investment guard** (Alinia-inspired) rejects LLM ideas that disagree with income scores, prefers income ETFs, and throttles buys on drawdown.
5. Risk gate enforces max position size, daily loss halt, cash reserve, and allowlist.
6. Executes via local paper ledger or Alpaca (paper/live).
7. Exposes a high-graphic FastAPI display dashboard (NAV curve, allocation ring, holdings bars) + CLI.

## Safety defaults

| Control | Default |
|---|---|
| Trading mode | `paper` |
| Live unlock | Off (`ALLOW_LIVE_TRADING=false`) |
| Investment guard | On (score agreement + DD throttle) |
| Wallet transfers | Paper simulation (`wallet.backend: paper`) |
| Live transfers unlock | Off (`ALLOW_LIVE_TRANSFERS=false`) |
| Kill switch | `pi-invest halt` freezes orders + outbound sends |
| Daily send cap | `$250` USD-equivalent (configurable) |
| Dashboard bind | `127.0.0.1` (use Tailscale/SSH; avoid open LAN) |
| Dashboard auth | Admin + optional read-only viewer (HTTP basic) |
| Alerts | Optional ntfy on halt / resume / drawdown |
| Max position | 15% of equity |
| Max daily loss | 3% of equity |
| Cash reserve | 10% kept uninvested |
| Universe | Configurable allowlist only |

### Wallet (USD + crypto)

A separate treasury from the brokerage book supports **send** and **receive** for USD and cryptocurrencies (BTC, ETH, USDC by default).

- Paper mode generates stable local receive addresses and simulates inbound/outbound transfers.
- Bridge commands move USD between the wallet and paper brokerage cash.
- **Coinbase App** connection (CDP ECDSA API keys) for live balances, receive addresses, and gated sends.

```bash
# 1) Create a Coinbase App API key at https://portal.cdp.coinbase.com/
#    Signature algorithm: ECDSA (required). Scopes: view (+ transfer to send).
# 2) Put key name + PEM secret in .env (see .env.example)
# 3) Test:
pi-invest coinbase status
pi-invest coinbase address BTC

# 4) Switch the agent wallet to Coinbase in config/config.yaml:
#    wallet.backend: coinbase
# 5) Live sends still require ALLOW_LIVE_TRANSFERS=true
```

> This is software for experimentation. It is **not** financial advice. Automated trading and transfers can lose money. Start in paper mode and only unlock live capital if you understand the risks.

## Raspberry Pi 5 quick start

### Option A — bootable OS image (v0.4)

Flash **Pi Invest OS** — download the prebuilt image from the
[v0.4.0 release](https://github.com/GutFarms/Japan-central/releases/tag/pi-invest-os-v0.4.0)
([`.img.xz`](https://github.com/GutFarms/Japan-central/releases/download/pi-invest-os-v0.4.0/pi-invest-os-0.4.0-arm64.img.xz))
or build from [`../pi-invest-os/`](../pi-invest-os/).

```bash
# download + flash, or:
cd ../pi-invest-os && ./scripts/build-image.sh
# Raspberry Pi Imager → Use custom
```

### Option B — install onto an existing Pi OS

```bash
# On the Pi (Bookworm / 64-bit recommended)
sudo apt update
sudo apt install -y python3-venv python3-pip git

git clone <your-repo-url>
cd Japan-central/pi-invest-agent

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp config/config.example.yaml config/config.yaml
cp .env.example .env

# Dry-run / preview one decision cycle (simulator, no network required)
pi-invest once --preview

# Continuous paper loop
pi-invest run

# Native desktop app on the Pi (preferred — no browser URL)
pi-invest app

# Backend only (optional SSH tunnel / service)
pi-invest dashboard
```

### Local-only AI (no cloud)

Run the agent solely on the Pi with Ollama — no OpenAI, Alpaca, Coinbase, or ntfy:

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.2:3b
# config: llm.provider: ollama, market.provider: simulator, broker/wallet: paper
echo 'PI_INVEST_LOCAL_ONLY=true' >> .env
```

On Pi Invest OS, first boot installs Ollama automatically. Existing flashes:

```bash
curl -fsSL https://raw.githubusercontent.com/GutFarms/Japan-central/cursor/pi-invest-os-0b6b/pi-invest-os/scripts/enable-local-ai-on-pi.sh | sudo bash
```

Install as a systemd service:

```bash
sudo ./scripts/install_service.sh
sudo systemctl enable --now pi-invest
journalctl -u pi-invest -f
```

## CLI

```bash
pi-invest once          # single research + trade cycle
pi-invest once --preview  # score + planned orders, no fills
pi-invest run           # scheduled loop
pi-invest status        # portfolio + wallet + recent decisions
pi-invest app           # native desktop window on the Pi
pi-invest dashboard     # backend service (used by the app)
pi-invest reset-paper   # wipe paper brokerage ledger (keeps config)

# USD + crypto wallet
pi-invest wallet balances
pi-invest wallet receive BTC
pi-invest wallet credit BTC --amount 0.01 --from alice
pi-invest wallet send USD --amount 25 --to usd:friend:abc123
pi-invest wallet history
pi-invest wallet bridge-to-broker --amount 100    # wallet USD → brokerage cash
pi-invest wallet bridge-from-broker --amount 50   # brokerage cash → wallet USD

# Safety + performance
pi-invest halt --reason "stepping away"
pi-invest resume
pi-invest journal
pi-invest export-journal --path data/journal.csv

# Secured sends
pi-invest wallet allowlist-add usd:friend:abc --label "Friend"
pi-invest wallet send USD --amount 25 --to usd:friend:abc --confirm "SEND 25.00 USD"
```

Set `DASHBOARD_READONLY_PASSWORD` for a viewer login that can see status/NAV but cannot send, halt, run cycles, or edit the allowlist. Set `NTFY_TOPIC` to get push alerts on halt/resume and drawdown.
## Config

Edit `config/config.yaml` (copied from the example):

- `universe` — tickers the agent may touch
- `risk.*` — hard caps
- `broker.backend` — `paper` or `alpaca`
- `llm.provider` — `none` | `ollama` | `openai`
- `schedule.interval_minutes` — how often to reassess

Secrets go in `.env` (never commit):

```
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
OPENAI_API_KEY=
ALLOW_LIVE_TRADING=false
```

## Project layout

```
pi-invest-agent/
  src/pi_invest/
    agent/       # scoring brain + risk gate + orchestrator
    broker/      # paper + Alpaca adapters
    wallet/      # USD + crypto send/receive treasury
    data/        # market snapshots
    storage/     # SQLite trade/decision/transfer log
    web/         # FastAPI dashboard
    alerts.py    # optional ntfy push notifications
  config/
  systemd/
  scripts/
  tests/
```

## License

MIT — use at your own risk.
