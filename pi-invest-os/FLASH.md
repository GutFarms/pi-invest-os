# Flashing with Raspberry Pi Imager

The release file is **`pi-invest-os-0.5.0-arm64.img.xz`** (~502 MB). Imager’s file picker often **hides `.xz`** unless you change the filter — that is the usual “I don’t see the image” issue.

## Fastest fix (Use custom)

1. Open **Raspberry Pi Imager**
2. **Choose Device** → Raspberry Pi 5
3. **Choose OS** → scroll to the bottom → **Use custom**
4. In the file dialog:
   - Change the file type filter to **All files (*.*)** / **All files**
   - Browse to your Downloads folder
   - Select `pi-invest-os-0.5.0-arm64.img.xz`
5. Choose storage → Next → Write

If the file still does not appear, decompress it first (next section) and select the `.img`.

## Option A — decompress to `.img` (most reliable)

### Windows
1. Install [7-Zip](https://www.7-zip.org/)
2. Right-click `pi-invest-os-0.5.0-arm64.img.xz` → **7-Zip** → **Extract Here**
3. You get `pi-invest-os-0.5.0-arm64.img` (~11 GB)
4. In Imager → **Use custom** → select the **`.img`** file

### macOS
```bash
# Terminal
cd ~/Downloads
xz -dk pi-invest-os-0.5.0-arm64.img.xz
# then Use custom → select the .img
```
Or open the `.xz` with **The Unarchiver**.

### Linux
```bash
cd ~/Downloads
xz -dk pi-invest-os-0.5.0-arm64.img.xz
```

## Option B — let Imager download it (custom OS list)

Point Imager at our OS list JSON (no manual file pick):

**JSON URL:**
```
https://raw.githubusercontent.com/GutFarms/Japan-central/cursor/pi-invest-os-0b6b/pi-invest-os/imager/os_list.json
```

- **Imager 1.8+:** app menu / settings → set **OS list repository URL** (wording varies by version) to that URL, then refresh the OS list and pick **Pi Invest OS 0.5.0**
- **CLI:**
  ```bash
  rpi-imager --repo https://raw.githubusercontent.com/GutFarms/Japan-central/cursor/pi-invest-os-0b6b/pi-invest-os/imager/os_list.json
  ```

## Verify the download is real

A failed GitHub download is sometimes saved as an HTML error page (small file). Check size and checksum:

| File | Expected size |
|---|---|
| `.img.xz` | ~502 MB (~526,229,700 bytes) |
| `.img` (after extract) | ~11 GB |

```bash
# Linux / macOS
sha256sum -c pi-invest-os-0.5.0-arm64.sha256
```

Download links:
- Image: https://github.com/GutFarms/Japan-central/releases/download/pi-invest-os-v0.5.0/pi-invest-os-0.5.0-arm64.img.xz
- Checksums: https://github.com/GutFarms/Japan-central/releases/download/pi-invest-os-v0.5.0/pi-invest-os-0.5.0-arm64.sha256

## After flashing

1. Insert the card, attach HDMI + keyboard/mouse
2. First boot takes a while (desktop packages + Ollama model pull — needs network once)
3. Login: `pi` / `change-me` — **change the password**
4. Open the **Pi Invest** desktop app (menu / Desktop icon) — local window, not a web URL
