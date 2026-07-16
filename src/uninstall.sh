#!/bin/sh
# MudiUI uninstaller — run ON the GL-E5800 "Mudi". Reverses install.sh:
# stops + disables both services, removes the files, strips sysupgrade.conf,
# and hands the panel back to gl_screen. Idempotent.
set -eu

INIT_DIR=/etc/init.d
SYSUP=/etc/sysupgrade.conf

TARGETS="/usr/bin/mudi.py /usr/bin/mudi-watch.py $INIT_DIR/mudi $INIT_DIR/mudi-watch"

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m  %s\n' "$*" >&2; }

[ "$(id -u)" = 0 ] || { echo "must run as root" >&2; exit 1; }

say "Stopping and disabling services"
for svc in mudi mudi-watch; do
  if [ -x "$INIT_DIR/$svc" ]; then
    "$INIT_DIR/$svc" stop    2>/dev/null || true
    "$INIT_DIR/$svc" disable 2>/dev/null || true
  fi
done

# make sure no stray process is still holding /dev/fb0
for p in $(pidof python3 2>/dev/null); do
  if grep -qs mudi "/proc/$p/cmdline"; then kill "$p" 2>/dev/null || true; fi
done

say "Removing files"
for f in $TARGETS; do
  [ -e "$f" ] && { rm -f "$f"; printf '    removed %s\n' "$f"; }
done
[ -e /etc/config/mudi ] && { rm -f /etc/config/mudi; printf '    removed %s\n' /etc/config/mudi; }

if [ -f "$SYSUP" ]; then
  say "Cleaning $SYSUP"
  tmp="$SYSUP.mudi.$$"
  grep -vxF -e /usr/bin/mudi.py -e /usr/bin/mudi-watch.py \
            -e "$INIT_DIR/mudi" -e "$INIT_DIR/mudi-watch" "$SYSUP" > "$tmp" || true
  mv "$tmp" "$SYSUP"
fi

say "Restoring stock UI (gl_screen)"
# unblank in case a mudi process left the panel blanked, then start gl_screen
echo 0 > /sys/class/graphics/fb0/blank 2>/dev/null || true
if [ -x "$INIT_DIR/gl_screen" ]; then
  "$INIT_DIR/gl_screen" restart 2>/dev/null || "$INIT_DIR/gl_screen" start 2>/dev/null || true
else
  warn "gl_screen init not found — start the stock UI manually if needed"
fi

echo
say "MudiUI removed."
