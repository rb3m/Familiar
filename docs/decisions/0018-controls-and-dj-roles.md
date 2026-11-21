# 0018: Controls and DJ are separate crew roles (amends 0011, 0013, 0014)

Date: 2026-11-20
Status: Accepted

## Context

One crew member, the carrier, owned every mutating tool: game actions (landing gear, cargo
scoop) and music playback. The Commander's user persona Ruslana, a radio DJ in the style of an
Eastern European station host, is great at running the music but does not belong at the
Diamondback's controls reporting hull damage. He wants the DJ and the controls assignable to
different crew members, or the same one, with a crew of one holding both.

## Options

1. Keep one carrier for everything (status quo).
2. A second hardcoded flag beside `carrier_only` (`dj_only`).
3. Roles: tools name the crew role that owns them (`controls` or `dj`), the session assigns a
   member to each role, and hand-offs target whoever holds the role the speaker lacks.

## Decision

Option 3.

- **Roles.** `controls` is the existing carrier (game actions, confirmations, critical alerts);
  `dj` runs music (play, skip, like, volume, pause, and autonomous DJ turns). The session file
  gains an optional `dj` (schema 3; older files load, saves write 3), defaulting to the carrier.
  `--persona` gives one member both roles. The GUI shows "Plays the music" beside "Takes the
  controls" when the crew has two or more members.
- **Tools.** `ServiceTool` and `ToolRegistry.register` take `role` (`"controls"`, `"dj"`, or
  `None` for everyone). `carrier_only=True` stays as the legacy spelling of `role="controls"`;
  combining it with another role is rejected. Music tools are `role="dj"` except
  `now_playing`. A speaker is offered the tools of the roles she holds; dispatch still checks
  the offered list. Plugin API 2.3 (additive).
- **Events.** `data["speaker"]` names a role: `"dj"`, `"controls"`, or legacy `"carrier"`.
  Music DJ turns use `"dj"`; the DJ's persona `tastes.music` drives taste picks. Critical
  events always go to the controls holder.
- **Hand-off.** `hand_to_carrier` becomes `hand_off(to, line?)`. `to` is an enum of members
  holding a role, in use by registered tools, that the speaker lacks, minus anyone who already
  spoke in this reply; it is required only when there is more than one target. So a hand-off
  can chain (Megumin → Frieren for the gear → Ruslana for the music) but can never bounce back,
  and the chain is bounded by the crew size. The speaker's own tool calls in the hand-off round
  now run (previously they were skipped), so "gear down and play something" lowers the gear
  and then hands the music on; the next speaker sees the outcome and lacks those tools anyway.
- **Guest picks.** The Commander wants every crew member to choose a song now and then. An
  event with `data["speaker"] = "guest"` goes to the next member other than the DJ, in
  rotation (the DJ when nobody else is aboard), and that one turn may use the event's offered
  autonomous tools whatever role owns them. The music service makes every `guest_pick_every`
  (3) heard DJ turn a guest pick with the guest's own `tastes.music`; 0 turns it off. A guest
  turn the Director drops does not count, so the guest's go simply comes at the next DJ turn.
- **Prompt.** A speaker lacking a role is told who handles each job ("Only Ruslana handles
  playing or changing music") and to hand off after doing her own part.

## Consequences

- Existing setups are unchanged: no `dj` key means the carrier is the DJ.
- A compound request started by a third member costs up to three LLM calls in one reply.
- Review fixes (Codex): `hand_off` arguments are validated; a bad or ambiguous `to` returns an
  error to the speaker instead of guessing a target. A hand-off waits while the speaker's own
  action in that round needs confirmation, so the yes/no question is asked first.
- Not adopted: Codex suggested the Director split compound requests into per-role
  sub-requests. The chained hand-off covers the live cases without another orchestration
  layer; revisit if live use shows dropped halves of compound requests.
- The prompt still names both jobs in desk mode, where nobody holds game controls; harmless,
  since no hand-off is offered without tools for that role.

## Who agreed

- Commander: DJ and controls separable, the same persona allowed, a crew of one holds both;
  every crew member gets a go at picking a song from time to time.
- Codex (gpt-6.1-sol, workhorse): role on tools with `carrier_only` normalised to controls and
  conflicts rejected; enforce at dispatch as well as in the offer; `hand_off` targeting crew ids
  with no tool when there are no targets; `"carrier"` as an alias; DJ drives tastes and DJ
  turns; flagged compound requests.
- agy: agreed on all five points and the rename; proposed running one's own part then handing
  off the remainder; flagged the third-member case.
- Claude (coordinator): merged; chained hand-offs (no return to anyone who spoke) instead of a
  Director orchestration layer.
