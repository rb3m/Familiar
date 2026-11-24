#!/usr/bin/env bash
# Keep an SSH tunnel from this Mac to the local voice server (ADR 0030).
#
# macOS Local Network privacy blocks Familiar.app from reaching LAN hosts, and an osacompile
# applet can't be granted that permission. launchd daemons are exempt, so a daemon (running as
# you, not root) holds `ssh -L 127.0.0.1:<port> -> <host>:8765`. Familiar then talks to loopback;
# nothing else on the network becomes reachable, and the traffic is encrypted.
#
#   sudo packaging/macos/install-voice-tunnel.sh you@voice-server-ip [~/.ssh/your_key] [8766]
#   sudo packaging/macos/install-voice-tunnel.sh --uninstall
#
# Then set [tts.local] url = "http://127.0.0.1:8766" in config.toml.
# Needs: a passphrase-free key the server accepts, and the host already in your known_hosts
# (ssh to it once from Terminal).
set -euo pipefail

LABEL="com.familiar.voice-tunnel"
PLIST="/Library/LaunchDaemons/$LABEL.plist"
REMOTE_PORT=8765

fail() { echo "error: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "run with sudo"
OWNER="${SUDO_USER:-}"
[[ -n "$OWNER" && "$OWNER" != root ]] || fail "run with sudo from your own account"
OWNER_HOME="$(dscl . -read "/Users/$OWNER" NFSHomeDirectory | awk '{print $2}')"

launchctl bootout "system/$LABEL" 2>/dev/null || true
if [[ "${1:-}" == "--uninstall" ]]; then
  rm -f "$PLIST"
  echo "Removed $LABEL"
  exit 0
fi

TARGET="${1:-}"
[[ "$TARGET" == *@* ]] || fail "usage: sudo $0 user@host [identity_file] [local_port]"
HOST="${TARGET#*@}"
KEY="${2:-$OWNER_HOME/.ssh/id_ed25519}"
KEY="${KEY/#\~/$OWNER_HOME}"
LOCAL_PORT="${3:-8766}"
KNOWN_HOSTS="$OWNER_HOME/.ssh/known_hosts"
LOG="$OWNER_HOME/Library/Logs/familiar-voice-tunnel.log"

[[ -r "$KEY" ]] || fail "identity file not found: $KEY"
sudo -u "$OWNER" ssh-keygen -y -P "" -f "$KEY" >/dev/null 2>&1 \
  || fail "$KEY needs a passphrase; a daemon can't use the SSH agent"
sudo -u "$OWNER" ssh-keygen -F "$HOST" -f "$KNOWN_HOSTS" >/dev/null \
  || fail "$HOST is not in $KNOWN_HOSTS; run 'ssh $TARGET' once from Terminal"
[[ "$LOCAL_PORT" =~ ^[0-9]+$ ]] || fail "local port must be a number"

sudo -u "$OWNER" mkdir -p "$(dirname "$LOG")"
sudo -u "$OWNER" touch "$LOG"

cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>UserName</key><string>$OWNER</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/ssh</string>
    <string>-F</string><string>/dev/null</string>
    <string>-i</string><string>$KEY</string>
    <string>-o</string><string>BatchMode=yes</string>
    <string>-o</string><string>IdentitiesOnly=yes</string>
    <string>-o</string><string>ExitOnForwardFailure=yes</string>
    <string>-o</string><string>ServerAliveInterval=10</string>
    <string>-o</string><string>ServerAliveCountMax=3</string>
    <string>-o</string><string>ConnectTimeout=5</string>
    <string>-o</string><string>StrictHostKeyChecking=yes</string>
    <string>-o</string><string>UserKnownHostsFile=$KNOWN_HOSTS</string>
    <string>-N</string>
    <string>-L</string><string>127.0.0.1:$LOCAL_PORT:127.0.0.1:$REMOTE_PORT</string>
    <string>$TARGET</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardErrorPath</key><string>$LOG</string>
  <key>StandardOutPath</key><string>$LOG</string>
</dict>
</plist>
EOF
chmod 644 "$PLIST"
plutil -lint "$PLIST" >/dev/null
launchctl bootstrap system "$PLIST"
echo "Installed $LABEL: 127.0.0.1:$LOCAL_PORT -> $TARGET:$REMOTE_PORT (log: $LOG)"
echo "Set [tts.local] url = \"http://127.0.0.1:$LOCAL_PORT\" in config.toml."
