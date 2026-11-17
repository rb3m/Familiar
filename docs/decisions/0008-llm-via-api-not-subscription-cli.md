# 0008 LLM via the Gemini API, not the subscription CLI

- Status: accepted
- Date: 2026-11-17

## Context

The Commander has a Gemini Pro subscription, used through the Antigravity CLI (`agy`). SwiftBlade
already drives it per turn with `agy -p "<prompt>" --continue`. Familiar instead pays nothing on
the free API tier and keeps a capped paid key as fallback (ADR 0001, `fallback_api_key_env`). The
question was whether Familiar could reuse the subscription. A voice companion needs the first
words within about a second of push-to-talk, and alerts lose their value when late (ADR 0007).

## Options

1. Subscription through the CLI: one `agy -p … [--continue]` subprocess per turn.
2. Gemini API via `google-genai`: a persistent in-process client streaming into TTS.

The consumer subscription grants no API key, so option 1 is the only way to use it.

## Measurements (2026-11-17, Mac, same prompt)

Prompt: "In one short sentence, as a friendly co-pilot: what is a good tip for driving at night?"
Three runs each, wall-clock seconds. The CLI prints only once the reply is complete, so its
total is also its time to first word.

| Path | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| `agy -p` (default model) | 6.62 | 5.59 | 5.81 |
| `agy -p --continue` | 5.77 | 5.11 | 4.93 |
| `agy -p --effort low` | 5.70 | 4.61 | 4.52 |
| `agy -p --effort low --continue` | 4.43 | 5.13 | 4.46 |
| `agy -p --model gemini-3.8-flash-low` | 4.77 | 4.10 | 5.03 |
| `agy --version` (process start only) | 0.05 | 0.05 | 0.05 |
| API `gemini-3.5-flash-lite`, first chunk | 0.87 | 0.65 | 0.47 |
| API `gemini-3.5-flash-lite`, total | 0.87 | 0.74 | 0.66 |
| API `gemini-3.8-flash`, first chunk | 6.14 | 5.76 | 6.02 |

Local models (Ollama 0.40.16, Apple M4, 16 GB, streamed, `think: false`). "Cold" includes
loading the model into memory; warm runs reuse it.

| Model | Cold first token | Warm first token | Warm total |
|---|---|---|---|
| `phi3:3.8b` | 5.84 | 0.03 / 0.03 / 0.03 | 0.65 / 0.42 / 0.46 |
| `gemma4:e4b` | 13.52 | 0.10 / 0.08 / 0.06 | 0.81 / 0.88 / 0.68 |
| `qwen3.5:9b` | 17.91 | 0.09 / 0.08 / 0.08 | 1.54 / 1.70 / 1.14 |
| `qwen3:4b` | 0.66 | 0.04 (a `<think>` tag) | 19.26 / 13.45 / 30.26 |
| `qwen3:4b`, thinking on | 17.04 | 17.43 / 21.91 / 7.38 | 18.47 / 22.64 / 8.21 |

Findings:
- The model, not the path, sets the latency. `gemini-3.8-flash` over the bare API, without the
  CLI's context, is as slow as the CLI (about 6 s), while flash-lite over the API answers in
  under 1 s. Thinking is the likely cost: `qwen3:4b` ignores `think: false` and spends 13–30 s
  reasoning before a one-line reply.
- The CLI adds little measurable overhead on top. Process start is 0.05 s. It does load the agent
  prompt and global instructions (replies addressed the Commander as "Cap'n", so `GEMINI.md` is
  read), but its 4–5 s runs are no slower than the same model family over the API.
- The CLI still loses for voice because of what it offers: no flash-lite, `--effort low` saves
  only about 1 s, and it prints once the reply is complete instead of streaming into TTS.
- Warm local non-thinking models are the fastest of all: first token in under 0.1 s, a full line
  in 0.4–1.7 s, with no quota or network. Cold loads take 6–18 s, so the model must stay
  resident (`keep_alive`), and it takes 2–10 GB of RAM while a game is running. Quality and tool
  calling were not tested.
- `--continue` was not slower here, but the test history was only a few turns.

## Decision

Option 2 stays. Familiar calls the Gemini API with `gemini-3.5-flash-lite` as the default voice
model and streams it into TTS; slower models are fallbacks only. The subscription CLI stays
SwiftBlade's text workbench, where a 5 s turn is acceptable (see also the SwiftBlade note in
`docs/roadmap.md`, M5). Any voice model, cloud or local, must be a fast non-thinking one. The
local runs are a baseline only: the Linux gaming machine has 8 GB of VRAM, mostly taken by the
games, which leaves no room for a resident model. If the subscription CLI ever offers flash-lite,
it may be worth re-timing, since the CLI's own overhead looked small.

## Who agreed

- Claude (coordinator): measured and proposed; records the reasoning ADR 0001 left implicit and
  backs the flash-lite default (commit `2277018`).
- Commander: asked for the timing and this record, spotted that the model rather than the CLI
  context explains the gap, and proposed the local Ollama experiment.
