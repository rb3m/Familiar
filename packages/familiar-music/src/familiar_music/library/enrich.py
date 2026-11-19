"""MusicBrainz artist tag and genre enrichment."""

import datetime
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx
from familiar.interfaces import load_data

from .index import LibraryIndex, primary_artist, store_artist_tags

logger = logging.getLogger(__name__)


class RateLimiter:
    """Enforce minimum interval between requests."""

    def __init__(
        self,
        min_interval_s: float,
        clock: Callable[[], float],
        sleeper: Callable[[float], None],
    ) -> None:
        self.min_interval_s = max(float(min_interval_s), 1.0)
        self.clock = clock
        self.sleeper = sleeper
        self.last_request_time: float = 0.0

    def wait(self) -> None:
        now = self.clock()
        elapsed = now - self.last_request_time
        if elapsed < self.min_interval_s:
            self.sleeper(self.min_interval_s - elapsed)
        self.last_request_time = self.clock()


class Enricher:
    """Queries MusicBrainz to enrich artist records with MBID and tags."""

    def __init__(
        self,
        index: LibraryIndex,
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.index = index
        self.clock = clock
        self.sleeper = sleeper

        data = load_data("familiar_music", "library").value
        mb_conf = data.get("musicbrainz", {})
        self.endpoint = mb_conf.get("endpoint", "https://musicbrainz.org/ws/2").rstrip("/")
        self.user_agent = mb_conf.get(
            "user_agent", "Familiar/0.1 ( https://github.com/rb3m/Familiar )"
        )
        self.match_threshold = int(mb_conf.get("match_threshold", 90))
        self.retry_error_days = float(mb_conf.get("retry_error_days", 7.0))
        self.search_limit = int(mb_conf.get("search_limit", 3))

        rate_limit_s = float(mb_conf.get("rate_limit_s", 1.0))
        self.limiter = RateLimiter(rate_limit_s, self.clock, self.sleeper)

        self.client = client or httpx.Client(
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            timeout=15.0,
        )

    def _should_process(self, row: Any, now_dt: datetime.datetime) -> bool:
        if row["enriched_at"]:
            return False
        error = row["enrich_error"]
        if not error:
            return True
        err_dt = (
            datetime.datetime.fromisoformat(row["enrich_error_at"])
            if row["enrich_error_at"]
            else None
        )
        if not err_dt:
            return True
        if err_dt.tzinfo is None:
            err_dt = err_dt.replace(tzinfo=datetime.UTC)
        age_seconds = (now_dt - err_dt).total_seconds()
        return age_seconds >= self.retry_error_days * 86400

    def _fetch_artist_search(self, artist_name: str) -> dict | None:
        self.limiter.wait()
        escaped_name = artist_name.replace('"', '\\"')
        url = f"{self.endpoint}/artist"
        params = {"query": f'artist:"{escaped_name}"', "fmt": "json", "limit": self.search_limit}
        return self._request(url, params)

    def _fetch_artist_details(self, mbid: str) -> dict | None:
        self.limiter.wait()
        url = f"{self.endpoint}/artist/{mbid}"
        params = {"inc": "tags+genres", "fmt": "json"}
        return self._request(url, params)

    def _request(self, url: str, params: dict) -> dict:
        resp = self.client.get(url, params=params)
        if resp.status_code == 503:
            self.sleeper(5.0)
            self.limiter.wait()
            resp = self.client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    def enrich_artist(self, name_norm: str, display: str) -> tuple[bool, str | None]:
        """Perform lookup for a single artist and commit result. Returns (success, error)."""
        now_str = datetime.datetime.now(datetime.UTC).isoformat()
        try:
            search_res = self._fetch_artist_search(primary_artist(display))
            artists = search_res.get("artists", []) if search_res else []
            if not artists:
                err = "No search results"
                self._record_error(name_norm, err)
                return False, err

            best = max(artists, key=lambda a: int(a.get("score", 0)))
            score = int(best.get("score", 0))
            if score < self.match_threshold:
                err = f"Best match score {score} < {self.match_threshold}"
                self._record_error(name_norm, err)
                return False, err

            mbid = best.get("id")
            if not mbid:
                err = "Missing mbid in search result"
                self._record_error(name_norm, err)
                return False, err

            details = self._fetch_artist_details(mbid)
            tags_map: dict[str, int] = {}
            for item in (details or {}).get("tags", []):
                t_name = str(item.get("name", "")).strip()
                t_count = int(item.get("count", 1))
                if t_name:
                    tags_map[t_name] = max(tags_map.get(t_name, 0), t_count)

            for item in (details or {}).get("genres", []):
                g_name = str(item.get("name", "")).strip()
                g_count = int(item.get("count", 1))
                if g_name:
                    tags_map[g_name] = max(tags_map.get(g_name, 0), g_count)

            with self.index._connect() as conn:
                conn.execute(
                    """
                    UPDATE artists
                    SET mbid = ?, enriched_at = ?, enrich_error = NULL, enrich_error_at = NULL
                    WHERE name_norm = ?
                    """,
                    (mbid, now_str, name_norm),
                )
                store_artist_tags(conn, name_norm, tags_map)
            return True, None

        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 503:
                logger.warning("MusicBrainz remains throttled for %s", display)
                return False, str(exc)
            self._record_error(name_norm, str(exc))
            return False, str(exc)
        except Exception as exc:
            err = str(exc)
            logger.warning("Error enriching artist %s: %s", display, exc)
            self._record_error(name_norm, err)
            return False, err

    def _record_error(self, name_norm: str, error_msg: str) -> None:
        with self.index._connect() as conn:
            conn.execute(
                "UPDATE artists SET enrich_error = ?, enrich_error_at = ? WHERE name_norm = ?",
                (error_msg, datetime.datetime.now(datetime.UTC).isoformat(), name_norm),
            )

    def enrich_all(
        self,
        *,
        limit: int | None = None,
        progress_cb: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, int]:
        """Enrich pending artists up to limit."""
        now_dt = datetime.datetime.now(datetime.UTC)
        with self.index._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM artists WHERE enriched_at IS NULL ORDER BY display"
            ).fetchall()

        candidates = [r for r in rows if self._should_process(r, now_dt)]
        if limit is not None and limit >= 0:
            candidates = candidates[:limit]

        total = len(candidates)
        enriched = 0
        failed = 0

        for i, row in enumerate(candidates, start=1):
            display = row["display"]
            name_norm = row["name_norm"]
            if progress_cb:
                progress_cb(i, total, display)

            success, _ = self.enrich_artist(name_norm, display)
            if success:
                enriched += 1
                continue
            failed += 1

        return {
            "processed": len(candidates),
            "enriched": enriched,
            "failed": failed,
        }
