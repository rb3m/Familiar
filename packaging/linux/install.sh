#!/usr/bin/env bash
# Install the Familiar launcher and icon for the current user (no root needed).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
mkdir -p "$APPS" "$ICONS"
chmod +x "$HERE/familiar-launcher.sh"
install -m 644 "$REPO/assets/familiar.svg" "$ICONS/familiar.svg"
sed "s#@LAUNCHER@#$HERE/familiar-launcher.sh#g" "$HERE/familiar.desktop.in" > "$APPS/familiar.desktop"
chmod 644 "$APPS/familiar.desktop"
command -v desktop-file-validate >/dev/null && desktop-file-validate "$APPS/familiar.desktop" || true
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" || true
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
echo "Installed: $APPS/familiar.desktop (icon: $ICONS/familiar.svg)"
