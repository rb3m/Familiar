# 0017: macOS mouse-button push-to-talk

Date: 2026-11-20. Status: accepted.

## Context
The GUI launcher refuses voice sessions with the terminal push-to-talk, and the only other
backend, evdev, is Linux-only. The Mac has no controllers. The Commander wants a spare mouse or
trackball button as push-to-talk, assigned from the GUI or CLI.

## Options
1. pynput with `darwin_intercept` to suppress the chosen button.
2. Quartz `CGEventTap` through PyObjC, on its own thread and CFRunLoop.

## Decision
Option 2: a `mouse` PushToTalk backend in core (`familiar.ptt.mouse`), macOS-only for now.
- An active session tap on other-mouse down/up/dragged events. Only the configured button (2 or
  higher; left and right clicks are never accepted) is consumed, so it doesn't also act as
  back/forward. The callback only queues press/release into asyncio.
- When macOS disables the tap (timeout or user input), the backend reports a release and
  re-enables it. It reports a release on shutdown too.
- Consuming events needs **Accessibility**. Input Monitoring only allows observing. The voice
  process checks `AXIsProcessTrusted` itself and prints which permission to grant. macOS
  attributes the permission to the responsible app (Terminal, or Familiar.app), not the browser.
- The "press it now" detector temporarily consumes other-mouse buttons, so a back button pressed
  while assigning doesn't navigate the GUI window. The CLI `ptt-setup` and the GUI share it, and
  it runs only while no session is running.

## Agreement
Codex and agy both recommended option 2. agy reported that pynput's interception leaks
suppressed events across macOS versions. Both flagged the timeout re-enable and the
Accessibility requirement.

## Addendum 2026-11-20: keyboard keys

A `key` backend shares the event-tap lifecycle with mouse PTT and accepts only F1-F20 and
keypad virtual keycodes. Assignment consumes those keys while passing ordinary typing through.
Autorepeat is consumed without another press; tap disablement releases PTT and re-enables the
tap. Remap a spare key or keyboard knob to F13; Secure Input pauses keyboard taps. GUI and CLI
offer an explicit detection kind, with mouse remaining the macOS default.
