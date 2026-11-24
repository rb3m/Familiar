#!/usr/bin/env bash
# Install ~/Applications/Familiar.app for the current user (no root needed). Opening it starts
# the GUI launcher; pass other arguments to bake them in, e.g.:
#   install.sh voice --game ats
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/Familiar.app"
ARGS="${*:-gui}"
mkdir -p "$HOME/Applications"
chmod +x "$HERE/familiar-launcher.sh"
rm -rf "$APP"
osacompile -o "$APP" -e "do shell script quoted form of \"$HERE/familiar-launcher.sh\" & \" $ARGS\""
# macOS blocks LAN connections from apps without Local Network permission ("No route to host"
# to the voice server). Declaring a usage string makes macOS ask once and list Familiar under
# System Settings > Privacy & Security > Local Network. Editing Info.plist breaks the applet's
# signature, so re-sign it ad hoc.
/usr/libexec/PlistBuddy -c "Add :NSLocalNetworkUsageDescription string Familiar talks to your \
local voice server on this network." "$APP/Contents/Info.plist"
codesign --force --sign - "$APP"
echo "Installed: $APP (runs: familiar-launcher.sh $ARGS)"
