#!/usr/bin/env bash
# Install ~/Applications/Familiar.app for the current user (no root needed). Opening it starts a
# desk voice chat in Terminal; pass other arguments to bake them in, e.g.:
#   install.sh voice --game ats
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/Familiar.app"
ARGS="${*:-voice}"
mkdir -p "$HOME/Applications"
chmod +x "$HERE/familiar-launcher.sh"
rm -rf "$APP"
osacompile -o "$APP" -e "do shell script quoted form of \"$HERE/familiar-launcher.sh\" & \" $ARGS\""
echo "Installed: $APP (runs: familiar-launcher.sh $ARGS)"
