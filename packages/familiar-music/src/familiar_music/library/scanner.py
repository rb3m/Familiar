"""Filesystem scanner for audio tracks and tags."""

import hashlib
import logging
import os
import re
import unicodedata
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import mutagen
from familiar.interfaces import load_data

from .index import LibraryIndex, accent_fold, primary_artist, tag_key

logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r"https?://|www\.|\.com\b|\.org\b|\.net\b", re.IGNORECASE)
REMASTER_SUFFIX_PATTERN = re.compile(
    r"\s*[\(\[\-].*?\b(?:remaster(?:ed)?|deluxe|bonus|anniversary|edition|version|mono|stereo|live|mix)\b.*?(?:[\)\]]|$)",
    re.IGNORECASE,
)
YEAR_PATTERN = re.compile(r"\b(19\d\d|20\d\d)\b")


def normalize_artist_title(artist: str, title: str) -> str:
    """Normalise artist and title for deduplication: casefold, strip accents/punct/remaster."""
    cleaned_title = REMASTER_SUFFIX_PATTERN.sub("", title)
    combined = f"{artist} {cleaned_title}"
    nfkd = unicodedata.normalize("NFKD", combined)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c)).casefold()
    clean = re.sub(r"[^\w\s]", " ", stripped)
    return " ".join(clean.split())


def compute_header_hash(path: Path) -> str:
    """Compute sha1 hash of the first 64 KiB of a file."""
    try:
        with path.open("rb") as stream:
            chunk = stream.read(65536)
            return hashlib.sha1(chunk).hexdigest()
    except OSError:
        return ""


def _extract_year(raw_date: Any) -> int | None:
    if not raw_date:
        return None
    val = raw_date[0] if isinstance(raw_date, list) else str(raw_date)
    match = YEAR_PATTERN.search(str(val))
    if match:
        return int(match.group(1))
    return None


def _clean_genre(raw_genre: Any, junk_genres: set[str]) -> str | None:
    if not raw_genre:
        return None
    val = raw_genre[0] if isinstance(raw_genre, list) else str(raw_genre)
    text = str(val).strip()
    if not text:
        return None
    if text.casefold() in junk_genres:
        return None
    if URL_PATTERN.search(text):
        return None
    return text


def _extract_tag_value(tags: Any, key: str) -> str | None:
    if not tags:
        return None
    val = tags.get(key)
    if not val:
        return None
    if isinstance(val, (list, tuple)):
        return str(val[0]).strip() if val else None
    return str(val).strip()


def _detect_artist(file_path: Path, root: Path, tag_artist: str | None) -> str:
    if tag_artist and tag_artist.strip():
        return tag_artist.strip()
    try:
        parts = file_path.relative_to(root).parts
    except ValueError:
        return "Unknown Artist"
    if len(parts) >= 3:
        return parts[-3]
    if len(parts) == 2:
        return parts[0]
    return "Unknown Artist"


def _detect_codec_and_lossless(ext: str, audio: Any) -> tuple[str, int]:
    clean_ext = ext.lower().removeprefix(".")
    codecs = {"flac": ("flac", 1), "mp3": ("mp3", 0), "ogg": ("vorbis", 0), "opus": ("opus", 0)}
    if clean_ext not in {"m4a", "aac"}:
        return codecs.get(clean_ext, (clean_ext, 0))
    info_codec = str(getattr(audio.info, "codec", "")).lower()
    if "alac" in info_codec or getattr(audio.info, "bits_per_sample", None):
        return "alac", 1
    return "aac", 0


def _cluster_by_duration(tracks: list[dict], dup_duration_s: float) -> list[list[dict]]:
    sorted_tracks = sorted(tracks, key=lambda t: t["duration_s"])
    clusters: list[list[dict]] = []
    current_cluster: list[dict] = []

    for track in sorted_tracks:
        if not current_cluster:
            current_cluster.append(track)
            continue
        if abs(track["duration_s"] - current_cluster[-1]["duration_s"]) <= dup_duration_s:
            current_cluster.append(track)
            continue
        clusters.append(current_cluster)
        current_cluster = [track]

    if current_cluster:
        clusters.append(current_cluster)

    return clusters


def deduplicate(index: LibraryIndex, dup_duration_s: float = 3.0) -> int:
    """Group duplicate tracks and set preferred member. Returns dup group count."""
    with index._connect() as conn:
        rows = conn.execute(
            """
            SELECT uuid, artist, title, duration_s, lossless, bitrate, mtime
            FROM tracks WHERE missing = 0
            """
        ).fetchall()

        groups: dict[str, list[dict]] = {}
        for row in rows:
            d = dict(row)
            key = normalize_artist_title(d["artist"], d["title"])
            groups.setdefault(key, []).append(d)

        updates: list[tuple[str | None, str]] = []
        dup_group_count = 0

        for tracks in groups.values():
            group_updates, count = _duplicate_updates(tracks, dup_duration_s)
            updates.extend(group_updates)
            dup_group_count += count

        conn.executemany("UPDATE tracks SET dup_group = ? WHERE uuid = ?", updates)
        return dup_group_count


def _duplicate_updates(tracks: list[dict], tolerance: float) -> tuple[list[tuple], int]:
    updates = []
    count = 0
    for cluster in _cluster_by_duration(tracks, tolerance):
        preferred_uuid = _preferred_uuid(cluster)
        updates.extend((preferred_uuid, track["uuid"]) for track in cluster)
        count += int(preferred_uuid is not None)
    return updates, count


def _preferred_uuid(cluster: list[dict]) -> str | None:
    if len(cluster) < 2:
        return None
    preferred = max(cluster, key=lambda t: (t["lossless"], t["bitrate"] or 0, t["mtime"]))
    return preferred["uuid"]


class Scanner:
    """Walks music roots, reads audio tags with mutagen, and populates index."""

    def __init__(self, index: LibraryIndex) -> None:
        self.index = index
        data = load_data("familiar_music", "library").value
        self.extensions = {ext.lower().removeprefix(".") for ext in data.get("extensions", [])}
        self.junk_genres = {str(g).casefold() for g in data.get("junk_genres", [])}
        self.featured_patterns = data.get("featured_artist_patterns", [])
        self.dup_duration_s = float(data.get("dup_duration_s", 3.0))

    def _read_audio(self, path: Path) -> Any:
        try:
            return mutagen.File(path, easy=True)
        except Exception:
            return None

    def scan(
        self,
        roots: list[Path],
        *,
        full: bool = False,
        progress_cb: Callable[[int, int | None, str], None] | None = None,
    ) -> dict[str, int]:
        """Scan roots incrementally; only complete walks mark unseen files missing."""
        counts = dict(scanned=0, added_or_updated=0, skipped=0, unreadable=0)
        for root in roots:
            self._scan_root(root.expanduser().resolve(), full, progress_cb, counts)
        counts["dup_groups"] = deduplicate(self.index, self.dup_duration_s)
        return counts

    def _scan_root(self, root: Path, full: bool, progress_cb, counts: dict) -> None:
        if not root.is_dir():
            return
        errors = []
        with self.index._connect() as conn:
            files = list(self._walk_files(root, errors))
            if not errors:
                self._mark_missing(conn, root, files)
            for path in files:
                self._scan_file(conn, root, path, full, progress_cb, counts)

    def _walk_files(self, root: Path, errors: list):
        for dirpath, dirnames, filenames in os.walk(root, onerror=errors.append):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            yield from (
                Path(dirpath) / name
                for name in filenames
                if not name.startswith("._")
                and Path(name).suffix.lower().removeprefix(".") in self.extensions
                and Path(name).suffix.lower() != ".m4p"
            )

    def _mark_missing(self, conn, root: Path, files: list[Path]) -> None:
        seen = {str(p.relative_to(root)) for p in files}
        rows = conn.execute(
            "SELECT uuid, relpath FROM tracks WHERE root = ? AND missing = 0", (str(root),)
        )
        missing = [(r["uuid"],) for r in rows if r["relpath"] not in seen]
        conn.executemany("UPDATE tracks SET missing = 1 WHERE uuid = ?", missing)

    def _scan_file(
        self, conn, root: Path, path: Path, full: bool, progress_cb, counts: dict
    ) -> None:
        relpath = str(path.relative_to(root))
        counts["scanned"] += 1
        if progress_cb:
            progress_cb(counts["scanned"], None, relpath)
        try:
            stat = path.stat()
        except OSError as exc:
            logger.warning("Unreadable file stat %s: %s", path, exc)
            counts["unreadable"] += 1
            return
        existing = conn.execute(
            "SELECT uuid, size, mtime, missing FROM tracks WHERE root = ? AND relpath = ?",
            (str(root), relpath),
        ).fetchone()
        if (
            not full
            and existing
            and existing["size"] == stat.st_size
            and abs(existing["mtime"] - stat.st_mtime) < 0.001
            and not existing["missing"]
        ):
            counts["skipped"] += 1
            return
        audio = self._read_audio(path)
        if audio is None or audio.info is None:
            logger.warning("Unreadable audio file %s", path)
            counts["unreadable"] += 1
            return
        header_hash = compute_header_hash(path)
        track_uuid = self._resolve_uuid(conn, existing, stat.st_size, header_hash)
        self._upsert(conn, root, path, stat, header_hash, track_uuid, audio)
        counts["added_or_updated"] += 1

    def _resolve_uuid(self, conn, existing, size: int, header_hash: str) -> str:
        if existing:
            return existing["uuid"]
        missing = conn.execute(
            "SELECT uuid FROM tracks WHERE size = ? AND header_hash = ? AND missing = 1 LIMIT 1",
            (size, header_hash),
        ).fetchone()
        if missing:
            return missing["uuid"]
        return str(uuid.uuid4())

    def _upsert(
        self, conn, root: Path, path: Path, stat, header_hash: str, track_uuid: str, audio
    ) -> None:
        tags = audio.tags
        title = _extract_tag_value(tags, "title") or path.stem
        artist = _detect_artist(path, root, _extract_tag_value(tags, "artist"))
        display = primary_artist(artist, self.featured_patterns)
        artist_norm = accent_fold(display)
        album_artist = _extract_tag_value(tags, "albumartist")
        album = _extract_tag_value(tags, "album")
        genre = _clean_genre(_extract_tag_value(tags, "genre"), self.junk_genres)
        codec, lossless = _detect_codec_and_lossless(path.suffix, audio)
        bitrate = getattr(audio.info, "bitrate", None)
        values = dict(
            uuid=track_uuid,
            root=str(root),
            relpath=str(path.relative_to(root)),
            size=stat.st_size,
            mtime=stat.st_mtime,
            header_hash=header_hash,
            title=title,
            artist=artist,
            artist_norm=artist_norm,
            album_artist=album_artist,
            album_artist_norm=accent_fold(
                primary_artist(album_artist or "", self.featured_patterns)
            ),
            album=album,
            year=_extract_year(tags.get("date") if tags else None),
            genre=genre,
            duration_s=float(getattr(audio.info, "length", 0.0)),
            codec=codec,
            bitrate=int(bitrate) if bitrate is not None else None,
            lossless=lossless,
            missing=0,
            search_text=accent_fold(f"{artist}\n{title}\n{album or ''}"),
        )
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        updates = ", ".join(f"{k} = excluded.{k}" for k in values if k != "uuid")
        conn.execute(
            f"INSERT INTO tracks ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(uuid) DO UPDATE SET {updates}",
            tuple(values.values()),
        )
        conn.execute(
            "INSERT OR IGNORE INTO artists (name_norm, display) VALUES (?, ?)",
            (artist_norm, display),
        )
        conn.execute("DELETE FROM track_genres WHERE uuid = ?", (track_uuid,))
        for chunk in re.split(r"[;,/]", genre or ""):
            self._store_genre(conn, track_uuid, chunk.strip())

    def _store_genre(self, conn, track_uuid: str, display: str) -> None:
        key = tag_key(display)
        if not key:
            return
        conn.execute("INSERT OR IGNORE INTO tag_display VALUES (?, ?)", (key, display.casefold()))
        conn.execute("INSERT OR IGNORE INTO track_genres VALUES (?, ?)", (track_uuid, key))
