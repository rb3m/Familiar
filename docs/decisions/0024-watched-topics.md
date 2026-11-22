# 0024: Watched topics: scheduled agy checks, rule-confirmed remarks

Date: 2026-11-22
Status: Accepted

## Context

The Commander wants the companion to keep an eye on topics for a limited time, first a trip
abroad: advisories, entry rules, airline and airport changes, rail works on travel days, natural
hazards, closures at planned places, the exchange rate and health news. The watch switches itself
off at departure (trip weather is covered by a separate app). Checks twice a day; unprompted
remarks only for items that need action, everything else on request. The foundation should
serve other topics later.

The research backend (ADR 0023) already runs read-only, self-tested `agy` web lookups. The
desk feed (ADR 0016) speaks only in desk sessions (ADR 0022), and its helper process runs only
while one is open. A live test of two checks (25–34 s each) returned empty lists that could not
show whether agy had searched at all.

## Options

1. Checks as a desk-feed source calling agy from the helper. The helper is another process: it
   would skip the research self-test and one-job lock and is killed when the session closes.
2. A shared `ResearchRunner` interface in core, used by a desk source (agy's suggestion).
3. **Checks run in the research service; a small desk source in the same package only reads
   their saved state and voices confirmed action items.**

## Decision

Option 3, in `familiar-research`.

- **Topics are private data.** One YAML file per topic in `~/.config/familiar/topics/`; the repo
  ships only a generic example. A topic has an id and title, an `active_until` timestamp with
  timezone (checked in code before every lookup and every remark), `check_times` (local times,
  e.g. 08:00 and 20:00), a short private context (dates, places), and areas. Each area has a
  question, whether it may ever remark, and the official domains that may confirm an action.
- **Schedule, honestly.** Two check times a day, *run whenever Familiar is on*. The research
  service keeps `next_due`, per-area last success and a saved jitter in
  `$XDG_STATE_HOME/familiar/topics/<id>.json`. On start, one catch-up if a slot was missed (no
  replays). Checks are silent and run in desk and game sessions; a lookup the player asks for
  goes first and a scheduled check waits.
- **One agy call per check** (web profile, JSON schema) covering every area, each reporting
  coverage (`searched`, sources consulted, status) and items (headline, summary, proposed
  severity, publication date, canonical source URL). Split per area only if quality suffers.
- **New means a new source URL.** An item is new only if its canonical URL was not seen before
  and it is dated after the area's last successful check; no URL, not new, never spoken. Seen
  URLs persist in the state file (bounded).
- **The model proposes, rules confirm.** `action` stands only if the area may remark, the
  source domain is on that area's official list, the item touches the trip's dates or places,
  and it is first seen; otherwise it is stored as `watch`. At most one action remark per check.
  Exchange rate and health areas never remark; a rate alert, if ever wanted, is a number
  compared in code.
- **Empty is not "all clear".** Coverage per area (checked when, which sources, failures) is
  stored and reported; a failed or empty area is shown as such, never as nothing to worry about.
- **Remarks: desk only, worded as a pointer.** A `topics` desk source (a `familiar_desk.sources`
  entry point in this package, structurally typed, importing nothing from `familiar_desk`) reads
  the state file and emits one event per confirmed action item, with a stable id, once
  (announced ids are saved). The summary tells the companion to say the watch *flagged* a
  notice from the named authority about the named date and that it is worth checking the link,
  never to state it as fact. Ordinary findings never enter the shared desk events list.
- **On request.** A research-service tool `topic_status(topic?, area?)` answers at once from the
  saved state: last check time, a stale flag if a slot was missed, per-area coverage and a few
  masked headlines with source and date, labelled as untrusted reference. Digging deeper uses the
  existing `research` tool.
- **Not in v1:** RSS feeds (agy's search covers the areas; deterministic keyword feeds may come
  later), unattended checks while Familiar is off, refresh-on-ask, autonomous escalation.

## Consequences

- If Familiar is off for days, the watch is stale; `topic_status` says so.
- The trip's itinerary details live only in the private topic file and the state file; agy's
  web queries include dates and places but no booking data.
- Up to two agy calls a day per topic on the Commander's plan.
- A false or injected "action" needs an official domain on the allow-list and is spoken as
  "worth checking", which limits but does not remove the risk of a misleading flag.

## Who agreed

- Commander: all eight areas; remarks only for action items, everything else on request; off at
  departure; twice a day; rule-confirmed remarks worded as pointers; desk only.
- Codex (gpt-6.1-sol): topics in `familiar-research` with a structural desk source; honest
  "two slots when running"; persisted due time, one catch-up, saved jitter; per-area coverage
  and "empty is not all clear" (its biggest risk); deterministic rules authorise `action`;
  findings outside the events list; interactive lookups first. Wanted a cross-process
  execution lease (not needed: checks stay in one process).
- agy: deterministic severity rules; cached `topic_status`; only action items create events;
  cut RSS in v1. Proposed a core `ResearchRunner` interface (not adopted: no core change
  needed) and two grouped calls per check (one call adopted, split later if needed).
- Grok (grok-4.7, high): checks must not run in the desk helper (decided the boundary); URL
  seen-set; data rules promote to `action` with an official-domain allow-list and a cap of one;
  FX and health ask-only. Argued for no unprompted remarks in v1 (not adopted: the Commander
  chose rule-confirmed remarks worded as pointers).
- Claude (coordinator): live checks showing empty answers can't prove a search happened (led to
  coverage reporting); the pointer wording as the middle path.
