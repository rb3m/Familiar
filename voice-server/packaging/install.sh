#!/bin/bash
# Install this checkout in place as the current user's launchd agent.
set -euo pipefail

if [[ "$(uname -s)" != Darwin ]]; then
    echo "This installer requires macOS." >&2
    exit 1
fi
project_dir="$(cd "$(dirname "$0")/.." && pwd -P)"
config_dir="$HOME/.config/familiar-voice"
agent_dir="$HOME/Library/LaunchAgents"
log_dir="$HOME/Library/Logs"
plist_path="$agent_dir/com.familiar.voice-server.plist"
mkdir -p "$config_dir" "$agent_dir" "$log_dir"
if [[ ! -f "$config_dir/config.yaml" ]]; then
    cp "$project_dir/config.example.yaml" "$config_dir/config.yaml"
fi
if [[ ! -s "$config_dir/token" ]]; then
    echo "Create $config_dir/token with your shared bearer token, then rerun this installer." >&2
    exit 1
fi
chmod 600 "$config_dir/token"
cd "$project_dir"
uv sync --extra worker

# plistlib escapes XML metacharacters in paths and secrets; never echo the token.
.venv/bin/python - "$project_dir" "$config_dir" "$plist_path" "$log_dir" <<'PY'
import os
import plistlib
import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from familiar_voice_server.config import load_config

project, directory, destination, logs = map(Path, sys.argv[1:])
config = load_config(directory / "config.yaml")
token = (directory / "token").read_text().strip()
if not token:
    raise SystemExit("The token file must contain a non-empty bearer token")
values = {
    "PYTHON": str(project / ".venv/bin/python"),
    "PROJECT": str(project),
    "CONFIG": str(config.path),
    "TOKEN_ENV": config.token_env,
    "TOKEN": token,
    "LOG": str(logs / "familiar-voice-server.log"),
}
content = (project / "packaging/com.familiar.voice-server.plist.template").read_text()
content = re.sub(r"@@([A-Z_]+)@@", lambda match: escape(values[match[1]]), content)
plist = plistlib.loads(content.encode())
# Restrict permissions before writing, including on repeat installations.
fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "wb") as output:
    os.chmod(destination, 0o600)
    plistlib.dump(plist, output)
PY

# A GUI login session gives the gui/ domain; a headless Mac only has the user/ domain.
domain="gui/$(id -u)"
launchctl print "$domain" >/dev/null 2>&1 || domain="user/$(id -u)"
service="$domain/com.familiar.voice-server"
# Reload rather than kickstart, so changes to the plist (paths, token) take effect.
if launchctl print "$service" >/dev/null 2>&1; then
    launchctl bootout "$service"
    sleep 1
fi
launchctl bootstrap "$domain" "$plist_path"
echo "Installed $service; log: $log_dir/familiar-voice-server.log"
