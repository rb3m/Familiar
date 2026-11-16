# 0001 Modular plugin architecture

- Status: accepted
- Date: 2026-11-15

## Context

Familiar must run with different LLM, speech and game backends, on Linux and macOS, for the
Commander and for other people with other accounts. The starting brief was a private design
note, "AI Voice Copilot and Game Telemetry Bridge Architecture".

## Options

1. Monolith built around one vendor's realtime API (Gemini Live): lowest latency, but it locks the
   voice to Gemini and makes other providers a rewrite.
2. Turnkey tools (VoiceAttack + EDDI): Elite only, Windows-centric.
3. Modular pipeline: `typing.Protocol` interfaces (LLM, STT, TTS, GameAdapter), backends discovered
   via entry points and chosen in TOML, personas as YAML data, a Director arbitrating speech,
   curated knowledge packs per game.

## Decision

Option 3. First backends are Gemini on the free API tier (Flash LLM, Transcribe STT, Flash TTS)
via `google-genai`, then ElevenLabs TTS. A speech-to-speech backend arrives later behind a separate
`Conversation` interface. First game is ATS through the existing TruckSim GPS Linux server.
Transcripts are logged as JSONL from day one; memory/RAG comes in M3.

## Who agreed

- Claude (coordinator): proposed.
- agy: agreed on all eight points; flagged pipeline latency/free-tier limits and global PTT on
  Wayland/macOS as top risks.
- codex (gpt-6.1-sol, medium): agreed with additions: plugin API versions, capability discovery,
  cancellation contracts, bounded queues and stale-interjection dropping, "grounded or abstain"
  for POIs. Questioned TruckSim GPS portability. That's handled by our Linux port for now, and a
  standard SCS telemetry source is on the roadmap for other users.
- Commander: chose the name, Gemini first, English first with switchable languages, ATS first.
