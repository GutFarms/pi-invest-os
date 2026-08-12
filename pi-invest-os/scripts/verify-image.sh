#!/usr/bin/env bash
# Spot-check a built Pi Invest OS image (rootfs + boot FAT).
# Mounts rootfs read-only so checksums are not disturbed.
set -euo pipefail

IMG="${1:?usage: $0 path/to/pi-invest-os-*.img}"
[[ -f "$IMG" ]] || { echo "missing $IMG"; exit 1; }

need_sudo() { [[ $EUID -eq 0 ]] || exec sudo -E bash "$0" "$@"; }
need_sudo "$@"

export MTOOLS_SKIP_CHECK=1
LOOP="$(losetup -f --show -P "$IMG")"
MNT=""
cleanup() {
  [[ -n "$MNT" ]] && umount "$MNT" 2>/dev/null || true
  [[ -n "$MNT" && -d "$MNT" ]] && rmdir "$MNT" 2>/dev/null || true
  losetup -d "$LOOP" 2>/dev/null || true
}
trap cleanup EXIT
partprobe "$LOOP" 2>/dev/null || true
for _ in $(seq 1 40); do
  [[ -b "${LOOP}p2" ]] && break
  partprobe "$LOOP" 2>/dev/null || true
  sleep 0.2
done
[[ -b "${LOOP}p2" ]] || { echo "missing ${LOOP}p2"; exit 1; }

MNT="$(mktemp -d)"
mount -o ro,noload "${LOOP}p2" "$MNT"
echo "version: $(cat "$MNT/etc/pi-invest-os-version")"
test -f "$MNT/opt/pi-invest-agent/pyproject.toml"
test -f "$MNT/opt/pi-invest-agent/src/pi_invest/wallet/__init__.py"
test -f "$MNT/opt/pi-invest-agent/src/pi_invest/web/app.py"
test -x "$MNT/usr/local/sbin/pi-invest-firstboot.sh"
test -x "$MNT/usr/local/sbin/pi-invest-update.sh"
test -x "$MNT/usr/local/sbin/pi-invest-kiosk.sh"
test -x "$MNT/usr/local/sbin/pi-invest-setup-local-ai.sh"
test -f "$MNT/etc/systemd/system/pi-invest-kiosk.service"
test -f "$MNT/etc/systemd/system/pi-invest-update.timer"
test -L "$MNT/etc/systemd/system/multi-user.target.wants/pi-invest-firstboot.service"
# Local-only: update timer must NOT be enabled by default
test ! -e "$MNT/etc/systemd/system/timers.target.wants/pi-invest-update.timer"
grep -q 'ConditionPathExists=/var/lib/pi-invest/firstboot-done' \
  "$MNT/etc/systemd/system/pi-invest.service"
grep -q 'ollama.service' "$MNT/etc/systemd/system/pi-invest.service"
grep -q 'rpd-wayland-core\|Desktop:' "$MNT/usr/local/sbin/pi-invest-firstboot.sh"
grep -q 'do_boot_behaviour B4\|graphical.target' "$MNT/usr/local/sbin/pi-invest-firstboot.sh"
grep -q chromium "$MNT/usr/local/sbin/pi-invest-firstboot.sh"
grep -q 'pi-invest-setup-local-ai\|LOCAL-ONLY' "$MNT/usr/local/sbin/pi-invest-firstboot.sh"
grep -q 'provider: ollama' "$MNT/opt/pi-invest-agent/config/config.os.yaml"
grep -q 'provider: simulator' "$MNT/opt/pi-invest-agent/config/config.os.yaml"
test ! -e "$MNT/etc/systemd/system/multi-user.target.wants/pi-invest.service"
test ! -f "$MNT/opt/pi-invest-agent/.env"
echo "rootfs: OK"
umount "$MNT"
rmdir "$MNT"
MNT=""

mdir -i "${LOOP}p1" :: | grep -qi 'pi-invest.env'
mdir -i "${LOOP}p1" :: | grep -qi 'userconf'
mdir -i "${LOOP}p1" :: | grep -qiE '(^|[[:space:]])ssh([[:space:]]|$)'
mdir -i "${LOOP}p1" :: | grep -qi 'bcm2712-rpi-5-b.dtb'
mtype -i "${LOOP}p1" ::pi-invest.env | grep -q 'PI_INVEST_AUTO_UPDATE=false'
mtype -i "${LOOP}p1" ::pi-invest.env | grep -q 'PI_INVEST_LOCAL_ONLY=true'
mtype -i "${LOOP}p1" ::pi-invest.env | grep -q 'OLLAMA_BASE_URL'
mtype -i "${LOOP}p1" ::pi-invest.env | grep -q 'PI_INVEST_KIOSK_URL'
echo "boot: OK"
echo "VERIFY_OK $IMG"
