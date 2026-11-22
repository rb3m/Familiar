# 0027: Music tools relay to the DJ (amends 0018, 0021)

Date: 2026-11-22
Status: Accepted

## Context

After a restart the Commander asked "Play some funky music"; Frieren (controls) answered
"Ruslana can handle the music." and called no tool, so nothing played. ADR 0021 forced the hand-off
*target* to act, but the carrier's own hand-off still depended on the model choosing `hand_off`
over prose. In an isolated live repro (no desk, research or system tools) Frieren handed off 8/8,
so the miss is intermittent and likely more frequent with the live session's longer tool list and
context. The model reliably calls `play_music` whenever it is offered.

## Options

1. Stronger prompt wording for `hand_off`.
2. Detect prose that names the DJ without a tool call and force a hand-off round afterwards.
3. **The Commander's idea: passing the job on is itself an action. Members without the DJ role
   are offered the DJ's tools; calling one hands the request to the DJ instead of running it.**

## Decision

Option 3, for the DJ role only (`RELAY_ROLES` in `crew.py`).

- On a player turn with a hand-off target, the speaker also gets the target's DJ tools, same
  name and parameters, described as "Passes this request to <DJ>, who does it: …".
- A call to one of them is held, never dispatched, and answered `handed_off`; the DJ then takes
  the turn with the ADR 0021 required tool round. If the speaker also called `hand_off`, that
  call wins and the relay call is refused, so the carrier never runs a DJ tool.
- The duty prompt adds "Calling a music tool hands the request to the DJ, who plays it."
- Game controls are not relayed: a non-carrier calling `act_*` is still refused (ADR 0018).

## Consequences

- Live after the change: 5/5 then 2/2 (one 503) hand-offs; every `play_music` that ran was the DJ's.
- The carrier's tool list grows by the DJ's tools on player turns.
- Extending relays to controls is a one-line change if a non-carrier ever fails to pass on a
  game action, but it would change the refusal behaviour of ADR 0018.

## Who agreed

- Commander: proposed treating the hand-off as the carrier's action for music.
- Claude: implemented and live-tested; no consensus round (narrow, Commander-specified design).
