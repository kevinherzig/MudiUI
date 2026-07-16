#!/bin/sh
# MudiUI installer — run ON the GL-E5800 "Mudi" (BusyBox ash).
#
#   cd <dir containing this script + the 4 source files>
#   sh install.sh
#
# Idempotent: safe to re-run — doubles as the update mechanism (overwrites in
# place, re-enables services, keeps sysupgrade.conf clean). Offline-tolerant:
# only touches the network if a required package is actually missing.
#
# Set MUDI_FORCE=1 to bypass the model check (geometry check still applies).
set -eu

SRC_DIR=$(cd "$(dirname "$0")" && pwd)

BIN_DIR=/usr/bin
INIT_DIR=/etc/init.d
SYSUP=/etc/sysupgrade.conf

# source_file  dest_path  mode
FILES="
mudi.py:$BIN_DIR/mudi.py:0755
mudi-watch.py:$BIN_DIR/mudi-watch.py:0755
mudi.init:$INIT_DIR/mudi:0755
mudi-watch.init:$INIT_DIR/mudi-watch:0755
"

# opkg deps (python3-pillow is installed separately with --nodeps, see below).
DEPS="python3-light python3-numpy python3-urllib python3-logging python3-ctypes python3-cffi python3-evdev"

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

# --- 1. preflight guard -----------------------------------------------------
say "Preflight checks"

[ "$(id -u)" = 0 ] || die "must run as root"

# strong gate: the Mudi's front panel is a 240x320 framebuffer. Headless GL
# routers (e.g. axt1800) have no /dev/fb0 at all, so this alone blocks them.
[ -e /dev/fb0 ] || die "no /dev/fb0 — this box has no front panel; wrong device?"
geom=$(cat /sys/class/graphics/fb0/virtual_size 2>/dev/null || echo "?")
[ "$geom" = "240,320" ] || die "/dev/fb0 is ${geom}, expected 240,320 — wrong device?"

# corroborating gate: model string. Soft-bypassable for forks/new revisions.
model=$(cat /proc/gl-hw-info/model 2>/dev/null || cat /tmp/sysinfo/model 2>/dev/null || echo "")
case "$model" in
  *[eE]5800*|*[mM]udi*) : ;;
  "") warn "could not read model string; relying on 240x320 panel check" ;;
  *)  if [ "${MUDI_FORCE:-0}" = 1 ]; then
        warn "model '$model' is not an E5800, continuing (MUDI_FORCE=1)"
      else
        die "model '$model' is not an E5800/Mudi. Set MUDI_FORCE=1 to override."
      fi ;;
esac

# all source files must be present before we change anything
for row in $FILES; do
  src=${row%%:*}
  [ -f "$SRC_DIR/$src" ] || die "missing source file: $SRC_DIR/$src"
done
say "OK — $model, ${geom} panel, all sources present"

# --- 2. dependencies --------------------------------------------------------
say "Checking dependencies"

installed() { opkg list-installed 2>/dev/null | grep -q "^$1 "; }

missing=""
for p in $DEPS; do installed "$p" || missing="$missing $p"; done
installed python3-pillow || missing="$missing python3-pillow"

if [ -n "$missing" ]; then
  say "Missing:$missing"
  if opkg update; then :; else
    warn "opkg update failed (no uplink?) — trying with cached lists"
  fi
  for p in $missing; do
    if [ "$p" = python3-pillow ]; then
      # pillow clashes with gl-sdk4-screen-large on libfreetype.so.6 — install
      # --nodeps so it reuses GL's freetype instead of overwriting it.
      opkg install --nodeps python3-pillow || die "failed to install python3-pillow"
    else
      opkg install "$p" || die "failed to install $p"
    fi
  done
else
  say "All dependencies already present — skipping network"
fi

# --- 3. deploy files --------------------------------------------------------
say "Installing files"
for row in $FILES; do
  src=${row%%:*}; rest=${row#*:}; dest=${rest%%:*}; mode=${rest#*:}
  cp "$SRC_DIR/$src" "$dest"
  chmod "$mode" "$dest"
  printf '    %s -> %s\n' "$src" "$dest"
done

# --- 3b. default settings (uci) — never clobber existing user settings ------
if [ -f "$SRC_DIR/mudi.config" ]; then
  if [ -f /etc/config/mudi ]; then
    say "/etc/config/mudi exists — keeping your settings"
  else
    cp "$SRC_DIR/mudi.config" /etc/config/mudi
    say "installed default settings -> /etc/config/mudi"
  fi
fi
# /etc/config/* is already in the default sysupgrade backup, so no sysupgrade.conf entry needed.

# --- 4. enable services + persist across firmware upgrades ------------------
say "Enabling services"
"$INIT_DIR/mudi" enable
"$INIT_DIR/mudi-watch" enable

say "Registering files in $SYSUP (survive firmware upgrade)"
touch "$SYSUP"
for row in $FILES; do
  rest=${row#*:}; dest=${rest%%:*}
  grep -qxF "$dest" "$SYSUP" || echo "$dest" >> "$SYSUP"
done

# verify gl_screen still starts before mudi (S99) so we grab the panel last.
gl_s=$(ls /etc/rc.d/ 2>/dev/null | sed -n 's/^S\([0-9]*\)gl_screen$/\1/p' | head -n1)
if [ -n "$gl_s" ] && [ "$gl_s" -ge 99 ] 2>/dev/null; then
  warn "gl_screen starts at S$gl_s (>= mudi's S99) — mudi may not grab the panel at boot"
elif [ -n "$gl_s" ]; then
  say "gl_screen starts at S$gl_s, before mudi (S99) — good"
fi

# --- 5. start ---------------------------------------------------------------
say "Starting services"
"$INIT_DIR/mudi-watch" start
"$INIT_DIR/mudi" start   # grabs the framebuffer; gl_screen is frozen behind it

cat <<'EOF'

MudiUI is installed and running.

  * The gauges own the front panel now.
  * Long-press the screen (~1.6s, hold still) to toggle stock UI <-> MudiUI.
  * Manage:  /etc/init.d/mudi {start,stop,restart}
             /etc/init.d/mudi-watch {start,stop,restart}
  * Update:  re-run this installer after editing the sources.
  * Remove:  sh uninstall.sh
EOF
