# 0015: Local music library, mpv, and a blended, taste-weighted DJ (amends 0012)

Date: 2026-11-19
Status: Accepted

## Context

The Commander's music library is now on this PC: about 12,000 playable tracks (8.1k MP3, 2.3k
FLAC, 1.7k M4A, a few loose AAC; 136 GB, ~1,000 artists), at a path that must come from config.
A 300-track sample had artist tags on 100%, titles on 97%, genre on 89%, but the genres are
coarse (a third just "rock", some junk). There are 80 Apple DRM `.m4p` files (unplayable), 103
macOS `._*` files and duplicate albums (FLAC and MP3 copies). Phase 1 (ADR 0012/0014) played
only YouTube Music through Pear Desktop, with `Track.video_id` as the identity everywhere.

The Commander chose: a **blend** (about 70% library, 20% where the DJ persona's taste overlaps,
10% YouTube Music discovery, plus voice requests for a source); **mpv** as the local player;
**MusicBrainz** artist tags to enrich genres; and **taste weighting** in this round.

## Options

1. Two players: a per-track router with one logical queue (A); one source per selection batch
   (B); everything in mpv with YouTube through yt-dlp, dropping Pear (C).
2. Identity: one source-qualified `uri`, or a separate ID field per source.
3. Index: SQLite + mutagen with incremental scans; alternatives (MPD's database, beets).
4. Free-text query to local tracks: token/synonym match (A), local embeddings (B), the LLM
   choosing a structured filter from the library's real vocabulary (C).
5. Local radio: artist + shared tags + year, or also ListenBrainz similar artists.
6. Taste: one fused score, or a lane draw followed by weighted sampling.

## Decision

- **B, behind one facade.** A `SourcedPlayer` implements `MusicPlayer` and delegates to the
  active backend (`local` = mpv, `ytm` = Pear). A selection batch (a pick plus its top-up) stays
  on one source; switching pauses the other backend first, so exactly one is audible. The `DJ`
  keeps one logical 0-100 volume and one ducking/pause owner, applied to the active backend. If
  one backend is unavailable, lanes that need it fall back to the other.
- **Identity is `Track.uri`**: `local:<uuid>` or `ytm:<videoId>` (video IDs never contain a
  colon). Recent tracks, the radio seed, queue IDs, announcements and dislikes all use it. The
  memory store migrates existing dislikes by prefixing `ytm:`.
- **Library index**: its own SQLite file in the user state dir (the memory store can be
  disabled), scanned with `mutagen`. A file keeps a stable UUID, matched on rescan by relative
  path, then by size plus a header hash (renames). mtime/size detect changes; files under a root
  that scanned fine but are gone are marked missing. `.m4p`, `._*` and unreadable files are
  skipped. Duplicates are grouped, not deleted: normalised artist + title with durations within
  a few seconds; selection prefers lossless, then bitrate. Roots come from `[music]
  library_paths` (default: the XDG Music dir). A background scan at startup never delays play;
  `familiar music scan` and `familiar music status` run it by hand.
- **Enrichment**: one MusicBrainz artist lookup per artist (MBID + weighted tags), at most
  1 request/s, generic User-Agent, cached in the index and resumable; only new artists are
  fetched later. Artist tags help selection but are not spoken as track facts.
- **Queries: C with A as fallback.** `play_music` gains optional `genres` (chosen from the
  library's own tag vocabulary, listed in the tool description at start-up), `years` and
  `source` (`library`, `youtube`, `any`). Free text that the filter doesn't cover is matched
  through a synonym data file (`genre_synonyms.yaml`, user-overridable); persona taste lines go
  through the same file. Filters are parameters to fixed SQL, never generated SQL.
- **Local radio**: same artist, shared tags, year proximity. ListenBrainz is deferred; later it
  may only rank artists already in the library, by MBID.
- **Lanes, then weighted sampling.** Each new unrequested batch (DJ turn, queue running dry)
  draws a lane from `blend` (`library: 70, persona: 20, discovery: 10`): library = weighted pick
  from the index; persona = the carrier's taste lines, local when they match, else YouTube;
  discovery = YouTube. Within a lane, weight = relevance x taste x behaviour x recency. Dislike
  is a hard block. A user skip within `skip_early_s` is negative; a song played through is
  positive, both capped and decaying; songs interrupted by Familiar count as neither. Recent
  tracks are excluded and artists are spaced within a batch. Explicit requests override the
  lanes: an artist or song plays locally when the index has it, else YouTube; a genre goes local
  when it has enough matches, unless `source` says otherwise.
- **Facts from tags**: `Track` gains `year` and `genre` (from file tags), and remarks are fed
  only those facts.

## Consequences

- mpv runs as a child process on an IPC socket in `XDG_RUNTIME_DIR`; its arguments live in
  `familiar_music/data/mpv.yaml`. It has no likes, so `like_track` reports that, and dislikes
  live only in memory, as with any player without them.
- The first scan reads ~12,000 files and the first enrichment takes ~17 minutes; until it ends,
  local genre matching uses file tags only.
- The service entry point grows from `pear` to one that builds the blend; `service = "pear"`
  keeps working as YouTube-only.
- Behaviour signals live in the library database (`plays`, keyed by `uri`) so YouTube tracks
  count too.

## Who agreed

- Codex (gpt-6.1-sol, workhorse) and Grok (grok-4.7, high): B with a single coordinating facade;
  agy preferred A (per-track router). B adopted: per-track switching races the single volume,
  pause and now-playing the DJ already assumes.
- All three: a source-qualified `uri` with a dislike migration; SQLite + mutagen, incremental,
  CLI and background scan; C with a synonym fallback, no embeddings; lane draw then weighted
  sampling with recency penalties, dislikes as hard blocks.
- Codex and Grok: duplicates only with close durations, grouped not merged; stable IDs not tied
  to the path. Grok: cap artist/genre share per batch; feed remarks only tag facts. Codex: only
  user-initiated early skips count, interrupted playback earns nothing.
- ListenBrainz: agy wanted it now; Codex and Grok deferred it (it names artists the Commander
  doesn't own). Deferred.
- Commander: blend, mpv, MusicBrainz enrichment, taste weighting this round; library path from
  config.
