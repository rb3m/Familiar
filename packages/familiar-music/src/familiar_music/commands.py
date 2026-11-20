"""CLI commands for music library indexing and enrichment."""

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path

from familiar.interfaces import PLUGIN_API_VERSION

from .library.enrich import Enricher
from .library.index import LibraryIndex
from .library.paths import database_path, library_roots
from .library.scanner import Scanner


def _scan_progress(scanned: int, total: int | None, current: str) -> None:
    trunc = (current[:60] + "…") if len(current) > 60 else current
    sys.stdout.write(f"\rScanning ({scanned} files)... {trunc}\033[K")
    sys.stdout.flush()


def _enrich_progress(done: int, total: int, artist: str) -> None:
    trunc = (artist[:40] + "…") if len(artist) > 40 else artist
    sys.stdout.write(f"\rEnriching ({done}/{total})... {trunc}\033[K")
    sys.stdout.flush()


def _run_scan(index: LibraryIndex, roots: list[Path], args: argparse.Namespace) -> int:
    scanner = Scanner(index)
    res = scanner.scan(roots, full=args.full, progress_cb=_scan_progress)
    sys.stdout.write("\n")
    sys.stdout.flush()
    print(
        f"Scan complete: {res['scanned']} scanned, {res['added_or_updated']} updated, "
        f"{res['skipped']} unchanged, {res['unreadable']} unreadable, "
        f"{res['dup_groups']} duplicate groups."
    )
    if not args.enrich:
        return 0
    enricher = Enricher(index)
    eres = enricher.enrich_all(progress_cb=_enrich_progress)
    sys.stdout.write("\n")
    sys.stdout.flush()
    print(f"Enrichment complete: {eres['enriched']} enriched, {eres['failed']} failed.")
    return 0


def _run_enrich(index: LibraryIndex, args: argparse.Namespace) -> int:
    enricher = Enricher(index)
    res = enricher.enrich_all(limit=args.limit, progress_cb=_enrich_progress)
    sys.stdout.write("\n")
    sys.stdout.flush()
    print(
        f"Enrichment complete: {res['enriched']} enriched, {res['failed']} failed "
        f"(total candidates: {res['processed']})."
    )
    return 0


def _run_status(index: LibraryIndex) -> int:
    stats = index.stats()
    print(f"Tracks: {stats['tracks']}")
    print(f"Missing: {stats['missing']}")
    print(f"Total rows: {stats['total_tracks']}")
    print(f"Duplicate groups: {stats['dup_groups']}")
    print(f"Artists: {stats['artists_enriched']} enriched, {stats['artists_pending']} pending")
    print(f"Plays recorded: {stats['plays_recorded']}")
    for outcome in ("played", "skipped"):
        artists = ", ".join(f"{a['artist']} ({a['count']})" for a in stats[f"top_{outcome}"])
        print(f"Top 5 artists by {outcome} (last 90 days): {artists or '(none)'}")

    tags: Iterable = stats.get("top_tags", [])
    formatted_tags = [f"{item['tag']} ({item['count']})" for item in tags]
    tag_str = ", ".join(formatted_tags) if formatted_tags else "(none)"
    print(f"Top 20 tags: {tag_str}")
    return 0


class MusicCommand:
    """Familiar CLI command plugin for the local music library."""

    name = "music"
    api_version = PLUGIN_API_VERSION
    help = "Manage the local music library index"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        subparsers = parser.add_subparsers(dest="subcommand", required=True)

        scan_parser = subparsers.add_parser("scan", help="Scan local music roots")
        scan_parser.add_argument(
            "--enrich",
            action="store_true",
            help="Run MusicBrainz artist enrichment after scan",
        )
        scan_parser.add_argument(
            "--full",
            action="store_true",
            help="Force full rescan ignoring size/mtime cache",
        )

        enrich_parser = subparsers.add_parser("enrich", help="Enrich artists via MusicBrainz")
        enrich_parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Limit number of artists to enrich",
        )

        subparsers.add_parser("status", help="Show library statistics")

    def run(self, args: argparse.Namespace, config) -> int:
        music_conf = getattr(config, "music", {})
        db = database_path(music_conf)
        roots = library_roots(music_conf)
        index = LibraryIndex(db)

        handlers = {
            "scan": lambda: _run_scan(index, roots, args),
            "enrich": lambda: _run_enrich(index, args),
            "status": lambda: _run_status(index),
        }
        return handlers.get(args.subcommand, lambda: 0)()
