# 0009: Hands: game actions as tools, core-enforced confirmation, uinput keyboard

Date: 2026-11-17
Status: Accepted

## Context

M5 gives the companion hands: acting in a game when the player asks. KSP has an API (kRPC, already
used read-only). ATS/ETS2 have none, so actions are key presses injected through Linux uinput,
using the player's own bindings from the profile's `controls_linux.sii`. The Commander chose:
harmless actions run at once, risky ones (stage, abort, throttle, warp, engine off while moving,
action groups) need a spoken "yes" first, and the companion never acts unprompted. Plugins may
import only `familiar.interfaces` (ADR 0004). LLM tool calling exists since ADR 0007.

## Options

1. Prompt-only confirmation (tell the model to ask first) vs. a gate enforced in core.
2. One tool per action vs. a single `do_action(action, args)` tool.
3. Key injection owned by the SCS pack, returned to core as key steps, or behind a core
   `Keyboard` protocol handed to the pack.
4. Share the telemetry kRPC connection for actions vs. a dedicated action connection.

## Decision

- **Interfaces (additive, `PLUGIN_API_VERSION` stays 2.0):** optional `Actuator` protocol
  (`actions() -> dict[str, ActionSpec]`, `async perform(name, args) -> dict`, optional
  `action_context()` identity such as the active vessel), `ActionSpec` (description, JSON schema,
  `risky`, optional per-argument risk predicate), `Keyboard` (`async tap(keys, hold_s)` for a key
  or chord, `aclose()`), and an optional `attach_keyboard(keyboard)` hook. Results carry a
  `status` of `done`, `already`, `unavailable`, `unknown` or `error` plus a detail.
- **One tool per action** (`act_<name>`), small schemas, only the active game's tools, offered only
  on player turns (interjections can never act).
- **Core `ActionKeeper` enforces confirmation.** A risky call returns `needs_confirmation` and stores
  one pending action with frozen arguments and context. `confirm_action` runs it only if: a later
  player turn than the request (no self-confirming within the tool rounds of one turn), within
  `confirm_ttl_s` (30 s), the player's latest utterance contains an affirmative and no negative
  (word lists per language, extendable in config), and the context is unchanged. It is consumed
  before running. A negative utterance cancels it, a new risky request replaces it, and a
  clarifying question in between does not cancel it. Identical calls run at most once per turn.
  Each action is time-bounded; a timeout reports `unknown` and is never retried.
- **Keyboard in core**, plugin group `familiar.keyboard`, first backend `uinput` (python-evdev).
  Core owns the device, serialises presses and releases every key in `finally` and on shutdown.
  The SCS pack owns bindings, toggle semantics and hold times, checks telemetry state before a
  toggle where telemetry has it, and says "toggled" when it cannot see the state.
- **KSP uses its own action connection** on a single worker thread with timeouts, separate from
  the telemetry poller, and re-checks the vessel before acting.
- **Focus guard:** SCS acts only while telemetry is fresh and the game is not paused. A real
  window-focus check (KWin scripting on KDE Wayland) is follow-up work.

## Consequences

Packs gain actions without touching core; a macOS keyboard backend can come later without changing
the SCS pack. Bindings that exist only on a joystick cannot be pressed and are reported as such.
Confirmation depends on speech-to-text hearing "yes" in the right language. Live behaviour (key
timing in SCS, flash-lite calling the right tool) needs checking in game.

## Who agreed

- codex: one tool per action; keyboard protocol in interfaces with core owning device lifecycle;
  keep API 2.0 (additive); core must check an explicit affirmative from the player, not trust the
  model; serialised kRPC work with timeouts, never retry stage/abort; per-turn deduplication.
  Wanted unrelated speech to cancel a pending action.
- agy: one tool per action; boundary D; flagged the self-confirmation leak across tool rounds,
  kRPC client not thread-safe (dedicated connection or lock), stuck keys, blind toggles, focus.
  Wanted pending actions to survive a clarifying question with a TTL and explicit cancel (adopted).
- Claude (coordinator): drafted the proposal, merged the views; deferred the focus check after a
  quick KWin scripting test did not return the active window without a D-Bus callback service.
- Commander: chose confirm-risky-only, never unprompted, bindings read from the game profile.
