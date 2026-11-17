"""Versioned SQLite storage shared by all games and personas."""

import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .text import words

SCHEMA_VERSION = 2
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
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            version = self.db.execute("SELECT version FROM schema_version").fetchone()
            if version is not None and version[0] not in {1, SCHEMA_VERSION}:
                raise ValueError(f"Unsupported memory schema version: {version[0]}")
            with self.db:
                self.db.executescript(_SCHEMA)
                self.db.executescript(_FLAVOR_SCHEMA)
                if version is None:
                    self.db.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
                if version is not None and version[0] < SCHEMA_VERSION:
                    self.db.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self.db.close()

    def start_session(self, game: str, persona: str, now: datetime | None = None) -> int:
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO sessions(game, persona, started_utc) VALUES (?, ?, ?)",
                (game, persona, utc_text(now)),
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
    ) -> int | None:
        with self.db:
            cursor = self.db.execute(
                """INSERT OR IGNORE INTO episodes
                (session_id, game, ts_utc, kind, place_key, place, region, activity, text, salience)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                ),
            )
        if not cursor.rowcount:
            return None
        self.revision += 1
        return cursor.lastrowid

    def _episodes(
        self, game: str, session_id: int, condition: str, parameters: tuple, limit: int
    ) -> list[dict]:
        rows = self.db.execute(
            f"""SELECT * FROM episodes WHERE game = ? AND session_id < ? AND {condition}
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
            "SELECT DISTINCT place, region, activity FROM episodes WHERE game = ?", (game,)
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
                "INSERT INTO recalls(episode_id, persona, ts_utc) VALUES (?, ?, ?)",
                (episode_id, persona, utc_text(now)),
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
