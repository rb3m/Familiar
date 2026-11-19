"""Versioned SQLite storage shared by all games and personas."""

import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .text import words

SCHEMA_VERSION = 5
CREW_MOMENT_CAP = 300
_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game TEXT NOT NULL, persona TEXT NOT NULL, started_utc TEXT NOT NULL,
    ended_utc TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    game TEXT NOT NULL, ts_utc TEXT NOT NULL, kind TEXT NOT NULL,
    place_key TEXT NOT NULL, place TEXT NOT NULL, region TEXT NOT NULL,
    activity TEXT NOT NULL, text TEXT NOT NULL, salience REAL NOT NULL,
    UNIQUE(session_id, kind, place_key)
);
CREATE TABLE IF NOT EXISTS recalls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL REFERENCES episodes(id),
    persona TEXT NOT NULL, ts_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS episodes_place ON episodes(game, place_key, session_id);
CREATE INDEX IF NOT EXISTS episodes_region ON episodes(game, region, session_id);
CREATE INDEX IF NOT EXISTS recalls_episode ON recalls(episode_id, persona, ts_utc);
"""

_FLAVOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS flavor_uses (
    game TEXT NOT NULL, item_id TEXT NOT NULL, used_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS flavor_item ON flavor_uses(game, item_id, used_at);
CREATE INDEX IF NOT EXISTS flavor_latest ON flavor_uses(used_at);
"""


def default_path() -> Path:
    root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return root / "familiar/memory.db"


def utc_text(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat()


class MemoryStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.create_function("memory_words", 1, words, deterministic=True)
        self.revision = 0
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=OFF")
            self.db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            version = self.db.execute("SELECT version FROM schema_version").fetchone()
            if version is not None and version[0] not in {1, 2, 3, 4, SCHEMA_VERSION}:
                raise ValueError(f"Unsupported memory schema version: {version[0]}")
            self.db.executescript(_SCHEMA)
            self.db.executescript(_FLAVOR_SCHEMA)
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS music_dislikes (
                    video_id TEXT NOT NULL, artist TEXT NOT NULL,
                    PRIMARY KEY(video_id, artist)
                );
                CREATE INDEX IF NOT EXISTS music_dislikes_artist ON music_dislikes(artist);
            """)
            with self.db:
                self.db.execute("BEGIN")
                self._migrate_attribution()
                self._migrate_moments()
                if version is not None and version[0] < 5:
                    self.db.execute(
                        "UPDATE music_dislikes SET video_id = 'ytm:' || video_id "
                        "WHERE instr(video_id, ':') = 0"
                    )
                if version is None:
                    self.db.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
                if version is not None and version[0] < SCHEMA_VERSION:
                    self.db.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
            self.db.execute("PRAGMA foreign_keys=ON")
        except BaseException:
            self.close()
            raise

    def _migrate_moments(self) -> None:
        """Keep fact deduplication, but allow multiple crew scenes at one place."""
        sql = self.db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'episodes'"
        ).fetchone()[0]
        if "UNIQUE(session_id, kind, place_key)" in sql:
            self._rebuild_episodes(sql)
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS episodes_fact_unique "
            "ON episodes(session_id, kind, place_key) WHERE kind != 'crew_moment'"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS episodes_place ON episodes(game, place_key, session_id)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS episodes_region ON episodes(game, region, session_id)"
        )

    def _rebuild_episodes(self, sql: str) -> None:
        sql = sql.replace("CREATE TABLE episodes", "CREATE TABLE episodes_new", 1)
        sql = sql.replace(",\n    UNIQUE(session_id, kind, place_key)", "")
        self.db.execute(sql)
        self.db.execute("INSERT INTO episodes_new SELECT * FROM episodes")
        self.db.execute("DROP TABLE episodes")
        self.db.execute("ALTER TABLE episodes_new RENAME TO episodes")

    def _migrate_attribution(self) -> None:
        """Older rows inherit the session's single persona; game facts stay grounded."""
        for table in ("sessions", "episodes", "recalls"):
            self._add_speaker(table)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(episodes)")}
        if "source" not in columns:
            self.db.execute(
                "ALTER TABLE episodes ADD COLUMN source TEXT NOT NULL DEFAULT 'game' "
                "CHECK (source IN ('player_observed', 'game', 'persona_claim'))"
            )

    def _add_speaker(self, table: str) -> None:
        columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
        if "speaker" in columns:
            return
        self.db.execute(f"ALTER TABLE {table} ADD COLUMN speaker TEXT")
        if table == "episodes":
            self.db.execute(
                "UPDATE episodes SET speaker = "
                "(SELECT persona FROM sessions WHERE id = episodes.session_id)"
            )
            return
        self.db.execute(f"UPDATE {table} SET speaker = persona")

    def dislike_music(self, uri: str, artists: tuple[str, ...]) -> None:
        """Record distinct tracks per artist, independently of game/session."""
        with self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO music_dislikes VALUES (?, ?)",
                [(uri, artist.casefold()) for artist in artists] or [(uri, "")],
            )

    def music_dislikes(self) -> tuple[set[str], set[str]]:
        tracks = {row[0] for row in self.db.execute("SELECT DISTINCT video_id FROM music_dislikes")}
        artists = {
            row[0]
            for row in self.db.execute(
                "SELECT artist FROM music_dislikes WHERE artist != '' "
                "GROUP BY artist HAVING COUNT(DISTINCT video_id) >= 2"
            )
        }
        return tracks, artists

    def close(self) -> None:
        self.db.close()

    def start_session(self, game: str, persona: str, now: datetime | None = None) -> int:
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO sessions(game, persona, started_utc, speaker) VALUES (?, ?, ?, ?)",
                (game, persona, utc_text(now), persona),
            )
        return cursor.lastrowid

    def end_session(self, session_id: int, summary: str = "", now: datetime | None = None) -> None:
        with self.db:
            self.db.execute(
                "UPDATE sessions SET ended_utc = ?, summary = ? WHERE id = ?",
                (utc_text(now), summary, session_id),
            )

    def add_episode(
        self,
        session_id: int,
        game: str,
        kind: str,
        place_key: str,
        place: str,
        region: str,
        activity: str,
        text: str,
        salience: float,
        now: datetime | None = None,
        *,
        speaker: str | None = None,
        source: str = "game",
    ) -> int | None:
        if source not in {"player_observed", "game", "persona_claim"}:
            raise ValueError(f"Invalid memory source: {source}")
        if kind == "crew_moment" and source != "persona_claim":
            raise ValueError("Crew moments must be persona claims")
        with self.db:
            cursor = self.db.execute(
                """INSERT INTO episodes
                (session_id, game, ts_utc, kind, place_key, place, region, activity, text, salience,
                 speaker, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, kind, place_key) WHERE kind != 'crew_moment' DO UPDATE SET
                text = excluded.text, source = excluded.source, speaker = excluded.speaker,
                salience = excluded.salience, ts_utc = excluded.ts_utc,
                place = excluded.place, region = excluded.region, activity = excluded.activity
                WHERE episodes.source = 'persona_claim' AND excluded.source != 'persona_claim'
                RETURNING id
                """,
                (
                    session_id,
                    game,
                    utc_text(now),
                    kind,
                    place_key,
                    place,
                    region,
                    activity,
                    text,
                    salience,
                    speaker,
                    source,
                ),
            )
            row = cursor.fetchone()
            if kind == "crew_moment":
                self._prune_crew_moments(game)
        if row is None:
            return None
        self.revision += 1
        return row[0]

    def _prune_crew_moments(self, game: str) -> None:
        stale = "SELECT id FROM episodes WHERE game = ? AND kind = 'crew_moment' "
        stale += "ORDER BY ts_utc DESC, id DESC LIMIT -1 OFFSET ?"
        parameters = (game, CREW_MOMENT_CAP)
        self.db.execute(f"DELETE FROM recalls WHERE episode_id IN ({stale})", parameters)
        self.db.execute(f"DELETE FROM episodes WHERE id IN ({stale})", parameters)

    def crew_moments(self, game: str) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM episodes WHERE game = ? AND kind = 'crew_moment' "
                "AND source = 'persona_claim' ORDER BY ts_utc DESC, id DESC",
                (game,),
            )
        ]

    def moment_last_use(self, episode_id: int) -> datetime | None:
        row = self.db.execute(
            "SELECT MAX(ts_utc) FROM recalls WHERE episode_id = ?", (episode_id,)
        ).fetchone()
        if row[0]:
            return datetime.fromisoformat(row[0])
        return None

    def _episodes(
        self, game: str, session_id: int, condition: str, parameters: tuple, limit: int
    ) -> list[dict]:
        rows = self.db.execute(
            f"""SELECT * FROM episodes WHERE game = ? AND session_id < ? AND {condition}
            AND source != 'persona_claim'
            ORDER BY salience DESC, ts_utc DESC, id DESC LIMIT ?""",
            (game, session_id, *parameters, limit),
        )
        return [dict(row) for row in rows]

    def episodes_at_place(
        self, game: str, place_key: str, session_id: int, limit: int = 3, min_salience: float = 0
    ) -> list[dict]:
        return self._episodes(
            game, session_id, "place_key = ? AND salience >= ?", (place_key, min_salience), limit
        )

    def region_history(self, game: str, region: str, session_id: int, limit: int = 3) -> list[dict]:
        return self._episodes(game, session_id, "region = ?", (region,), limit)

    def search_terms(
        self, game: str, terms: list[str], session_id: int, limit: int = 3
    ) -> list[dict]:
        if not terms:
            return []
        expression = (
            "' ' || memory_words(place || ' ' || region || ' ' || activity || ' ' || text) || ' '"
        )
        condition = " OR ".join(f"instr({expression}, ?) > 0" for _ in terms)
        parameters = tuple(f" {words(term)} " for term in terms)
        return self._episodes(game, session_id, f"({condition})", parameters, limit)

    def known_terms(self, game: str) -> set[str]:
        rows = self.db.execute(
            "SELECT DISTINCT place, region, activity FROM episodes WHERE game = ? "
            "AND source != 'persona_claim'",
            (game,),
        )
        terms = set()
        for row in rows:
            terms.update((words(row["place"]), words(row["region"])))
            # The optional activity hook is plain language; recover named cargo/destination.
            match = re.fullmatch(r"hauling (?:.*? of )?(.+?) to (.+)", row["activity"], re.I)
            if match:
                terms.update(words(part) for part in match.groups())
        return terms - {""}

    def latest_summaries(self, game: str, session_id: int, limit: int = 2) -> list[dict]:
        rows = self.db.execute(
            """SELECT * FROM sessions WHERE game = ? AND id < ?
            AND summary IS NOT NULL AND summary != ''
            ORDER BY started_utc DESC, id DESC LIMIT ?""",
            (game, session_id, limit),
        )
        return [dict(row) for row in rows]

    def record_recall(self, episode_id: int, persona: str, now: datetime | None = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO recalls(episode_id, persona, ts_utc, speaker) VALUES (?, ?, ?, ?)",
                (episode_id, persona, utc_text(now), persona),
            )

    def last_recall(self, episode_id: int, persona: str) -> datetime | None:
        row = self.db.execute(
            "SELECT MAX(ts_utc) FROM recalls WHERE episode_id = ? AND persona = ?",
            (episode_id, persona),
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row[0] else None

    def latest_recall(self) -> datetime | None:
        row = self.db.execute("SELECT MAX(ts_utc) FROM recalls").fetchone()
        return datetime.fromisoformat(row[0]) if row[0] else None

    def crew_last_recall(self, episode_id: int, crew: tuple[str, ...]) -> datetime | None:
        placeholders = ", ".join("?" for _ in crew)
        row = self.db.execute(
            f"SELECT MAX(ts_utc) FROM recalls WHERE episode_id = ? AND persona IN ({placeholders})",
            (episode_id, *crew),
        ).fetchone()
        if row[0]:
            return datetime.fromisoformat(row[0])
        return None

    def record_flavor_use(self, game: str, item_id: str, now: datetime | None = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO flavor_uses(game, item_id, used_at) VALUES (?, ?, ?)",
                (game, item_id, utc_text(now)),
            )

    def last_flavor_use(self, game: str, item_id: str) -> datetime | None:
        row = self.db.execute(
            "SELECT MAX(used_at) FROM flavor_uses WHERE game = ? AND item_id = ?",
            (game, item_id),
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row[0] else None

    def latest_flavor_use(self) -> datetime | None:
        row = self.db.execute("SELECT MAX(used_at) FROM flavor_uses").fetchone()
        return datetime.fromisoformat(row[0]) if row[0] else None
