# Local TTS pilot: results

Pilot run 2026-11-23 to see whether a local voice can replace ElevenLabs. The goals are saving
credits and getting more control over inflection. The harness, lines and clips live outside the
repo (`familiar-tts-pilot/`, a throwaway folder); this page keeps only the results. The full
write-up is the Vault field report `SoftwareDevelopment/20261123-0634 Familiar Local TTS Bake-off
- Mac Pilot Results.md`.

## Method
- 18 real persona lines (Mira, Frieren, Megumin, Ruslana) in en/es/ja, taken from Mac transcripts
  2026-11-21..23, plus translations. All models ran through mlx-audio 0.6.7 on Apple MLX, one
  process per model, with a warm-up line excluded.
- First audio is when the first audio chunk is ready (the whole sentence for models that do not
  stream). RTF is synthesis time divided by audio length, so below 1 is faster than real time.
  Footprint is macOS `time -l` "peak memory footprint" for the whole process.
- Baseline from transcripts (`first_audio_ms - first_sentence_ms`, n=110): ElevenLabs Flash v2.5
  median **268 ms**, p90 349 ms.
- Usage: desk sessions speak a median of **~3,100 characters an hour**. Gaming is estimated at
  2×, about 6,200 an hour.
- Listening: Commander's blind 1–5 scores, 102 clips, model labels shuffled.

## Results: M4 Mac Mini, 16 GB

| Model (mlx-community, 6-bit unless noted) | First audio, median / p90 | RTF | Footprint | Listening (1–5) |
|---|---:|---:|---:|---:|
| Qwen3-TTS 1.7B VoiceDesign | 251 / 257 ms | 0.52 | 3.8 GiB | **4.22** (round 2: **4.50**) |
| Qwen3-TTS 1.7B CustomVoice | 226 / 267 ms | 0.51 | 3.8 GiB | 3.06 (slow, slurred on "sleepy"/"weary" instructions) |
| Qwen3-TTS 1.7B Base (clones from sample) | 720 / 904 ms | 0.62 | 7.5 GiB | round 2: 4.28 (es/ja 3.7) |
| Qwen3-TTS 0.6B Base (clones from sample) | 455 / 581 ms | 0.45 | 6.7 GiB | round 2: **4.39** |
| Qwen3-TTS 0.6B CustomVoice | 155 / 158 ms | 0.40 | 3.0 GiB | round 2: 3.67 |
| Kokoro 82M (4-bit) | 393 / 590 ms | 0.05 | 4.6 GiB | 3.78 |
| Chatterbox Multilingual v3 (no streaming) | 9,958 / 13,115 ms | 1.30 | 8.8 GiB | 4.06 |
| Chatterbox Turbo (4-bit, English only) | 1,181 / 1,218 ms | 1.46 | 7.7 GiB | 1.00 (garbled) |
| Fish Audio S2 Pro (8-bit) | 12,685 / 29,638 ms | 3.91 | 12.6 GiB | 4.39 |

## Results: M1 Mac Mini, 8 GB (home server with a ~4 GB VM, already swapping)

| Model | First audio, median / p90 | RTF |
|---|---:|---:|
| Qwen3-TTS 1.7B VoiceDesign | 565 / 629 ms | 0.98 |
| Qwen3-TTS 0.6B CustomVoice | 364 / 481 ms | 0.76 |
| Qwen3-TTS 0.6B Base (clones) | 902 / 1,413 ms | 0.86 |
| Kokoro 82M | 685 / 1,155 ms | 0.09 |

## Findings
- **Qwen3-TTS 1.7B VoiceDesign is the pick for desk mode on the M4.** It starts speaking as fast
  as ElevenLabs and is the steadiest across languages (round 1: es 4.3 where the others score
  about 3). It ranks behind only Fish (unusably slow) in round 1, is top in round 2, and needs
  3.8 GiB. Compare scores within a round; the scale moved a little between sessions.
- **0.6B Base cloning (4.39) is the option** when a persona's ElevenLabs voice identity matters.
  It is the most even across personas and better for Ruslana than VoiceDesign, but it starts in
  455 ms and uses 6.7 GiB. 1.7B Base cloning is weaker in es/ja (3.7), and 0.6B CustomVoice is
  fast but the weakest Qwen.
- Chatterbox, Turbo and Fish generate slower than real time on the M4, so they are out for live use.
- **Cloning costs latency.** The 20 s sample is re-read with every line. A shorter sample may cut
  the delay.
- **Ruslana's transliterated Russian was mispronounced by every model.** "Pojaluysta" is a French
  transliteration; "kopiyki" is Ukrainian (копійки). In the spelling test (18 clips, VoiceDesign
  and 1.7B Base), Cyrillic and phonetic respelling both averaged 4.17 against 4.00 as written,
  which is within noise. VoiceDesign was best with Cyrillic (4.3), Base with phonetic (4.7). Try
  standard transliteration in the persona prompt before building a per-backend spelling layer.
- **An M1 has no margin for the 1.7B model** (RTF 0.98). The 0.6B Base runs at RTF 0.86 there,
  and only the weakest model (0.6B CustomVoice) has real margin. For a dedicated server: 16 GB,
  newer than M1; 32 GB is not needed.
- **Ruslana round 2 (27 clips, per-sentence synthesis, new "young, upbeat" description): Cyrillic
  wins.** Averages: Cyrillic 4.11, phonetic 3.89 (sometimes strange emphasis), as written 3.22.
  Every model said Пожалуйста and боже мой correctly in Cyrillic. 0.6B Base cloning her
  ElevenLabs voice (Marichka) scored **5.00** with Cyrillic. VoiceDesign fell from 4.0 to 2.89 with
  the new description, so its voice is very sensitive to wording. Decisions: Ruslana writes
  Slavic words in Cyrillic, and the local voice server needs a model and voice mode per persona
  (designed or cloned). Running VoiceDesign and 0.6B Base together needs about 10.5 GiB.
- **Synthesize per sentence, as `voice.py` does.** The bake-off first fed whole lines, which ran
  "…La Gran Q. Da, let…" together.
- Qwen does not act out `[tags]` (ADR 0020); emotion comes from a written instruction instead.
  Fish is the only model tested that acts out tags.

## M1 stress test (2026-11-23, gate for ADR 0029)

The test ran 40 real multi-sentence turns (116 sentences), synthesized sentence by sentence
back to back, with playback simulated from each chunk's ready time. The VM was running.

| Run | Sentence first audio p50 / p95 | Over 1 s | Turns with a gap | RTF |
|---|---:|---:|---:|---:|
| 0.6B Base, 20 s sample | 886 / 1,112 ms | 29% | 23 / 40 | 1.01 |
| 0.6B Base, 8–9 s sample | 717 / 903 ms | 1.7% | 8 / 40 | 0.91 |
| 0.6B CustomVoice | 294 / 320 ms | 0% | 0 / 40 | 0.76 |

- **A short sample is required for cloning on the M1.** Every gap fell at a sentence start (the
  ~0.7 s re-read of the sample), none mid-sentence. The short sample leaves an occasional ~0.2 s
  longer pause between sentences.
- **`kill -9` of the worker freed its memory within 2 s.** Details and the go decision are in ADR 0029.

## Built (2026-11-23)

The server is `voice-server/` (supervisor + worker, launchd on the M1) and the client is
`backends/local/` (lease, 1 s first-audio deadline, stall parking). The contract is in
`docs/voice-server.md`.

End to end from the Mac through Familiar's chain:
- First audio: Mira 836 ms, Ruslana 754 ms, Megumin (ja) 756 ms.
- A persona without a local voice falls back per sentence.
- A busy worker answers 503 at once.
- The worker unloads 60 s after the last lease.

The first desk session (same evening) never reached the M1: macOS Local Network privacy gave
Familiar.app `No route to host`, while the terminal tests had passed. An osacompile applet
can't be granted Local Network access (shared executable UUID, ad hoc signature; Apple TN3179).
A launchd daemon holding an SSH tunnel to the M1 fixes it (ADR 0030): first audio through the
tunnel was 0.77–0.79 s, the same as direct. The client now logs lease failures, and the GUI
shows the server state and which backend voiced each sentence.

Open:
- Cache the processed reference prompt per sentence (the ~0.7 s re-read).
- Samples for the voices that still use ElevenLabs only.
