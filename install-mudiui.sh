#!/bin/sh
# MudiUI one-line installer bootstrap — run ON the GL-E5800 "Mudi":
#
#   wget -q https://github.com/kevinherzig/MudiUI/releases/latest/download/install-mudiui.sh -O install-mudiui.sh && sh install-mudiui.sh
#
# It downloads the MudiUI source from GitHub and hands off to src/install.sh (which does the
# hardware guard, dependencies, deploy, service enable, and start). Set MUDIUI_REF to install a
# branch/tag other than main.
set -eu

REPO="kevinherzig/MudiUI"
REF="${MUDIUI_REF:-main}"
TARBALL="https://codeload.github.com/$REPO/tar.gz/refs/heads/$REF"
TMP="/tmp/mudiui-src"

echo "MudiUI installer — fetching $REPO ($REF)..."
rm -rf "$TMP"; mkdir -p "$TMP"

# GL.iNet firmware ships wget with SSL; fall back to uclient-fetch / curl if not.
fetch() {  # <url> <outfile>
  if command -v wget >/dev/null 2>&1;          then wget -qO "$2" "$1"
  elif command -v uclient-fetch >/dev/null 2>&1; then uclient-fetch -qO "$2" "$1"
  elif command -v curl >/dev/null 2>&1;          then curl -fsSL "$1" -o "$2"
  else echo "no wget/uclient-fetch/curl available" >&2; return 1
  fi
}

if ! fetch "$TARBALL" "$TMP/src.tar.gz"; then
  echo "download failed: $TARBALL" >&2; exit 1
fi
tar xzf "$TMP/src.tar.gz" -C "$TMP"

DIR="$(find "$TMP" -maxdepth 1 -type d -name 'MudiUI-*' | head -1)"
if [ -z "$DIR" ] || [ ! -f "$DIR/src/install.sh" ]; then
  echo "download/extract failed — src/install.sh not found" >&2; exit 1
fi

echo "Running installer..."
sh "$DIR/src/install.sh"
