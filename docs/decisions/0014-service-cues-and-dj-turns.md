# 0014: Service cues and carrier DJ turns (amends 0012)

Date: 2026-11-19
Status: Accepted

## Context

In the 2026-11-19 Elite session the Commander wanted two things from the music companion:

1. A remark on the new song every three or four songs. Track remarks were `ambient` events,
   and the Director admits an ambient event only after ~5 minutes of nobody speaking. With
   Elite events and crew banter that never happened (one remark in 45 minutes), and the music
   service started its own 15-minute cooldown on *emitting* a remark even when it was dropped.
2. The crew member who performs actions (the carrier, ADR 0011/0013) picking music now and then,
   "something fitting the moment or something she likes". Unprompted turns had no tools at all,
   so only replies to the player could call `play_music`.

The remark must still never interrupt someone who is speaking.

## Options

- **A.** Generic core feature: an event names the tools it offers; the music service emits a
  periodic "DJ turn" and the LLM decides using live context and the persona.
- **B.** No LLM tools: persona taste lists drive a DJ-side pick, followed by an ordinary remark.
- **C.** A with persona taste hints injected into the DJ turn.

For remarks: count song changes in the service and emit at a higher priority, or exempt them
from the quiet gaps.

## Decision

- **C.** Core gains three generic event keys and one tool flag, without importing any service:
  `data["tools"]` offers named tools on that unprompted turn, honoured only for tools
  registered as `autonomous` (and still subject to carrier-only rules); `data["speaker"] =
  "carrier"` routes the event to the carrier; `data["taste"]` adds the speaker's persona
  `tastes[topic]` as hints. Other interjections stay tool-free. Plugin API 2.2 (additive).
- **Cue events.** `data["cue"]` marks an event tied to a moment: the Director skips the
  priority cooldown and per-kind gap but still waits until nobody is speaking, replying or
  holding push-to-talk; the short TTL drops it if the moment passes. This follows the
  Commander's rule (no 5-minute wait, never interrupt) rather than promoting remarks to
  `notable`, which would still impose a 40-second gap after any line.
- **Music cadence.** The service counts natural song changes (songs someone just picked don't
  count): a remark every `remark_every` [3, 4], a carrier DJ turn every `dj_turn_every` [8, 12]
  that offers only `play_music` and may keep the current music. A DJ turn that speaks also
  counts as the remark. Counts restart when `speech_state` reports the event being spoken
  (the Director admitted it), so a dropped remark retries at the next song change.
- **The Commander stays in charge.** Any selection request (play, skip, more like this,
  calmer, dislike) holds autonomous picks for `autonomy_hold_s` (900 s); `pause_music` stops
  them until music resumes; `autonomous_picks = false` turns them off. Pressing push-to-talk
  cancels a DJ turn in progress, as it does every interjection, so a request always wins.
- **No silent fallback pick.** Grok proposed playing a taste query when the LLM times out.
  Because the carrier may legitimately keep the music, "no tool call" cannot be told apart from
  a failure; an unadmitted DJ turn simply retries at the next song change instead.
- Genre playlists are now chosen at random among the top three unused results (relevance falls
  off quickly below that).

## Consequences

- The "speech started" acknowledgement is the turn being admitted, not audio playing: voice
  reports `speaking` with the event kind when the interjection task starts. A turn that then
  fails in the LLM still counts as heard; acceptable for a cadence.
- Track remarks still pause the new song until the line ends (ADR 0012); DJ turns duck instead,
  since the carrier may switch songs mid-turn.
- Review fixes (Codex): a DJ turn queued before a request or pause cannot select music once it
  speaks; a player paused outside Familiar gets no DJ turn; songs reached by a skip, dislike or
  other request are not counted as natural changes; repeated speech states within one turn
  acknowledge it once.
- Any future service can use cues and offered tools (e.g. a timer or news companion) without
  core changes.

## Who agreed

- agy, Codex (gpt-6.1-sol, workhorse) and Grok (grok-4.7, high) all chose C, a separate
  schedule for remarks and DJ turns, counting song changes in the service with a random 3-4
  threshold, restarting counts only on a spoken acknowledgement, and a hold after user requests.
- Grok: never speak and start playback in one turn; play the pick silently and voice it at the
  next remark slot; fallback taste pick on timeout. Not adopted: remarks already pause the song,
  DJ turns duck, and a silent change would feel odd to the Commander (see "No silent fallback").
- Codex: let the LLM choose "no change"; intersect offered tools with an autonomous allowlist;
  a DJ turn satisfies the pending remark. Adopted.
- agy: an "ownership lease" after user requests (adopted as `autonomy_hold_s`).
- Commander: the carrier role picks (never a hardcoded persona); remarks skip the quiet gap but
  must not interrupt anyone.
