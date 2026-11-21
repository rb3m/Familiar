# 0021: A hand-off target must act; no audio tags in prompts yet (amends 0018, 0020)

Date: 2026-11-21
Status: Accepted

## Context

The Commander asked for "punchy rock" and nothing played. Frieren (controls) answered "Ruslana,
the Commander wants some punchy rock" in words, with no `hand_off` call. Reproduced with the
live crew and Gemini (flash-lite), six runs each:

| Variant | Frieren hands off | Ruslana plays |
|---|---|---|
| ADR 0020 rule, `[sighs]` example | 0/6 | 0 |
| Shorter tag rule, no example | 6/6 | 2/6 |
| Tags in braces, `{sighs}` (the Commander's idea) | about 3/6 | 1 |
| No tags at all | every run | 1/5 |
| Target's first round must be a tool call | every run | 6/6 |

Two faults: asking for vocal tags made the model reply in prose instead of calling tools (new
in 0020), and a DJ handed a job often described it instead of doing it, sometimes writing the
call as text ("[play_music(kind='genre', query='punchy rock')]"). The second predates 0020: the
05:13 anthem request on 2026-11-21 went the same way.

## Options

1. Prompt wording only.
2. Let the model write calls as text in a delimiter (`{play_music: rock}`) and parse them.
3. Make the hand-off target's first round a required native tool call.

## Decision

Options 1 and 3; not 2.

- **Required call.** An optional LLM capability `REQUIRE_TOOL` (plugin API 2.4, additive):
  `stream(messages, system, tools, require_tool=True)` means the reply must call one of
  `tools`. Gemini maps it to function calling mode ANY with those names. Core passes it only to
  backends that declare it, only on the first round of a hand-off target on a player turn;
  the next round (after the results) speaks freely. `hand_off` stays offered, so a chain
  (controls lowers the gear, then passes the music on) still works.
- **Prompt.** `MARKUP_RULE` asks for spoken words only: no stage directions, actions, sound
  effects or bracketed notes; asterisks only for emphasis. Asking for tags returns when a
  tag-capable voice (ElevenLabs v3) is added, behind that backend's `audio_tags` capability, and
  its wording must be re-tested against tool use first.
- **Guard.** Text that looks like a tool call (`[play_music(…)]`, `{play_music: …}`,
  `[Tool call: …`) is never spoken and is logged as a warning. Any all-lowercase bracketed note
  ("[playing punchy rock music]") counts as a stage note; "[Track 2]" and "[Intro]" are spoken.

## Consequences

- Live re-test after the change: 6/6 hand-offs, 6/6 `play_music` calls.
- A hand-off that wasn't really a job ("Ruslana, what do you think of this song?") still makes
  the target pick a tool; `now_playing` is the harmless choice, and a wrong pick is audible and
  reversible.
- Text-call parsing (option 2) was rejected: the model follows formatting rules unevenly,
  streamed sentences can split a call, arguments would be unvalidated, any text (song titles,
  other crew lines) could fire actions, and it would only rescue the minority of runs where a
  call was written out at all.

## Who agreed

- Commander: proposed a different delimiter or text calls; chose the native required call
  after comparing robustness and misinterpretation risk.
- Claude (coordinator): ran the live comparisons above; recommended options 1 and 3 with the
  text-call guard.
- Codex (gpt-6.1-sol, workhorse) reviewed the implementation: the first text-call guard (a
  regex) ate ordinary speech ("A tool call lets me…") and stopped at the first bracket inside
  list arguments; replaced by a scanner that starts only at an opening `[` or `{`, follows
  nesting and quotes to the matching close, and cuts an unterminated call to the end.
