# 0007: Structured LLM tools and core telemetry alerts

Date: 2026-11-16
Status: Accepted

## Decision

Use API 2.0 structured assistant calls and tool result messages for LLM tool round trips.
Provider metadata, including Gemini thought signatures, survives history and retransmission.
Core validates and dispatches tools, bounds execution to three rounds and preserves complete
call/result groups when trimming or interrupting history. Interjections offer no tools.

Core owns session-only telemetry alerts. Game packs implement an optional `Watchable` protocol
with SI quantity descriptions and cached, timestamped samples; they contain no alert rules.
Alerts bind to vessel/job identity, suspend on invalid samples and disappear on context changes.
A sleeping-when-idle 2 Hz task emits one-shot critical events. Stable rate estimates can cover
speech latency; unsafe predictions fall back to observed crossings.

## Consequences

External API 1 plugins must update their message translation and declare major version 2.
Packs remain independent of core implementation modules. Alerts need no database or migration,
and disappear at session shutdown. Live Gemini and game verification remain separate from the
network-free test suite.

## Who agreed

agy and codex both recommended structured tool messages with a major API bump, a core
AlertKeeper fed by an optional pack protocol, edge-triggered one-shot alerts that report
`already_satisfied`, and session-only state. Rounds: agy suggested 2, codex 3 plus a tool-free
final call (adopted). Lead time: agy wanted prediction from the start; codex preferred actual
crossings first. Adopted prediction with codex's guards (stable rate, normal spacing, off during
warp and after discontinuities), since speech latency at KSP climb rates overshoots by hundreds
of metres.

## Live check (2026-11-16, gemini-3.5-flash-lite)

The tool round trip (including thought signatures) works. With only a one-line instruction the
model sometimes claimed an alert without calling `set_alert`; the stronger `ALERT_RULE` in
conversation.py brought trials to 6/6 (503s from the free tier excluded).
