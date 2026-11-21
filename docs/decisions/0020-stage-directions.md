# 0020: Stage directions are kept in text, cut from speech

Date: 2026-11-21
Status: Accepted; prompt contract and tag grammar amended by 0021

## Context

Gemini sometimes writes stage directions in replies, e.g. Ruslana's "*Sighs, smoke curling
past the microphone.* Oh, *darling*…", and the voice read them aloud. The same persona uses
asterisks for spoken emphasis ("*da*", "*Bozhe moy*"), which must stay. The Commander wants the
directions silent today but kept, so a later ElevenLabs v3 voice can act out audio tags such as
`[sighs]`. In six recent sessions about 50 asterisk spans were emphasis and one was a direction.

## Options

1. Only tell the model to leave directions out.
2. Only filter heuristically before the voice.
3. Ask the model for a distinct syntax (audio tags in brackets, asterisks only for emphasis) and
   filter what reaches the voice, with a conservative heuristic for asterisk directions that
   still slip through.

## Decision

Option 3.

- **Prompt contract.** `MARKUP_RULE` (in every persona prompt and the banter prompt): vocal
  sounds only as short lowercase `[tags]`, no narrated actions, asterisks only for emphasis,
  with one good and one bad example.
- **Speech filter** (`familiar/speech.py`, vocabulary in `familiar/data/speech.yaml`).
  `speakable(text, backend)` removes direction spans, unwraps emphasis, removes tags and tidies
  punctuation. A tag is `[lowercase words]`, at most three, so "[Track 2]" is spoken. A TTS
  backend with capability `audio_tags` keeps tags that are in `vocal_tags`; physical actions
  (`[adjusts vinyl]`) never reach any voice. An asterisk span is a direction when its first
  word, after lead words like "she" or "slowly", is a `direction_verbs` verb in any form
  (sigh, sighs, sighed, sighing; whole words, so "*nodes*" stays). Verbs that read as emphasis
  in their base form are in `direction_verbs_inflected` and match only as "lights", "hums".
- **Where.** The voice loop filters each sentence and skips one left empty (not played, but
  counted as delivered so the answer stays heard). `FallbackTTS` advertises `audio_tags` when
  either backend has it and filters again for the backend actually speaking, so v3 falling back
  to Gemini TTS drops tags.
  Transcript, history, GUI and banter records keep the raw text.
- **Sentences.** The splitter does not break inside an open `*…*`, `**…**` or `[…]`, newlines
  included.
- **Banter** keeps markup as written; a line is malformed only if markup other than spans and
  tags remains (parentheses, angle brackets, stray marks) or nothing speakable is left.

## Consequences

- Neither prompt nor list can guarantee every direction is caught; unmarked narration still
  speaks. The lists are data, so a missed verb is a one-line override.
- A real emphasis span that starts with an action word ("*sighing* is for amateurs") is cut.
- Review fixes (Codex): a minus sign before a number is never trimmed (punctuation is tidied
  only where something was removed); a skipped direction-only sentence counts as delivered, so
  the rest of the answer stays heard; bold spans and newlines no longer split a direction;
  whole-verb matching replaced prefix stems ("*nodes*", "*Siphon*" were being cut).
- A future v3 backend only needs `capabilities` to include `audio_tags`.

## Who agreed

- Commander: filter directions from speech, keep Ruslana's expressions, keep the directions
  for a future ElevenLabs v3 voice; review the plan before building.
- Codex (gpt-6.1-sol, workhorse): agreed on the prompt contract, parser changes and tests;
  asked to filter per backend inside `FallbackTTS`, use the `capabilities` set, define a
  reserved tag grammar, keep the heuristic a conservative fallback and keep original text.
  All adopted.
- agy: agreed; asked to check past lead words ("*slowly exhales*"), trim orphaned punctuation
  and keep non-vocal tags from a v3 voice through a whitelist. All adopted.
- Claude (coordinator): merged; inflected-only forms for ambiguous verbs, splitter guard,
  direction-only sentences skipped without playback.
