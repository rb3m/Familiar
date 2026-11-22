# 0025: System health as a companion service, in games and at the desk

Date: 2026-11-22
Status: Accepted

## Context

The player wants to ask about the machine ("how hot is my GPU?", "how much disk is left?") at
any time, and to hear once when something crosses a line: a drive at its own warning
temperature, CPU or GPU near its limit, a filesystem under 10% free. Games stress the machine
most, so lookups and alerts must work during game sessions too. Nothing else is volunteered.

The first 2026-11-22 consensus round (roadmap) planned this as an ask-only desk-feed source.
The desk feed runs only without a game (ADR 0022), so that no longer fits.

What the platforms offer without root: on Linux, hwmon exposes CPU, GPU, NVMe, RAM and
motherboard sensors. Motherboard chips often have unconnected inputs that read junk (0 °C or
well over 100 °C). NVMe drives publish their own warning and critical limits for the Composite
temperature; their secondary sensors publish none and can report values that do not track the
drive. A filesystem can span several drives, and `statvfs` on a network mount can hang when the
remote host sleeps. macOS offers load, memory, filesystem space and uptime; temperatures need
private APIs or root.

## Options

1. A desk-feed source (the earlier plan): no lookups or alerts in games.
2. Run the desk feed in games for non-location sources: reopens ADR 0022 for one feature.
3. **A separate companion service, sampling in-process, in every session.**

## Decision

Option 3: a new `familiar-system` package with a `system` service (`familiar.services`),
depending only on `familiar.interfaces`, enabled by `[system] enabled` for desk and game
sessions. `describe()` stays empty: nothing in the prompt.

- **Sampling.** Readers run in a private thread pool with a timeout (a stuck read is abandoned,
  never awaited forever): temperatures every 10 s, filesystems every 60 s, one read in flight
  per kind. A reader protocol with a `{linux, darwin}` map; every field optional; absent means
  unavailable, never zero.
- **Sensors are data.** `familiar_system/data/sensors.yaml` (user overrides via `load_data`)
  matches driver and label, never `hwmonN`. Packaged defaults cover common Linux drivers: CPU
  package (`coretemp`, `k10temp`), GPU edge reported with junction for the limit (`amdgpu`),
  `nvme` Composite, RAM report-only (`spd5118`). The allow-list
  is closed: unknown chips and the motherboard inputs are not reported or spoken; the user adds
  rules. A plausibility band (configurable, e.g. 1–120 °C) drops junk. `familiar system
  sensors` prints the inventory with match status, to help write rules.
- **Limits.** The device's published limit first (NVMe `temp1_max` is the warning line), then
  the data file's fallback for that driver and label; a sensor with neither never alerts.
  Drives alert on Composite only; secondary sensors are not reported (see Context).
- **Filesystems.** Local block filesystems only, by type from the mount table; network and FUSE
  types (cifs, smb3, nfs, sshfs, …, listed in data) are never probed. One entry per
  filesystem, not per device. Alert under 10% free, re-arm above 12%.
- **Alerts.** Kind `system_notice`, priority `notable`, not `critical` (never preempts speech;
  a game's critical events go first) and not `alert` (which is critical and shares the
  per-kind gap with fuel and damage). Not a `cue`: it waits out the normal notable cooldown.
  Crossings in one sample fold into one event; `ttl_s` about 180 s so a stale warning expires.
  Dwell two samples above the line; clear 5 °C (or 2 points of free space) below it; at most
  one notice per sensor per 15 min. Latches are in memory: the first sample seeds silently, so
  a restart while already hot stays quiet. Wording names the part and the reading against its
  limit.
- **Lookup.** One tool, `system_status(aspect)` with `overview` (CPU package, GPU edge,
  tightest local filesystem, 1-min load), `thermal`, `storage`, `memory`, `load`. It returns
  the last snapshot at once, never blocking for a fresh read: value, unit (°C, bytes, s, %),
  `observed_at`, `age_s`, `stale` past two intervals, and the limit and its source where one
  applies.
- **macOS v1:** load, memory, filesystem free space, uptime; temperatures `unavailable`.
- **Not in v1:** SMART, pending updates, fans, per-core or load/memory alerts, root or
  vendor tools (`nvidia-smi`, `powermetrics`), persisted latches, refresh-on-ask.

## Consequences

- Works with any game pack and at the desk; one service, no helper process.
- Other users' hardware needs rules in their override file before it reports; the inventory
  command makes that a copy-paste job.
- A restart while a part is already over its line stays silent until it clears and re-crosses.
- Units: temperatures in °C (SI-derived, as the weather feed), sizes in bytes.

## Who agreed

- Commander: lookups and alerts in games as well as at the desk; the thresholds above; macOS
  without temperatures in v1; machine-specific findings kept out of this ADR.
- Codex (gpt-6.1-sol): separate `familiar-system` package; driver/label rules, device limit
  before data fallback; NVMe Composite only; GPU edge vs junction; filesystems deduplicated;
  `notable`, never `critical`; dwell/clear/re-arm numbers; never zero for unavailable; biggest
  risk hanging filesystem I/O. Wanted kelvin internally, persisted latches and a bounded
  refresh on stale lookups (not adopted: °C matches the rest of Familiar; memory latches and
  no refresh are simpler and two reviewers preferred them).
- agy: peer package in all sessions; plausibility clamps; device limits first; 2-tick dwell,
  5 °C re-arm; in-memory state; summary lookup; network mounts excluded (its biggest risk).
  Suggested skipping RAM sensors (kept, report-only).
- Grok (grok-4.7, high): own kind `system_notice`, not `alert` and not `cue`; folding crossings;
  15-min per-sensor cooldown; closed allow-list; private executor with timeout; one tool with
  aspects; never block. Suggested the name `familiar-health` (two reviewers chose
  `familiar-system`).
- Claude (coordinator): a live sensor survey on the development machine (junk inputs, device
  limits, unreliable secondary sensors, network mounts), whose details are kept outside the
  repo; the inventory command.
