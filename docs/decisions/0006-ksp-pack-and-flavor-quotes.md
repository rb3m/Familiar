# 0006: KSP pack via kRPC, and core-gated flavor quotes

Date: 2026-11-16. Status: accepted.

## Context
KSP is the next M4 game. The user installed the kRPC 0.7.0 server mod (KSP 1.12.5, auto-start
servers and auto-accept clients on). They also want the companion to paraphrase famous real
astronaut/cosmonaut quotes at fitting moments, but rarely ("not one small step every EVA").

## Options
- Telemetry: Telemachus Reborn (HTTP/WebSocket JSON, needs an antenna part or partless mode)
  vs kRPC (protobuf RPC, official Python client, also allows vessel control later). With kRPC:
  sync client in a worker thread polling, kRPC streams, or a custom asyncio protobuf client.
- Quotes: gate inside the KSP pack vs a generic core mechanism reusable by Elite/MSFS.

## Decision
- kRPC with the official sync client on one worker thread, polling at 2 Hz, no streams (streams
  break across scene changes); disconnected snapshots with jittered backoff; the pure
  EventDetector re-baselines after reconnect, vessel switch or scene change.
- v1 events: liftoff, staged, orbit_reached, soi_changed, reentry, landed, splashed, eva_started,
  warp_changed, low_charge, low_fuel. Crash, max-Q and docking are deferred (polling can't tell
  crashes from staging or unloading). Low fuel uses whole-vessel liquid fuel so normal stage
  burnouts don't warn.
- Flavor: packs attach candidates (`event.data["flavor"]`); the core `FlavorKeeper` gates them
  (20%, 30 min global, 14 days per quote, never on critical events) and records use only once
  spoken, persisted in the memory DB (schema v2). Quotes are curated YAML with checked sources;
  popularly credited lines are `attributed` and the companion hedges them.

## Who agreed
agy and codex both recommended the sync client in a thread with polling, deferring max-Q and
docking, and the content-in-pack / gate-in-core split. agy would include crash detection in v1;
codex advised deferring it; we deferred. Codex added: gate after Director acceptance, record on
delivered speech, exclude critical events (adopted).
