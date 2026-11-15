#!/usr/bin/env bash
# Start Familiar. `familiar-launcher.sh gui` opens the GUI launcher (no terminal window);
# anything else opens a terminal running `familiar <args>`, e.g.:
#   familiar-launcher.sh voice --game ats --persona ember
# The command runs in an interactive zsh/bash so API keys exported in ~/.zshrc or ~/.bashrc
# are available, from the repo directory so ./config.toml is used.
set -euo pipefail
REPO="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)"
ARGS="${*:-voice}"
SHELL_BIN="$(command -v zsh || command -v bash)"
if [[ "${1:-}" == "gui" ]]; then
  exec "$SHELL_BIN" -ic "cd '$REPO' && exec uv run familiar $ARGS"
fi
INNER="cd '$REPO' && uv run familiar $ARGS; echo; read -r -p 'Familiar has stopped. Press Enter to close… ' _"
[[ "$SHELL_BIN" == *zsh ]] && INNER="cd '$REPO' && uv run familiar $ARGS; echo; read -r '?Familiar has stopped. Press Enter to close… '"
TERMINAL="$(command -v konsole || command -v gnome-terminal || command -v xterm)"
case "$TERMINAL" in
  *konsole) exec konsole --workdir "$REPO" -p tabtitle=Familiar -e "$SHELL_BIN" -ic "$INNER" ;;
  *gnome-terminal) exec gnome-terminal --title=Familiar -- "$SHELL_BIN" -ic "$INNER" ;;
  *) exec xterm -T Familiar -e "$SHELL_BIN" -ic "$INNER" ;;
esac
