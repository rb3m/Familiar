---
title: "Japanese practice mode (superseded plan)"
created: 2026-11-17
status: superseded 2026-11-17
---

Superseded: the Commander chose to do Japanese practice in SwiftBlade, with a way to read its
output aloud, instead of building a tutor mode into Familiar. Kept for the research notes
(pronunciation testing, browser mic over Tailscale). Original roadmap entry:

Planned (M5): **Japanese practice mode with vault access and browser audio.** Megumin (Ayaka's
voice) as a spoken Japanese tutor, used from any device over Tailscale. SwiftBlade (the
Mac Mini chat front end for CLI agents) was considered and rejected for this: every turn runs a
CLI agent, which is too slow for speech, and its voice stack would duplicate Familiar's in Swift.
SwiftBlade stays the text workbench, and both share the vault. Steps:
1. Vault tools (LLM tool calls, limited to the vault folder): `search_vault`, `read_note`,
   `write_note`. The tutor uses the grammar guides and song analyses and saves vocabulary or
   "mistakes from today" notes. Decide the write scope (new notes in a practice folder only,
   or edits) before building.
2. Practice mode: a lesson prompt (correct gently, then carry on in character), politeness
   levels (see the addressing item above), and learning memory on top of M3 (words practised,
   recurring mistakes).
3. Browser audio in the GUI: push-to-talk, mic capture and playback in the page, over the
   Tailscale HTTPS name (browsers require a secure page for the mic). Architecture decision:
   get agy/codex consensus and an ADR first. Decide where it runs: this PC (vault local) or the
   Mac Mini (always on).
4. Pronunciation, test before building: speech-to-text tidies speech into correct words, so
   grammar and word-choice correction works but pitch accent and vowel length mostly don't
   survive. Try Gemini Live native audio and Azure Pronunciation Assessment (ja-JP) on real
   recordings first.

