# 0029: Local voice server on the M1, leased by any Familiar

Date: 2026-11-23
Status: Proposed (not built; gated on the stress test below)

## Context

Every spoken line costs ElevenLabs credits: desk sessions speak ~3,100 characters an hour, and
gaming is estimated at ~6,200. The local TTS pilot (`docs/local-tts.md`) found Qwen3-TTS via
mlx-audio as good as ElevenLabs by blind listening.

The Commander wants the voice to run on the 8 GB M1 Mac Mini home server. It is idle while
Familiar runs on the M4 desk Mac or the Linux gaming PC, so neither machine gives up memory or
GPU. The server should start when Familiar starts on any computer and stop when Familiar stops,
so the M1's memory is free otherwise.

The M1 is constrained:
- **Memory**: an always-on VM uses ~4 GB and the machine already swaps, so only one model fits.
- **Speed**: measured one sentence at a time, every candidate runs close to real time.

| Model on the M1 | First audio | RTF | Listening score |
|---|---:|---:|---:|
| 1.7B VoiceDesign | 565 ms | 0.98 | 4.50 |
| 0.6B Base (clones from a sample) | 902 ms | 0.86 | 4.39; Ruslana 5.00 with Cyrillic |
| 0.6B CustomVoice | 364 ms | 0.76 | 3.67 |

Loading a model takes 4–18 s.

## Options

Lifecycle:
1. **SSH**: Familiar starts and stops the server over SSH. Needs keys on every client, can't
   coordinate two clients, and a crashed client leaves ~5.7 GB loaded.
2. **Socket activation**: launchd starts the server on the first connection and it exits when
   idle. The 4–18 s load lands on the first spoken sentence, and it still needs ownership logic.
3. **Lease supervisor**: a tiny always-on launchd daemon (no model loaded, ~40 MB) loads one
   model worker while any client holds a lease, and kills the worker when the last lease ends
   or expires.

Model: 0.6B CustomVoice (most speed margin) or 0.6B Base cloning (better voices, less margin).

## Decision

**Option 3, the lease supervisor, running 0.6B Base cloning.**

- **Supervisor** (runs on the M1, its own uv environment outside the Familiar workspace):
  - Speaks HTTP on the LAN.
  - `POST /lease` returns a lease id. Clients renew it every ~10 s; it expires after ~30 s;
    `DELETE /lease/{id}` releases it.
  - The first lease starts the model worker as a separate process. When no leases remain, after
    a short grace period and once in-flight requests finish, it kills the worker. Ending the
    process is what reliably frees memory for the VM.
  - Two clients hold independent leases. A crashed client's lease simply expires.
- **Synthesis**: `POST /synthesize {text, persona, language}` streams 24 kHz PCM16 chunks, the
  same shape as `AudioChunk`.
  - Requests run one at a time. When the worker is busy, it answers "busy" at once rather than
    queueing, so the client falls back.
  - Voices are data: a persona maps to a reference clip and its transcript (cloning), or to a
    description (a VoiceDesign model, if a bigger machine ever runs one). The model and voice
    mode are chosen per persona, so a future server can mix them.
- **Client**: a `local` TTS plugin, an HTTP client only.
  - Takes a lease at session start and releases it at shutdown, renewing in the background.
  - It is unavailable (a retryable `BackendError`) until the worker reports ready, so the 4–18 s
    load is spoken by ElevenLabs and never stalls a sentence.
  - The chain nests the existing `FallbackTTS`: local → ElevenLabs → Gemini.
- **Protecting the conversation**:
  - **First-audio deadline per sentence**, starting at ~1.0 s (configurable, tuned from p95).
    A miss cancels the local request and raises, and `FallbackTTS` speaks that sentence through
    ElevenLabs. It already falls back only before any audio has played.
  - **Stall detection**: if the playback buffer drops below ~200 ms during a sentence, finish it
    and send the rest of the turn to ElevenLabs. A sentence is never replayed.
  - **Prepare the next sentence while the current one plays**, so an RTF below 1 stays ahead.
  - **Repeated misses park the local voice for a while**, like the 15-minute LLM park.
- **Spelling**: Ruslana writes Slavic words in Cyrillic (her persona prompt). No spelling layer.

## Gate before building

A stress test on the M1, with the VM under its normal load:
1. Ten multi-sentence turns per persona: first audio p95, gaps, swap growth, and whether memory
   is reclaimed after the lease ends.
2. A shorter cloning sample (8–10 s instead of 20 s) to cut 0.6B Base's delay and memory.
3. Two clients at once, a killed client, and a cold start.

If 0.6B Base misses the deadline too often, try 0.6B CustomVoice there. Failing that, keep
ElevenLabs and run the local voice on the M4 for desk sessions only.

## Consequences

- Credits are spent only on fallback sentences and during model warm-up.
- When a sentence falls back, the voice changes mid-turn, so persona voices should stay close to
  their ElevenLabs originals. Cloning the ElevenLabs samples keeps them close.
- Qwen does not act out `[tags]` (ADR 0020); the `local` backend leaves out `audio_tags`, so tags
  are stripped for it.
- New moving parts: a launchd service on the M1, lease renewal in the client, and a LAN
  dependency. A Linux client needs the M1 on the network; if it is missing, the session uses
  ElevenLabs.
- No hardware purchase. A newer 16 GB Mac Mini would let the server run VoiceDesign as well.

## Who agreed

- **Commander**:
  - Local voice to save credits.
  - The M1 as a voice server that starts and stops with Familiar on any computer.
  - Cyrillic for Ruslana; the timing fix confirmed by ear.
  - Listening scores that ranked 0.6B Base above 0.6B CustomVoice.
- **codex** (`gpt-6.1-sol`, standard tier):
  - Lease supervisor with a worker process, heartbeat about every 10 s and expiry about 30 s.
  - ElevenLabs during warm-up; a ~1.2 s first-audio deadline.
  - No replay after playback starts; reject rather than queue.
  - Recommended 0.6B CustomVoice for margin.
- **agy**:
  - Lease supervisor (rejected SSH for orphaned processes and two clients; rejected socket
    activation for the cold load on the first sentence).
  - 0.8–1.0 s deadline; preparing the next sentence ahead; a 200 ms buffer bailout.
  - Recommended 0.6B Base, with a swap stress test and an A/B against CustomVoice.
- **Disagreement**: the model. The Commander's blind scores (Base 4.39 vs CustomVoice 3.67) had
  already answered agy's proposed A/B, so Base is chosen, gated on the stress test, with
  CustomVoice as the fallback plan.
