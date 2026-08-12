# Pi Invest OS

**Version 0.5** — bootable Raspberry Pi OS with a **full desktop** and a **non-cloud AI income agent** that runs solely on the Pi (Ollama + paper trading + on-device market simulator).

## What you get

| Item | Detail |
|---|---|
| Base OS | Raspberry Pi OS **64-bit** (Trixie) |
| Desktop | Official **Wayland / labwc** desktop with autologin |
| Local AI | **Ollama** on `127.0.0.1:11434` (`llama3.2:3b` by default) |
| App menu | **Pi Invest** local desktop app (native window, not a URL) |
| Agent | `/opt/pi-invest-agent` — paper broker/wallet, simulator quotes |
| Cloud | Off by default (no OpenAI / Alpaca / Coinbase / ntfy / GitHub auto-update) |
| Hostname | `pi-invest` |
| Default user | `pi` / `change-me` (**change immediately**) |

## Flash

See **[FLASH.md](./FLASH.md)** if Raspberry Pi Imager does not show the `.img.xz` file.

**Release:** [Pi Invest OS v0.5.0](https://github.com/GutFarms/Japan-central/releases/tag/pi-invest-os-v0.5.0)

| File | Link |
|---|---|
| Image | [pi-invest-os-0.5.0-arm64.img.xz](https://github.com/GutFarms/Japan-central/releases/download/pi-invest-os-v0.5.0/pi-invest-os-0.5.0-arm64.img.xz) |
| Checksums | [pi-invest-os-0.5.0-arm64.sha256](https://github.com/GutFarms/Japan-central/releases/download/pi-invest-os-v0.5.0/pi-invest-os-0.5.0-arm64.sha256) |

```bash
xz -dk pi-invest-os-0.5.0-arm64.img.xz   # then Imager → Use custom → .img
```

Imager OS list JSON:
```
https://raw.githubusercontent.com/GutFarms/Japan-central/cursor/pi-invest-os-0b6b/pi-invest-os/imager/os_list.json
```

## First boot

1. HDMI + keyboard/mouse recommended; network needed **once** (apt + Ollama model pull).
2. Wait for desktop packages + local model download (can take a while on first boot).
3. Autologin; the **Pi Invest** desktop app opens as a local window (no web address).
4. Change password: `passwd`

After that, the agent does **not** require cloud APIs. Decisions use Ollama on the Pi; trading stays paper unless you deliberately unlock live mode.

### Already flashed? Enable local-only AI

```bash
curl -fsSL https://raw.githubusercontent.com/GutFarms/Japan-central/cursor/pi-invest-os-0b6b/pi-invest-os/scripts/enable-local-ai-on-pi.sh | sudo bash
```

## Local AI controls

```bash
sudo systemctl status ollama
ollama list
pi-invest status
sudo pi-invest-setup-local-ai.sh          # re-run / repair
```

Boot secrets: `/boot/firmware/pi-invest.env` → copied to `/opt/pi-invest-agent/.env`.

| Flag | Default | Meaning |
|---|---|---|
| `PI_INVEST_LOCAL_ONLY` | `true` | Force Ollama + paper stack; block OpenAI |
| `PI_INVEST_AUTO_UPDATE` | `false` | No daily GitHub pull |
| `OLLAMA_MODEL` | `llama3.2:3b` | Local model name |

Optional live quotes: set `PI_INVEST_ALLOW_ONLINE_QUOTES=true` and `market.provider: yahoo` (LLM stays on Ollama). See [LOCAL_AI.md](./LOCAL_AI.md).

## Build yourself

```bash
cd pi-invest-os && ./scripts/build-image.sh
./scripts/verify-image.sh dist/pi-invest-os-*-arm64.img
```

## Safety

Paper trading by default. Not financial advice. Change the default password after first login.

## License

MIT
