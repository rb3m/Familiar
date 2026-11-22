# 0023: Research through agy: holding line, background lookup, spoken answer

Date: 2026-11-22
Status: Accepted

## Context

The Commander wants to ask the companion things that need a lookup: details saved in the
Obsidian vault (the January 2027 Japan trip: hotels, flights, plans) and quick facts from the
web. The companion should say something like "give me a moment", look it up in the
background, then answer in two or three sentences, with faster follow-ups. Familiar is not a
deep-research tool and should stay that way. Read-only: no note writing.

`agy` (Antigravity CLI, the Commander's paid plan) already has web search and the vault MCP
servers on each machine, so Familiar never needs the vault path and one codebase serves Linux
and macOS. ADR 0008 kept the conversational LLM on the API; this ADR uses agy only for lookups.

Measured on Linux, 2026-11-22, `gemini-3.8-flash-low`:

| Lookup | Time |
|---|---|
| Web (snow and New Chitose, USD/JPY, JR Rapid Airport) | 14–21 s |
| Vault (Sapporo hotel, Jetstar flight) | 25–47 s (151 s on the default model) |

Answers checked against the itinerary were correct.

## Options

1. Block the tool call while agy runs, with a spoken filler. Push-to-talk cancels the reply
   task, and the Director stays silent for the whole lookup.
2. A generic "deferred tool result" in core. A late result after a user turn breaks ADR
   0007's call/result pairing; a core change for one service.
3. **The tool returns "started" at once; the answer arrives later as a Director event.**
4. Our own search: Gemini grounding for the web plus a local vault index. Duplicates what agy
   already has, and needs the vault path per machine.

## Decision

Option 3, in a new `familiar-research` package (a `familiar.services` plugin, `research`).

- **Tools.** `research(question, scope, follow_up_of?)` with `scope` = `vault` or `web` returns
  `{status: "started", id}` or `{status: "busy"}`. "Started" means "say the holding line", never
  "answer from memory". `research_details(id?, question)` answers follow-ups from the cache in
  the same turn.
- **Delivery.** A `research_result` event: `notable`, `cue: true` (waits only for silence, not
  cooldowns), a long `ttl_s`, carrying the research id, the original question and the short
  answer. The Director keys it by research id (today unknown kinds are keyed by kind alone, so
  a second result would replace the first). It is spoken in a tool-free turn (as for every
  interjection), in persona, naming the question ("about your Sendai hotel…"). Timeouts and
  failures produce a short "couldn't find it" event. Not `critical`, which can cut off speech.
- **One lookup at a time.** A second request while one runs returns "busy". The job is cancelled
  (process group killed) when the service closes.
- **Follow-ups.** Cache first, then resume. The service keeps the last few results (short answer,
  bounded details, sources, agy `conversation_id`) in memory for the session only, cleared on
  close. New digging on the same thread resumes it with `--conversation`; a new topic starts a
  fresh conversation so a bad page does not carry over. No persistent `agy acp` session:
  process start is 0.05 s; the time is the model.
- **Invocation.** argv only (no shell), an empty scratch working directory, `--model` from
  config (fast tier by default), `--print-timeout`, `--output-format json`, `--json-schema`
  `{short_answer, details, sources}`, and a per-scope `--settings` permission profile. Invalid
  JSON or a timeout is a failure, never read aloud. Familiar's own API keys are removed from
  agy's environment.
- **Read-only, enforced by agy permissions, not the prompt.** Each scope profile allows only
  its tools and denies `command(*)`, `write_file(*)`, `read_file(*)`, `execute_url(*)` and
  `download_url(*)`; the vault profile also denies `read_url(*)` and allows only the vault MCP
  servers' read tools. MCP tools not on the allow list are refused in headless mode. Profiles,
  tool names and the model live in `familiar_research/data/agy.yaml` with user overrides.
- **Self-test.** `familiar research check` asks agy to write a file and run a command under each
  profile and confirms by marker files that nothing happened. The service refuses to start
  until the check has passed for the installed `agy --version`.
- **Untrusted text, masked secrets.** Page text and note excerpts never enter conversation
  history, transcripts or trip memory; the voice LLM sees the short answer, and details only
  through `research_details` after the Commander asks, labelled as untrusted reference.
  Booking-like strings (PNRs, e-ticket and confirmation numbers, check-in codes) are masked in
  everything voice-facing and in logs; the patterns are data in `agy.yaml`.
- **Sessions.** Works at the desk and in game sessions: it only speaks when asked.

## Consequences

- Answers take 15–45 s. The holding line and naming the question on delivery carry the UX.
- Familiar depends on agy and its machine-local MCP config for this feature; without agy the
  service is a config error, the rest of Familiar is unaffected.
- **Known limit:** denying `read_url` does not stop Gemini's server-side URL and search tools; a
  vault-scope test returned example.com's title. A planted instruction in the vault (e.g. a web
  clipping) could in principle push vault text into a web request. The voice LLM still only
  sees the masked short answer. Phase 2: an OS sandbox around agy and the MCP servers.
- The vault profile must list every read tool the model may reach for; a missing one gives an
  empty answer (seen in testing).
- The RAG index (`rag-mcp-server`) was stale (no `JapanTrip2027`); vault search used
  `ipad-vault-mcp` instead. Refreshing it is outside Familiar.
- Not in v1: concurrent lookups, ACP, a core deferred-result mechanism, note writing.

### Found while building (2026-11-22)

- **Notes are read through the vault server, never `read_file`.** Denying `read_file(*)` stays:
  in agy a deny beats a path-scoped allow, and without the deny a vault lookup read a probe file
  outside the vault. The vault instructions name `get_note` and its `path_or_title` argument;
  loosely worded instructions made the fast model send the wrong argument name.
- **agy gets the user's interpreter.** Run from Familiar's virtualenv, agy started the vault MCP
  server (`python3 server.py`) with our venv's Python, which lacked its dependencies, so the
  server died at start. `AgyRunner.environment()` drops `VIRTUAL_ENV` and our venv's `bin` from
  `PATH`.
- **The self-test has a positive control.** Each scope first runs unrestricted in a throwaway
  directory; if the markers don't appear there the model never tried, and the check fails
  instead of passing vacuously.
- The profile hash covers only allow/deny rules, so rewording instructions needs no re-check.
- Measured end to end through the service: vault lookup 58 s, resumed follow-up 23.5 s, booking
  codes masked in answers and details.

## Who agreed

- Commander: agy as the backend; holding line then 2–3 sentences; faster follow-ups; read-only;
  fast model (not a deep-research tool); mask booking details from speech; allow-list plus
  self-test now, OS sandbox in phase 2.
- Codex (gpt-6.1-sol): started-plus-event; result ids and the Director overwrite; not
  `critical`; cache then resume, no ACP; fail-closed permissions outside the prompt; keep raw
  text out of history; keep vault text out of web queries (led to the scope split); OS sandbox
  for agy and MCP servers (deferred to phase 2).
- agy: started-plus-event; cache with LRU; no ACP; OS sandbox recommended; argued for cache-only
  in v1 (not adopted: faster follow-ups were the Commander's requirement).
- Grok (grok-4.7, high): started-plus-event with `cue`, long TTL and keying by id; one job at a
  time; voice model sees only the short answer; argued for speaking agy's answer verbatim (not
  adopted: a tool-free persona turn keeps the voice and an injection has nothing to act on) and
  for refusing to run without an OS sandbox (deferred by the Commander).
- Claude (coordinator): live tests of latency, permissions and the self-test (which caught that
  allow-lists alone do not stop writes or shell; deny rules do); scope split; masking.
