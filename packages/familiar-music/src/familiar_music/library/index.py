"""SQLite schema and LibraryIndex query interface."""

import collections.abc
import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any

from familiar.interfaces import load_data


def accent_fold(text: str | None) -> str:
    """Casefold and strip combining accents for index matching."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).casefold()


def tag_key(text: str) -> str:
    """Canonical key shared by file genres, enrichment and query filters."""
    return "".join(c for c in accent_fold(text) if c.isalnum())


def primary_artist(text: str, patterns: list[str] | None = None) -> str:
    """Remove featured credits using overridable patterns, preserving band names."""
    if patterns is None:
        patterns = load_data("familiar_music", "library").value.get("featured_artist_patterns", [])
    for pattern in patterns:
        text = re.split(pattern, text, maxsplit=1, flags=re.IGNORECASE)[0]
    return text.strip()


def store_artist_tags(
    conn: sqlite3.Connection, name_norm: str, tags: dict, *, replace: bool = True
) -> None:
    if replace:
        conn.execute("DELETE FROM artist_tags WHERE name_norm = ?", (name_norm,))
    for display, count in tags.items():
        key = tag_key(display)
        if not key:
            continue
        conn.execute("INSERT OR IGNORE INTO tag_display VALUES (?, ?)", (key, display.casefold()))
        conn.execute(
            "INSERT INTO artist_tags VALUES (?, ?, ?) ON CONFLICT(name_norm, tag) "
            "DO UPDATE SET count = MAX(count, excluded.count)",
            (name_norm, key, int(count)),
        )


def _apply_years_filter(
    years: tuple[int, int] | list[int] | set[int] | int | None,
    conditions: list[str],
    params: list[Any],
) -> None:
    if years is None:
        return
    if isinstance(years, int):
        conditions.append("t.year = ?")
        params.append(years)
        return
    if isinstance(years, (tuple, list)) and len(years) == 2:
        conditions.append("t.year >= ? AND t.year <= ?")
        params.extend([years[0], years[1]])
        return
    if isinstance(years, (list, set, tuple)):
        placeholders = ",".join("?" for _ in years)
        conditions.append(f"t.year IN ({placeholders})")
        params.extend(list(years))
        return


def _apply_genres_filter(
    genres: list[str] | tuple[str, ...] | str | None,
    conditions: list[str],
    params: list[Any],
) -> None:
    if not genres:
        return
    genre_list = [genres] if isinstance(genres, str) else list(genres)
    keys = list({tag_key(g) for g in genre_list} - {""})
    if not keys:
        conditions.append("0")
        return
    placeholders = ",".join("?" for _ in keys)
    conditions.append(
        f"(t.uuid IN (SELECT uuid FROM track_genres WHERE tag IN ({placeholders})) "
        f"OR t.artist_norm IN (SELECT name_norm FROM artist_tags WHERE tag IN ({placeholders})))"
    )
    params.extend(keys + keys)


class LibraryIndex:
    """SQLite-backed index of local music tracks and artists."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.create_function("accent_fold", 1, accent_fold)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            version = conn.execute("SELECT version FROM schema_version").fetchone()
            old_artists = []
            if version is None or version[0] != 1:
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'artists'"
                ).fetchone()
                if exists:
                    old_artists = [dict(r) for r in conn.execute("SELECT * FROM artists")]
                conn.executescript(
                    "DROP TABLE IF EXISTS tracks; DROP TABLE IF EXISTS artists; "
                    "DROP TABLE IF EXISTS artist_tags; DROP TABLE IF EXISTS track_genres; "
                    "DROP TABLE IF EXISTS tag_display; DELETE FROM schema_version; "
                    "INSERT INTO schema_version VALUES (1);"
                )
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tracks (
                    uuid TEXT PRIMARY KEY,
                    root TEXT NOT NULL,
                    relpath TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    header_hash TEXT NOT NULL,
                    title TEXT NOT NULL,
                    artist TEXT NOT NULL,
                    artist_norm TEXT NOT NULL,
                    album_artist_norm TEXT,
                    search_text TEXT NOT NULL,
                    album_artist TEXT,
                    album TEXT,
                    year INTEGER,
                    genre TEXT,
                    duration_s REAL NOT NULL,
                    codec TEXT NOT NULL,
                    bitrate INTEGER,
                    lossless INTEGER NOT NULL,
                    missing INTEGER NOT NULL DEFAULT 0,
                    dup_group TEXT
                );

                CREATE TABLE IF NOT EXISTS artists (
                    name_norm TEXT PRIMARY KEY,
                    display TEXT NOT NULL,
                    mbid TEXT,
                    enriched_at TEXT,
                    enrich_error TEXT,
                    enrich_error_at TEXT
                );

                CREATE TABLE IF NOT EXISTS artist_tags (
                    name_norm TEXT NOT NULL, tag TEXT NOT NULL, count INTEGER NOT NULL,
                    PRIMARY KEY (name_norm, tag)
                );
                CREATE TABLE IF NOT EXISTS track_genres (
                    uuid TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY (uuid, tag)
                );
                CREATE TABLE IF NOT EXISTS tag_display (
                    tag TEXT PRIMARY KEY, display TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_artist_tags_tag ON artist_tags(tag, name_norm);
                CREATE INDEX IF NOT EXISTS idx_track_genres_tag ON track_genres(tag, uuid);
                CREATE INDEX IF NOT EXISTS idx_tracks_artist_norm ON tracks(artist_norm);
                CREATE INDEX IF NOT EXISTS idx_tracks_album_artist_norm
                    ON tracks(album_artist_norm);
                CREATE TABLE IF NOT EXISTS plays (
                    uri TEXT NOT NULL,
                    at TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK(outcome IN ('played', 'skipped')),
                    listened_s REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tracks_root_relpath ON tracks (root, relpath);
                CREATE INDEX IF NOT EXISTS idx_tracks_size_hash ON tracks (size, header_hash);
                CREATE INDEX IF NOT EXISTS idx_tracks_dup_group ON tracks (dup_group);
                CREATE INDEX IF NOT EXISTS idx_tracks_missing ON tracks (missing);
                CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks (artist);
                CREATE INDEX IF NOT EXISTS idx_artists_enriched ON artists (enriched_at);
                """
            )
            self._restore_artists(conn, old_artists)

    def _restore_artists(self, conn: sqlite3.Connection, rows: list[dict]) -> None:
        if not rows:
            return
        patterns = load_data("familiar_music", "library").value.get("featured_artist_patterns", [])
        for row in rows:
            display = primary_artist(row["display"], patterns)
            norm = accent_fold(display)
            conn.execute(
                "INSERT INTO artists (name_norm, display, mbid, enriched_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(name_norm) DO UPDATE SET "
                "mbid = COALESCE(artists.mbid, excluded.mbid), "
                "enriched_at = COALESCE(artists.enriched_at, excluded.enriched_at)",
                (norm, display, row.get("mbid"), row.get("enriched_at")),
            )
            store_artist_tags(conn, norm, json.loads(row.get("tags_json") or "{}"), replace=False)

    def by_uuid(self, uuid_str: str) -> dict | None:
        """Fetch preferred, non-missing track by UUID."""
        clean_uuid = uuid_str.removeprefix("local:")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM tracks
                WHERE uuid = ? AND missing = 0 AND (dup_group IS NULL OR dup_group = uuid)
                """,
                (clean_uuid,),
            ).fetchone()
            if not row:
                return None
            return dict(row)

    def artists_matching(self, text: str) -> list[dict]:
        """Find artists matching text (casefold, accent-insensitive)."""
        folded = accent_fold(primary_artist(text))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT a.* FROM artists a
                WHERE INSTR(accent_fold(a.display), ?) > 0
                   OR INSTR(a.name_norm, ?) > 0
                ORDER BY a.display COLLATE NOCASE
                """,
                (folded, folded),
            ).fetchall()
            return [dict(r) for r in rows]

    def tracks_by_artist(self, name: str) -> list[dict]:
        """Fetch preferred, non-missing tracks for an artist."""
        folded = accent_fold(primary_artist(name))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tracks
                WHERE (artist_norm = ? OR album_artist_norm = ?)
                  AND missing = 0
                  AND (dup_group IS NULL OR dup_group = uuid)
                ORDER BY album, title
                """,
                (folded, folded),
            ).fetchall()
            return [dict(r) for r in rows]

    def search(self, text: str) -> list[dict]:
        """Substring search on artist, title, or album (casefold, accent-insensitive)."""
        folded = accent_fold(text)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tracks
                WHERE INSTR(search_text, ?) > 0
                AND missing = 0
                AND (dup_group IS NULL OR dup_group = uuid)
                ORDER BY artist, album, title
                """,
                (folded,),
            ).fetchall()
            return [dict(r) for r in rows]

    def filter(
        self,
        *,
        genres: list[str] | tuple[str, ...] | str | None = None,
        years: tuple[int, int] | list[int] | set[int] | int | None = None,
        artist: str | None = None,
        exclude: collections.abc.Iterable[str] = (),
    ) -> list[dict]:
        """Filter tracks by genres, years, artist, and exclusions with parameterised SQL."""
        conditions = ["t.missing = 0", "(t.dup_group IS NULL OR t.dup_group = t.uuid)"]
        params: list[Any] = []

        cleaned_exclude = [u.removeprefix("local:") for u in exclude]
        if cleaned_exclude:
            placeholders = ",".join("?" for _ in cleaned_exclude)
            conditions.append(f"t.uuid NOT IN ({placeholders})")
            params.extend(cleaned_exclude)

        if artist:
            folded_artist = accent_fold(primary_artist(artist))
            conditions.append("(t.artist_norm = ? OR INSTR(t.artist_norm, ?) > 0)")
            params.extend([folded_artist, folded_artist])

        _apply_years_filter(years, conditions, params)
        _apply_genres_filter(genres, conditions, params)

        sql = f"""
            SELECT t.* FROM tracks t
            WHERE {" AND ".join(conditions)}
            ORDER BY t.artist, t.album, t.title
        """

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def tag_vocabulary(self, limit: int = 50) -> list[dict]:
        """Return display tags and weighted counts for preferred available tracks."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                WITH active AS (
                    SELECT uuid, artist_norm FROM tracks
                    WHERE missing = 0 AND (dup_group IS NULL OR dup_group = uuid)
                ), counts AS (
                    SELECT g.tag, COUNT(*) AS n FROM track_genres g
                    JOIN active t ON t.uuid = g.uuid GROUP BY g.tag
                    UNION ALL
                    SELECT a.tag, SUM(a.count) FROM artist_tags a
                    JOIN active t ON t.artist_norm = a.name_norm GROUP BY a.tag
                )
                SELECT d.display, SUM(c.n) AS total FROM counts c
                JOIN tag_display d ON d.tag = c.tag
                GROUP BY c.tag ORDER BY total DESC, d.display LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [{"tag": r["display"], "count": r["total"]} for r in rows]

    def stats(self) -> dict:
        """Summary statistics of the library index."""
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
            missing = conn.execute("SELECT COUNT(*) FROM tracks WHERE missing = 1").fetchone()[0]
            active = conn.execute(
                """
                SELECT COUNT(*) FROM tracks
                WHERE missing = 0 AND (dup_group IS NULL OR dup_group = uuid)
                """
            ).fetchone()[0]
            dup_groups = conn.execute(
                "SELECT COUNT(DISTINCT dup_group) FROM tracks WHERE dup_group IS NOT NULL"
            ).fetchone()[0]
            artists_enriched = conn.execute(
                "SELECT COUNT(*) FROM artists WHERE enriched_at IS NOT NULL"
            ).fetchone()[0]
            artists_pending = conn.execute(
                "SELECT COUNT(*) FROM artists WHERE enriched_at IS NULL"
            ).fetchone()[0]

        top_tags = self.tag_vocabulary(limit=20)
        return {
            "tracks": active,
            "total_tracks": total,
            "missing": missing,
            "dup_groups": dup_groups,
            "artists_enriched": artists_enriched,
            "artists_pending": artists_pending,
            "top_tags": top_tags,
        }
