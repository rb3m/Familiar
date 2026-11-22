# 0028: A request that names a job acts on its first round (amends 0021, 0027)

Date: 2026-11-22
Status: Accepted

## Context

"Ruslana, play some funky music" got "Ah, *da*, *da*, funky music for *The Big Q*…" and nothing
played: the DJ, addressed directly, answered in prose. ADR 0021 forces a tool round only on a
hand-off target, and ADR 0027 only helps someone who passes the job on. Live with Gemini
flash-lite: 4/4 plays with only music tools, 2/3 with the session's research and system tools
too (the miss said "Playing some funk for you now.").

## Options

A. Force every player turn of a member with tools, plus a no-op `just_talk` tool.
B. Detect an unbacked claim ("playing…") afterwards and force a round.
C. Offer fewer tools on player turns.
D. Prompt wording only.
E. **A data phrase list spots requests that name a role's job; only those turns get a forced
   first round, over that job's tools only.**

## Decision

E, which is C applied to the turns that matter.

- `familiar/data/action_phrases.yaml`: role → language → whole-word phrases ("play", "skip",
  "put on", "pon", "toca", "volumen"…), user override as usual. `familiar/intents.py` matches
  them.
- On a player turn (not a hand-off continuation) whose text names a job, the first round offers
  that role's tools (own or ADR 0027 relays) plus `hand_off`, with `require_tool`; later rounds
  get every tool and speak freely. Chat is untouched: no extra request, no added latency.
- No `just_talk`: with a no-op allowed, a chatty model takes it and the miss stays.

## Consequences

- Live: 5/5 direct DJ requests played with the full tool list; carrier requests 2/2 handed on
  (one Gemini 500).
- A phrase the list lacks ("something with a groove") is still free and may be missed: widen
  the list from live misses. A false match ("I love how you play") forces a music tool; wrong
  picks are audible and reversible (ADR 0021). `now_playing` has no role, so it is not in the
  forced list.
- B was rejected: sentences stream, so the false claim is spoken before it can be caught.

## Who agreed

- Codex and Grok: selective forced round behind a local phrase gate, no `just_talk`, C as the
  default. agy: C plus wording plus B as fallback; outvoted on B (Grok: streaming).
- All three: no blanket forcing (15 requests/min shared with banter; latency).
