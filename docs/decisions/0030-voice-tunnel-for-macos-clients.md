# 0030: SSH tunnel daemon for macOS clients of the voice server

Status: Accepted (2026-11-23)

## Context
The first desk session never reached the M1 voice server (ADR 0029). macOS Local Network privacy
denied every LAN connection from Familiar.app with `EHOSTUNREACH` ("No route to host"). The same
Python from Terminal worked, because Terminal and SSH children are exempt. Familiar.app is an
`osacompile` applet. Its main executable is a stock binary shared by every applet, and it is
ad hoc signed. Apple's TN3179 says both stop Local Network privacy from tracking an app. The app
never appeared in System Settings › Privacy & Security › Local Network, even after adding a
bundle id and `NSLocalNetworkUsageDescription`.

## Options
1. **`AllowedWiFiLocalNetworkAddresses` / `AllowedEthernetLocalNetworkAddresses`** (sudo, reboot):
   treat the M1's address as non-local for every program. Rejected by the user: any app on the
   Mac could then reach that host without asking.
2. **Launch the GUI through Terminal** (exempt): works, but leaves a Terminal window open.
3. **A real app bundle (Swift wrapper)**: ad hoc signing is still unreliable for this permission
   without an Apple-issued identity.
4. **A launchd daemon holding an SSH tunnel** (launchd daemons are exempt): Familiar talks to
   `127.0.0.1`, which is not local network. Only the voice server's port is forwarded, and the
   traffic and token are encrypted on the LAN.

## Decision
Option 4. `packaging/macos/install-voice-tunnel.sh` (sudo, once) writes
`/Library/LaunchDaemons/com.familiar.voice-tunnel.plist`. It runs as the user (`UserName`), with
`KeepAlive` and a 10 s throttle. It runs
`ssh -N -L 127.0.0.1:8766:127.0.0.1:8765` with a passphrase-free key, `BatchMode`,
`StrictHostKeyChecking=yes` and keepalives. Linux clients connect directly.

Measured: first audio 0.77–0.79 s through the tunnel, the same as direct (0.75–0.84 s).

Agreed: the user (chose against option 1) and Claude. Not sent to agy/codex; the constraint came
from Apple's documented exemptions rather than a design trade-off.
