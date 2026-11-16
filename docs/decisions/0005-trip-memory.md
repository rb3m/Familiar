# 0005 Trip memory: SQLite episodes, context injection, gated recall

- Status: accepted
- Date: 2026-11-16

## Context

M3 asks the companion to remember earlier trips. Two uses: prompted recall ("have we been here
before?", "remember the trip to Reno?") and occasional spontaneous recall ("I remember when we
passed through here carrying cattle"), which must stay rare: recalling every known place gets
tiresome. Replies are spoken, so ordinary turns must not gain latency. The core must stay free of
game code (ADR 0004). The Gemini backend supports tool calls, but the conversation loop does not
execute them, and a tool round trip would add a second LLM call to a spoken reply.

## Options

- Storage: A) SQLite in the user data directory; B) one file per game; C) only the JSONL
  transcripts plus embeddings.
- Retrieval: A) deterministic lookup by place, region, job cities and cargo; B) embeddings
  (Gemini or Ollama) over episodes and summaries.
- Delivery: A) inject a short labelled memory block into the system prompt; B) a recall tool the
  LLM calls.
- Spontaneous recall: A) gated by salience, cooldowns, recall history and a low probability;
  B) recall on every revisit.

## Decision

Storage A, one database with a `game` column. Retrieval A now; embeddings are deferred until real
paraphrased queries show gaps. Delivery A; no recall tool for now. Spontaneous recall A.

- Core `familiar.memory` owns the store, a keeper that observes game events, the context block
  and the recall gate. Tables: sessions (game, persona, start/end, summary), episodes (session,
  game, UTC time, kind, place key/name, region, activity, compact text, salience) and recalls
  (episode, persona, time). JSONL transcripts stay the raw record.
- Game packs supply the game knowledge through two optional, duck-typed hooks: an adapter
  `whereabouts()` returning the current place key/name, region and activity (e.g. hauling
  cattle to Bayonne), and a `memory_salience` value in event data (0 = not stored). Only
  meaningful events become episodes; plain visits are stored at low salience so "have we been
  here?" works.
- Each player turn gets at most a few short episodes for the current place, plus matches for
  place names or cargo named in the player's words, or an explicit "no earlier record here".
  Memories are labelled as past, with real dates, so they don't blend into the current job.
- Spontaneous recall is an ambient game event, eligible only on arrival-type events, for a
  distinctive episode from an earlier session at the same place, with a global cooldown, a long
  per-episode cooldown and a low probability. Recalls are recorded when the companion actually voices them.
- A short session summary is written in the background at clean shutdown (time-limited).
  Trip facts are shared across personas; recall history is per persona.

## Who agreed

- agy: SQLite, location/entity keys without embeddings first, context injection over a tool
  (latency), cooldown + salience + ~20% probability gate, async summaries, facts shared and
  persona notes separate. Suggested one DB per game.
- codex: same core design; one DB scoped by game; record what was actually mentioned; say "no
  record" instead of guessing; group repeated location updates into visits.
- Claude: merged the two; chose one database (codex) for simpler queries. Main risk both named:
  repetitive or blended recall, so injected memories stay short, dated and clearly past.
