#!/usr/bin/env bash
# Start Familiar on macOS. `familiar-launcher.sh gui` opens the GUI launcher (no Terminal window);
# anything else opens a Terminal window running `familiar <args>`, e.g.:
#   familiar-launcher.sh voice --game ats --persona mira
# Terminal runs the login zsh, so API keys exported in ~/.zshrc are available; the command runs
# from the repo directory so ./config.toml is used.
set -euo pipefail
REPO="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)"
ARGS="${*:-voice}"
if [[ "${1:-}" == "gui" ]]; then
  exec zsh -ic "cd '$REPO' && exec uv run familiar $ARGS"
fi
INNER="cd '$REPO' && clear && uv run familiar $ARGS; echo; read -r '?Familiar has stopped. Press Enter to close… '; exit"
# AppleScript string literal: escape backslashes, then double quotes.
INNER="${INNER//\\/\\\\}"
INNER="${INNER//\"/\\\"}"
exec osascript \
  -e "tell application \"Terminal\" to do script \"$INNER\"" \
  -e 'tell application "Terminal" to activate' >/dev/null
