"""MusicBrainz artist tag and genre enrichment."""

import datetime
import logging
import re
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
from familiar.interfaces import load_data

from .index import LibraryIndex, primary_artist, store_artist_tags, tag_key

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
        self.retry_transient_hours = float(mb_conf.get("retry_transient_hours", 6))
        self.collaboration_separators = mb_conf.get("collaboration_separators", [])
        self.tag_stoplist = {tag_key(tag) for tag in mb_conf.get("tag_stoplist", [])}
        self.search_limit = int(mb_conf.get("search_limit", 3))

        rate_limit_s = float(mb_conf.get("rate_limit_s", 1.0))
        self.limiter = RateLimiter(rate_limit_s, self.clock, self.sleeper)

        self.client = client or httpx.Client(
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            timeout=15.0,
        )

    def _should_process(
        self,
        row: Any,
        now_dt: datetime.datetime,
        *,
        retry_failed: bool = False,
        refresh: bool = False,
    ) -> bool:
        if refresh:
            return True
        error = row["enrich_error"]
        if error and retry_failed:
            return True
        if row["enriched_at"] and not error:
            return False
        if not error or not row["enrich_error_at"]:
            return True
        err_dt = datetime.datetime.fromisoformat(row["enrich_error_at"])
        if err_dt.tzinfo is None:
            err_dt = err_dt.replace(tzinfo=datetime.UTC)
        retry_seconds = self.retry_error_days * 86400
        if error.startswith("transient:"):
            retry_seconds = self.retry_transient_hours * 3600
        return (now_dt - err_dt).total_seconds() >= retry_seconds

    @staticmethod
    def _name_queries(name: str) -> tuple[str, str]:
        escaped = re.sub(r'([+\-!(){}\[\]^"~*?:\\/&|])', r"\\\1", name)
        phrase = re.sub(r'(["\\])', r"\\\1", name)
        return f'artist:"{phrase}"', escaped

    def _search_queries(self, display: str) -> Iterator[str]:
        name = primary_artist(display)
        yield from self._name_queries(name)
        if not self.collaboration_separators:
            return
        pattern = "|".join(re.escape(s) for s in self.collaboration_separators)
        first = re.split(pattern, name, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        if first and first != name:
            yield from self._name_queries(first)

    def _find_artist(self, display: str) -> dict | None:
        for query in self._search_queries(display):
            result = self._fetch_artist_search(query)
            artists = result.get("artists", [])
            best = max(artists, key=lambda a: int(a.get("score", 0)), default={})
            if int(best.get("score", 0)) >= self.match_threshold:
                return best
        return None

    def _fetch_artist_search(self, query: str) -> dict:
        self.limiter.wait()
        url = f"{self.endpoint}/artist"
        params = {"query": query, "fmt": "json", "limit": self.search_limit}
        return self._request(url, params)

    def _fetch_artist_details(self, mbid: str) -> dict:
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
        body = resp.json()
        if "error" in body:
            raise ValueError(f"MusicBrainz error: {body['error']}")
        return body

    def enrich_artist(self, name_norm: str, display: str) -> tuple[bool, str | None]:
        """Perform lookup for a single artist and commit result. Returns (success, error)."""
        now_str = datetime.datetime.now(datetime.UTC).isoformat()
        try:
            best = self._find_artist(display)
            if best is None:
                err = "No search candidate above match threshold"
                self._record_error(name_norm, err, kind="not_found")
                return False, err

            mbid = best.get("id")
            if not mbid:
                raise ValueError("Missing mbid in search result")

            details = self._fetch_artist_details(mbid)
            tags_map = self._artist_tags(details)

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

        except Exception as exc:
            err = str(exc)
            logger.warning("Error enriching artist %s: %s", display, exc)
            self._record_error(name_norm, err, kind="transient")
            return False, err

    def _artist_tags(self, details: dict) -> dict[str, int]:
        genres = details.get("genres", [])
        items = genres or details.get("tags", [])
        tags: dict[str, int] = {}
        for item in items:
            name = str(item.get("name", "")).strip()
            if not name or (not genres and tag_key(name) in self.tag_stoplist):
                continue
            tags[name] = max(tags.get(name, 0), int(item.get("count", 1)))
        return tags

    def _record_error(self, name_norm: str, error_msg: str, *, kind: str) -> None:
        with self.index._connect() as conn:
            conn.execute(
                "UPDATE artists SET enrich_error = ?, enrich_error_at = ? WHERE name_norm = ?",
                (
                    f"{kind}: {error_msg}",
                    datetime.datetime.now(datetime.UTC).isoformat(),
                    name_norm,
                ),
            )

    def enrich_all(
        self,
        *,
        limit: int | None = None,
        retry_failed: bool = False,
        refresh: bool = False,
        progress_cb: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, int]:
        """Enrich eligible artists, optionally overriding retry delays and prior success."""
        now_dt = datetime.datetime.now(datetime.UTC)
        with self.index._connect() as conn:
            rows = conn.execute("SELECT * FROM artists ORDER BY display").fetchall()

        candidates = [
            r
            for r in rows
            if self._should_process(r, now_dt, retry_failed=retry_failed, refresh=refresh)
        ]
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
