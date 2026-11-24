# 0031: Multiple push-to-talk bindings and evdev hot-plug

Date: 2026-11-24. Status: accepted.

## Context
Push-to-talk was one backend, so a keyboard key at the desk and a controller button in a game
meant re-assigning between them. On Linux a USB drop (a Keychron V3 Ultra 8K on a cheap hub
resets often, sometimes taking the hub with it: `disabled by hub (EMI?)`) made evdev raise, which
ended the session; it never resumed when the device came back.

## Options
1. A user-facing `multi` backend whose settings hold a list of child backends.
2. `[[ptt.bindings]]` in core: a list of `{backend, ...settings}`, always wrapped by a combiner.
Reconnect detection: poll `evdev.list_devices()`, a pyudev monitor, or inotify on `/dev/input`.
A device missing at startup: fail, or start and wait.

## Decision
- Option 2. The legacy `[ptt] backend` + `[ptt.<name>]` form still loads as a one-item list;
  mixing both forms is an error. Duplicates (effective settings) are rejected; terminal must be
  the only binding. Saves (GUI, `ptt-setup`) rewrite the section as `[[ptt.bindings]]`.
- `familiar.ptt.combined.CombinedPTT` ORs children with a set of held labels: True on
  empty->held, False on held->empty. A child that ends or raises `PluginError` counts as
  released and the rest keep working; events end only when every child has.
- evdev polls every `RECONNECT_POLL_S` (2 s). Missing at startup or after a drop: print one
  status line, wait, and resume. A drop while held releases first. On (re)open a button already
  down is ignored until released, so a reconnect can't start a phantom recording. Unknown button
  names stay fatal. Status lines go to stderr, so the GUI log shows them.
- macOS mouse and key bindings keep their own event taps (different masks, one thread each).

## Agreement
agy, Codex and Grok all chose option 2, polling (a create event still needs an open plus a
name/capability match), and start-and-wait. Grok and Codex added the already-held check on
reopen; Grok flagged that `replace_section` had to learn `[[...]]` tables. agy and Codex would
prefer one shared macOS tap; Grok judged separate taps fine. We kept separate taps (existing
behaviour, low risk); revisit if a mixed mouse+key setup misbehaves on the Mac.
