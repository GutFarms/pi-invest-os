#!/usr/bin/env bash
# Build a bootable Raspberry Pi 5 SD image with Pi Invest Agent preinstalled.
#
# Output: dist/pi-invest-os-<version>-arm64.img.xz
#
# Requires: sudo, curl/wget, xz, losetup, mount, rsync, sha256sum
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO_ROOT="$(cd "$ROOT/.." && pwd)"
AGENT_SRC="${AGENT_SRC:-$REPO_ROOT/pi-invest-agent}"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
WORK="${WORK_DIR:-$ROOT/.build}"
DIST="${DIST_DIR:-$ROOT/dist}"
CACHE="${CACHE_DIR:-$ROOT/.cache}"

# Raspberry Pi OS Lite 64-bit (Trixie) — Pi 5 compatible
IMAGE_DIR_URL="${IMAGE_DIR_URL:-https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2026-06-19}"
IMAGE_NAME="${IMAGE_NAME:-2026-06-18-raspios-trixie-arm64-lite.img.xz}"
IMAGE_URL="${IMAGE_URL:-$IMAGE_DIR_URL/$IMAGE_NAME}"

OUT_BASENAME="pi-invest-os-${VERSION}-arm64"
OUT_IMG="$DIST/${OUT_BASENAME}.img"
OUT_XZ="$DIST/${OUT_BASENAME}.img.xz"

die() { echo "ERROR: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing tool: $1"; }

need curl
need xz
need losetup
need mount
need rsync
need sha256sum
need parted
need mcopy
need sudo

export MTOOLS_SKIP_CHECK=1

[[ -d "$AGENT_SRC" ]] || die "agent source not found: $AGENT_SRC"
[[ $EUID -eq 0 ]] || exec sudo -E env \
  "AGENT_SRC=$AGENT_SRC" "WORK_DIR=$WORK" "DIST_DIR=$DIST" "CACHE_DIR=$CACHE" \
  "IMAGE_URL=$IMAGE_URL" "IMAGE_NAME=$IMAGE_NAME" \
  bash "$0" "$@"

mkdir -p "$WORK" "$DIST" "$CACHE"
echo "==> Pi Invest OS image builder v${VERSION}"
echo "    agent: $AGENT_SRC"
echo "    base:  $IMAGE_URL"

ARCHIVE="$CACHE/$IMAGE_NAME"
if [[ ! -f "$ARCHIVE" ]]; then
  echo "==> Downloading base image"
  curl -fL --retry 3 --retry-delay 2 -o "$ARCHIVE.partial" "$IMAGE_URL"
  mv "$ARCHIVE.partial" "$ARCHIVE"
else
  echo "==> Using cached base image $ARCHIVE"
fi

# Optional checksum if published
SUM_URL="${IMAGE_URL}.sha256"
if curl -fsSL -o "$CACHE/${IMAGE_NAME}.sha256" "$SUM_URL" 2>/dev/null; then
  echo "==> Verifying sha256"
  (cd "$CACHE" && sha256sum -c "${IMAGE_NAME}.sha256")
fi

RAW="$WORK/base.img"
echo "==> Decompressing"
rm -f "$RAW"
xz -T0 -dkc "$ARCHIVE" > "$RAW"

# Grow image by 8GiB so first-boot desktop + Chromium packages fit
echo "==> Expanding image +8GiB"
dd if=/dev/zero bs=1M count=8192 status=none >> "$RAW"
LOOP="$(losetup -f --show -P "$RAW")"
cleanup() {
  set +e
  sync
  if [[ -n "${MNT_ROOT:-}" ]]; then
    umount "${MNT_ROOT}/boot/firmware" 2>/dev/null
    umount "${MNT_ROOT}/boot" 2>/dev/null
    umount "$MNT_ROOT" 2>/dev/null
  fi
  if [[ -n "${LOOP:-}" ]]; then
    losetup -d "$LOOP" 2>/dev/null
  fi
}
trap cleanup EXIT

# Some hosts need an explicit partition rescan for loopXpN nodes
partprobe "$LOOP" 2>/dev/null || true
udevadm settle 2>/dev/null || sleep 1

# Wait for partition nodes
for _ in $(seq 1 50); do
  [[ -b "${LOOP}p2" ]] && break
  partprobe "$LOOP" 2>/dev/null || true
  sleep 0.2
done
[[ -b "${LOOP}p2" ]] || die "root partition ${LOOP}p2 not found (loop=$LOOP)"

echo "==> Growing root partition"
# Grow partition 2 to end of disk
parted --script "$LOOP" resizepart 2 100%
partprobe "$LOOP" 2>/dev/null || true
udevadm settle 2>/dev/null || sleep 1
for _ in $(seq 1 50); do
  [[ -b "${LOOP}p2" ]] && break
  partprobe "$LOOP" 2>/dev/null || true
  sleep 0.2
done
[[ -b "${LOOP}p2" ]] || die "root partition missing after resize"
e2fsck -fy "${LOOP}p2"
resize2fs "${LOOP}p2"

MNT_ROOT="$WORK/mnt"
mkdir -p "$MNT_ROOT"
echo "==> Mounting rootfs (ext4)"
mount "${LOOP}p2" "$MNT_ROOT"
# Keep an empty mountpoint for firmware; real boot files go on p1 via mtools
# (this build host may lack kernel vfat support).
mkdir -p "$MNT_ROOT/boot/firmware"

echo "==> Staging agent into /opt/pi-invest-agent"
rm -rf "$MNT_ROOT/opt/pi-invest-agent"
mkdir -p "$MNT_ROOT/opt/pi-invest-agent"
rsync -a \
  --exclude '.venv' \
  --exclude '.pytest_cache' \
  --exclude '__pycache__' \
  --exclude 'data/*.db' \
  --exclude 'data/*.csv' \
  --exclude '.git' \
  "$AGENT_SRC/" "$MNT_ROOT/opt/pi-invest-agent/"

# OS-tuned defaults
install -m 644 "$ROOT/config/config.os.yaml" \
  "$MNT_ROOT/opt/pi-invest-agent/config/config.os.yaml"
# Ship OS systemd templates at /opt for firstboot rewrite reference
mkdir -p "$MNT_ROOT/opt/pi-invest-agent/systemd"
install -m 644 "$ROOT/overlay/etc/systemd/system/pi-invest.service" \
  "$MNT_ROOT/opt/pi-invest-agent/systemd/pi-invest.service"
install -m 644 "$ROOT/overlay/etc/systemd/system/pi-invest-dashboard.service" \
  "$MNT_ROOT/opt/pi-invest-agent/systemd/pi-invest-dashboard.service"

# Mark ownership for default pi user (may be created via userconf)
chown -R 1000:1000 "$MNT_ROOT/opt/pi-invest-agent" 2>/dev/null || true

echo "==> Applying OS overlay"
install -d -m 755 "$MNT_ROOT/usr/local/sbin" "$MNT_ROOT/usr/local/bin" \
  "$MNT_ROOT/usr/share/applications"
install -m 755 "$ROOT/overlay/usr/local/sbin/pi-invest-firstboot.sh" \
  "$MNT_ROOT/usr/local/sbin/pi-invest-firstboot.sh"
install -m 755 "$ROOT/overlay/usr/local/sbin/pi-invest-update.sh" \
  "$MNT_ROOT/usr/local/sbin/pi-invest-update.sh"
install -m 755 "$ROOT/overlay/usr/local/sbin/pi-invest-kiosk.sh" \
  "$MNT_ROOT/usr/local/sbin/pi-invest-kiosk.sh"
install -m 755 "$ROOT/scripts/setup-local-ai.sh" \
  "$MNT_ROOT/usr/local/sbin/pi-invest-setup-local-ai.sh"
install -m 755 "$ROOT/scripts/enable-local-ai-on-pi.sh" \
  "$MNT_ROOT/usr/local/sbin/pi-invest-enable-local-ai.sh"
install -m 755 "$ROOT/overlay/usr/local/bin/pi-invest-dashboard" \
  "$MNT_ROOT/usr/local/bin/pi-invest-dashboard"
install -m 644 "$ROOT/overlay/usr/share/applications/pi-invest-dashboard.desktop" \
  "$MNT_ROOT/usr/share/applications/pi-invest-dashboard.desktop"
install -m 755 "$ROOT/scripts/install-desktop-app.sh" \
  "$MNT_ROOT/usr/local/sbin/pi-invest-install-desktop-app.sh"
install -d -m 755 "$MNT_ROOT/etc/systemd/system"
for unit in \
  pi-invest-firstboot.service \
  pi-invest.service \
  pi-invest-dashboard.service \
  pi-invest-kiosk.service \
  pi-invest-update.service \
  pi-invest-update.timer
do
  install -m 644 "$ROOT/overlay/etc/systemd/system/$unit" \
    "$MNT_ROOT/etc/systemd/system/$unit"
done
install -m 644 "$ROOT/overlay/etc/hostname" "$MNT_ROOT/etc/hostname"
install -m 644 "$ROOT/overlay/etc/motd" "$MNT_ROOT/etc/motd"

# Enable first-boot only (local-only AI — GitHub update timer stays installed but off)
mkdir -p \
  "$MNT_ROOT/etc/systemd/system/multi-user.target.wants" \
  "$MNT_ROOT/etc/systemd/system/timers.target.wants"
ln -sf /etc/systemd/system/pi-invest-firstboot.service \
  "$MNT_ROOT/etc/systemd/system/multi-user.target.wants/pi-invest-firstboot.service"
# Update timer unit is shipped but not enabled; local-only mode skips GitHub pulls.

# Version stamp on rootfs
echo "$VERSION" > "$MNT_ROOT/etc/pi-invest-os-version"
# Prefer updating from the branch this image was built from (avoids rolling
# back to an older master before the OS PR is merged).
UPDATE_BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo master)"
if [[ "$UPDATE_BRANCH" == "HEAD" ]]; then
  UPDATE_BRANCH=master
fi
echo "$UPDATE_BRANCH" > "$MNT_ROOT/etc/pi-invest-os-update-branch"
cat > "$MNT_ROOT/etc/pi-invest-os-release" <<EOF
NAME="Pi Invest OS"
VERSION="$VERSION"
ID=pi-invest-os
VARIANT="Raspberry Pi 5 / arm64 Desktop + local AI app"
AGENT_PATH=/opt/pi-invest-agent
FEATURES="desktop-app,local-ai,ollama,chromium,investment-guard"
UPDATE_BRANCH=$UPDATE_BRANCH
EOF

echo "==> Writing boot partition (FAT via mtools)"
need mcopy
BOOT_DEV="${LOOP}p1"
[[ -b "$BOOT_DEV" ]] || die "boot partition $BOOT_DEV missing"
TMP_BOOT="$WORK/boot-files"
rm -rf "$TMP_BOOT"
mkdir -p "$TMP_BOOT"
cp "$ROOT/boot/pi-invest.env" "$TMP_BOOT/pi-invest.env"
cp "$ROOT/boot/userconf.txt" "$TMP_BOOT/userconf.txt"
: > "$TMP_BOOT/ssh"
echo "$VERSION" > "$TMP_BOOT/pi-invest-os-version.txt"
mcopy -o -i "$BOOT_DEV" "$TMP_BOOT/pi-invest.env" ::pi-invest.env
mcopy -o -i "$BOOT_DEV" "$TMP_BOOT/userconf.txt" ::userconf.txt
mcopy -o -i "$BOOT_DEV" "$TMP_BOOT/ssh" ::ssh
mcopy -o -i "$BOOT_DEV" "$TMP_BOOT/pi-invest-os-version.txt" ::pi-invest-os-version.txt

sync
echo "==> Unmounting"
umount "$MNT_ROOT"
losetup -d "$LOOP"
LOOP=""
trap - EXIT

echo "==> Writing $OUT_IMG"
mkdir -p "$DIST"
mv -f "$RAW" "$OUT_IMG"

echo "==> Compressing $OUT_XZ"
rm -f "$OUT_XZ"
xz -T0 -9 -f -k "$OUT_IMG"
# xz -k keeps .img; also keep xz. Prefer shipping xz only to save space in dist listing
(cd "$DIST" && sha256sum "$(basename "$OUT_XZ")" "$(basename "$OUT_IMG")" > "${OUT_BASENAME}.sha256")

SIZE_XZ="$(du -h "$OUT_XZ" | awk '{print $1}')"
SIZE_IMG="$(du -h "$OUT_IMG" | awk '{print $1}')"
echo
echo "Built Pi Invest OS ${VERSION}"
echo "  $OUT_IMG  ($SIZE_IMG)"
echo "  $OUT_XZ   ($SIZE_XZ)"
echo "  $DIST/${OUT_BASENAME}.sha256"
echo
echo "Flash:"
echo "  xzcat $OUT_XZ | sudo dd of=/dev/sdX bs=4M status=progress conv=fsync"
echo "  # or open in Raspberry Pi Imager → Use custom"
