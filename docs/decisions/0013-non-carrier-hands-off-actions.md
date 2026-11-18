# 0013: A non-carrier hands action requests to the carrier (amends 0011)

Date: 2026-11-18
Status: Accepted (amends the "Hands" bullet of ADR 0011)

## Context

ADR 0011 had a non-carrier, asked to act, refuse in character and name the carrier. In the
live Elite session of 2026-11-18 (crew Frieren as carrier, Megumin), the 30-second follow-up
window routed the Commander's commands to Megumin right after her event remarks (undocked,
scoopable star), although he never addressed her. She answered "Have Frieren open the cargo
scoop" several times, and twice called the only tool she had (`list_alerts`) and then echoed the
history label as "[Tool outcome] Tool completed". It looked like the ship was broken until the
window lapsed. In a cockpit, a refused command reads as a failure, not as character.

## Options

1. Keep refusing in character (status quo).
2. Exclude event remarks from the follow-up window (helps, but a real follow-up conversation
   with a non-carrier still swallows the next command).
3. Delegation: non-carriers get a `hand_to_carrier` tool; calling it ends their turn and the same
   player request runs as a carrier turn with the carrier's tools.

## Decision

Option 3. Non-carriers in a crew with carrier-only tools are offered `hand_to_carrier`
(optional short in-character line). Core then re-runs the player's request as the carrier's turn
in the same reply, so the action happens and the carrier confirms it. One hand-off per player
turn (no ping-pong); the carrier never gets the hand-off tool. Separately, history labels such as
`[Tool outcome]` are stripped from spoken output, and the spoken-units rule no longer lists
imperial examples: units are spelled out as given, never converted (light-years stay
light-years).

## Consequences

A command routed to a non-carrier costs one extra LLM call, only in that case. Character survives
(Megumin can quip before Frieren acts). Codex had proposed delegation in the 0011 round; the
majority chose refusal, and live use reversed it.

## Who agreed

- Commander: reported the failure in the live session.
- Coordinator (Claude): diagnosis from the transcript; chose delegation over a follow-up-window
  tweak because both failure paths (event remark, real follow-up) end in the same refusal.
- Codex (in the 0011 round): carrier-only tools with delegation.
