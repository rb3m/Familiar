# 0026: Scene topics, quiet background facts and repetition guards

Date: 2026-11-22
Status: Accepted

## Context

Hours-long desk sessions sounded the same every few minutes.

- Linux, 2026-11-22 (Frieren, Megumin, Ruslana; no location notes): Megumin voiced all 14 song
  remarks plus the weather and midday remarks, and nearly every line read "Even [song] must bow
  before the … majesty of my *Explosion*!". Ruslana opened most lines with "*Da*, let …". A whole
  line was repeated verbatim 8 minutes later. Weather, time of day and the Big Q barely appeared.
- M4, 2026-11-21 (Ruslana, Frieren; with location notes): "La Gran Q" in ~25 of Ruslana's ~27
  lines despite "use it now and then"; Frieren recited "X is playing now. It is four in the
  morning here in La Gran Q." six times; Ruslana opened nearly every line with "*Da*, *bozhe moi*".

Causes: (1) `Crew.pick_for_event` breaks an interest × chattiness tie by roster order, so the
first 0.7-chattiness member takes every unassigned ambient event; (2) the banter prompt has no
topic, so the model grabs the most specific token in the situation block (the song title, the
nickname, the clock time) every time; (3) the recent transcript shows the model its own tics and
nothing tells it, or checks, not to repeat them.

## Options

1. Prompt-only: "vary topics", "now and then", higher temperature.
2. **Code picks one topic per scene from a pool with cooldowns; optional high-salience facts are
   hidden unless they are the topic; opening-avoidance in the prompt plus near-duplicate
   rejection in code.**
3. Option 2 plus a planner LLM call that chooses the topic.

## Decision

Option 2.

- **Speaker rotation.** An ambient event without an assigned speaker goes to a weighted-random
  member (interest × chattiness), never the previous ambient speaker when someone else has
  weight. An event may name a role to lean toward (`data["lean"]`, e.g. song remarks lean to
  the DJ, weight × 2) without owning it.
- **Topics.** Services and adapters may offer `scene_context()` returning a low-salience frame
  ("late night; drizzle") and topic candidates (family, key, text). Desk offers weather, time of
  day/daylight and location notes; music offers the current song; each persona may list free
  `topics` in its YAML; a "free conversation" topic is always eligible. A weighted pick with
  per-family cooldowns (tuning constants in code, starting values: song 20 min and none within
  10 min of a song remark, weather and time 30 min, location 75 min, a persona topic 30 min per
  item) picks one topic per banter scene. History covers banter and event remarks.
- **Hidden unless chosen.** Banter and unprompted remarks see only the frame plus the chosen
  topic's text (an event remark's topic is its event). When the song or location is not the
  topic, the prompt says not to mention them. Replies to the player keep the full, exact
  `describe()` context. Sources without `scene_context()` (game adapters) keep `describe()`.
- **Repetition.** The banter prompt shows a shorter transcript. Each speaker's recent openings
  (first three words of their last lines; any opening used twice is a tic) are listed as "do not
  open with". A generated scene with a line that repeats or nearly repeats (content-word Jaccard
  ≥ 0.6) a line from the last 45 minutes is rejected, retried once with a different topic, then
  skipped. Remarks are streamed, so they get the opening list but no rejection.
- **Dropped sentences.** A sentence whose audio is missing or much shorter than its text
  predicts is logged as a warning, to find the drop next time.

## Consequences

- Silence beats repetition: a skipped scene costs one or two LLM calls and nothing is said.
- Paraphrased repetition still passes the similarity check; topic cooldowns limit it.
- Cooldowns are guesses to tune from logs.
- Game banter keeps today's behaviour until adapters offer `scene_context()`.

## Who agreed

- Commander: speaker rotation, topic pool, repetition guards, sentence diagnostics; Big Q notes
  copied from the M4 config.
- agy, Codex (gpt-6.1-sol), Grok 4.7: code-side weighted topic choice with cooldowns, both
  repetition defenses, hide the song and nickname unless they are the topic. Grok: low-salience
  frame, explicit "don't mention", shorter transcript, ration tics instead of banning them.
  Codex: free-conversation topic, history across banter and remarks, retry once then skip.
  agy: skip without retry; overruled by the other two (one retry, on a new topic).
