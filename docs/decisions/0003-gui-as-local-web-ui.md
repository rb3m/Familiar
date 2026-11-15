# 0003 GUI launcher as a local web UI

- Status: accepted
- Date: 2026-11-15

## Context

The Commander wants a launcher where they pick the game and persona, start/stop the companion,
and create personas. It must run on KDE Wayland and macOS Apple Silicon. The voice session is
an asyncio CLI with its own audio and push-to-talk handling.

## Options

1. PySide6/Qt desktop app: native widgets and tray support, but a large dependency and Qt/asyncio
   event-loop integration.
2. Local web UI: aiohttp server plus vanilla HTML/CSS/JS, shown in an app-mode browser window.
   The voice session runs as a subprocess and its output streams over SSE.
3. Tkinter: no new dependency, but dated look and awkward for a form-heavy editor.

## Decision

Option 2. It matches the user's vanilla web preference and is async-native. It keeps the CLI
session untouched as a subprocess and works the same on both OSes, and could even be opened
from a tablet later. The server binds to 127.0.0.1 only and requires a per-launch token plus an
Origin check, so other web pages cannot drive it. The server owns at most one session and stops
it gracefully, then forcibly, so a closed tab never orphans the microphone. The API contract is
in `docs/gui-api.md`.

Qt becomes the better choice if a tray icon, global shortcuts or other native desktop behaviour
becomes central.

## Who agreed

- Claude: proposed option 2, added the token/Origin requirement.
- agy: option 2. Top risk: closing the tab leaves the session running (zombie processes).
- codex (gpt-6.1-sol, medium): option 2, with HTTP for commands and SSE for output. Top risk:
  session lifecycle (orphaned mic capture, duplicate sessions), so the server owns the session.
