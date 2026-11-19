"""Tests for music library paths, index, scanner, deduplication, enrichment, and query."""

import datetime
import sqlite3
import subprocess
from pathlib import Path

import httpx
import mutagen
import pytest
from familiar_music.library.enrich import Enricher
from familiar_music.library.index import LibraryIndex, primary_artist, store_artist_tags, tag_key
from familiar_music.library.paths import database_path, library_roots, xdg_music_dir
from familiar_music.library.query import expand
from familiar_music.library.scanner import Scanner


@pytest.fixture(autouse=True)
def isolated_data(monkeypatch, tmp_path):
    monkeypatch.setenv("FAMILIAR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


def _make_audio(
    path: Path,
    *,
    duration: float = 0.5,
    title: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    albumartist: str | None = None,
    date: str | None = None,
    genre: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ext = path.suffix.lower()

    encoding = {".flac": [], ".m4a": ["-c:a", "aac"]}.get(ext, ["-q:a", "9"])
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=44100:cl=mono",
        "-t",
        str(duration),
        *encoding,
        str(path),
    ]

    subprocess.run(cmd, check=True, capture_output=True)

    audio = mutagen.File(path, easy=True)
    assert audio is not None
    metadata = dict(
        title=title, artist=artist, album=album, albumartist=albumartist, date=date, genre=genre
    )
    for key, value in metadata.items():
        if value is None:
            continue
        audio[key] = value
    audio.save()


def test_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    db = database_path()
    assert db == (tmp_path / "state" / "familiar" / "music" / "library.sqlite").resolve()

    custom_db = database_path({"library_db": str(tmp_path / "custom.sqlite")})
    assert custom_db == (tmp_path / "custom.sqlite").resolve()

    roots = library_roots({"library_paths": [str(tmp_path / "music1"), str(tmp_path / "music2")]})
    assert roots == [(tmp_path / "music1").resolve(), (tmp_path / "music2").resolve()]

    xdg_music = xdg_music_dir()
    assert isinstance(xdg_music, Path)


def test_incremental_skip(tmp_path):
    root = tmp_path / "music"
    track_file = root / "Artist" / "Album" / "song.mp3"
    _make_audio(track_file, title="Song 1", artist="Artist 1")

    db_path = tmp_path / "lib.sqlite"
    index = LibraryIndex(db_path)
    scanner = Scanner(index)

    res1 = scanner.scan([root])
    assert res1["scanned"] == 1
    assert res1["added_or_updated"] == 1
    assert res1["skipped"] == 0

    res2 = scanner.scan([root])
    assert res2["scanned"] == 1
    assert res2["added_or_updated"] == 0
    assert res2["skipped"] == 1

    res3 = scanner.scan([root], full=True)
    assert res3["scanned"] == 1
    assert res3["added_or_updated"] == 1
    assert res3["skipped"] == 0


def test_rename_keeps_uuid(tmp_path):
    root = tmp_path / "music"
    old_file = root / "Artist" / "Album" / "song.mp3"
    _make_audio(old_file, title="Song 1", artist="Artist 1")

    db_path = tmp_path / "lib.sqlite"
    index = LibraryIndex(db_path)
    scanner = Scanner(index)

    scanner.scan([root])
    with index._connect() as conn:
        row = conn.execute(
            "SELECT uuid FROM tracks WHERE relpath = ?", ("Artist/Album/song.mp3",)
        ).fetchone()
        orig_uuid = row["uuid"]

    new_file = root / "Artist" / "Album" / "renamed_song.mp3"
    old_file.rename(new_file)

    # First scan without old file marks missing=1
    scanner.scan([root])

    with index._connect() as conn:
        new_row = conn.execute(
            "SELECT uuid FROM tracks WHERE relpath = ?", ("Artist/Album/renamed_song.mp3",)
        ).fetchone()
        assert new_row["uuid"] == orig_uuid


def test_missing_marking(tmp_path):
    root = tmp_path / "music"
    track_file = root / "Artist" / "Album" / "song.mp3"
    _make_audio(track_file, title="Song 1", artist="Artist 1")

    db_path = tmp_path / "lib.sqlite"
    index = LibraryIndex(db_path)
    scanner = Scanner(index)

    scanner.scan([root])
    with index._connect() as conn:
        orig_uuid = conn.execute("SELECT uuid FROM tracks").fetchone()["uuid"]

    assert index.by_uuid(orig_uuid) is not None

    track_file.unlink()
    scanner.scan([root])

    # Record still in tracks table but marked missing=1
    with index._connect() as conn:
        row = conn.execute(
            "SELECT uuid, missing FROM tracks WHERE uuid = ?", (orig_uuid,)
        ).fetchone()
        assert row["missing"] == 1

    # Query helpers never return missing tracks
    assert index.by_uuid(orig_uuid) is None
    assert index.stats()["missing"] == 1


def test_junk_skip_and_missing_tags(tmp_path):
    root = tmp_path / "music"
    junk_audio = root / "Band" / "Record" / "junk.mp3"
    _make_audio(
        junk_audio,
        title="Valid Title",
        artist="",
        genre="http://example.com/genres",
    )

    dot_file = root / "Band" / "Record" / "._ignored.mp3"
    _make_audio(dot_file, title="Dot Underscore")

    m4p_file = root / "Band" / "Record" / "protected.m4p"
    m4p_file.write_bytes(b"dummy")

    hidden_dir_audio = root / ".hidden" / "hidden.mp3"
    _make_audio(hidden_dir_audio, title="Hidden")

    db_path = tmp_path / "lib.sqlite"
    index = LibraryIndex(db_path)
    scanner = Scanner(index)
    scanner.scan([root])

    with index._connect() as conn:
        rows = conn.execute("SELECT title, artist, genre FROM tracks").fetchall()
        assert len(rows) == 1
        assert rows[0]["title"] == "Valid Title"
        # Artist inferred from parent-of-album directory: Band
        assert rows[0]["artist"] == "Band"
        # Junk URL genre was dropped
        assert rows[0]["genre"] is None


def test_dedupe_preference(tmp_path):
    root = tmp_path / "music"
    flac_file = root / "Artist" / "Album" / "song.flac"
    mp3_file = root / "Artist" / "Album" / "song (Remastered).mp3"

    _make_audio(flac_file, duration=1.0, title="Song", artist="Artist", genre="Rock")
    _make_audio(mp3_file, duration=1.2, title="Song (Remastered)", artist="Artist", genre="Rock")

    db_path = tmp_path / "lib.sqlite"
    index = LibraryIndex(db_path)
    scanner = Scanner(index)
    scanner.scan([root])

    with index._connect() as conn:
        flac_row = conn.execute(
            "SELECT uuid, lossless, dup_group FROM tracks WHERE codec = 'flac'"
        ).fetchone()
        mp3_row = conn.execute(
            "SELECT uuid, lossless, dup_group FROM tracks WHERE codec = 'mp3'"
        ).fetchone()

    # Flac is lossless, so it should be preferred
    assert flac_row["dup_group"] == flac_row["uuid"]
    assert mp3_row["dup_group"] == flac_row["uuid"]

    # Queries return only preferred
    assert index.by_uuid(flac_row["uuid"]) is not None
    assert index.by_uuid(mp3_row["uuid"]) is None

    tracks = index.tracks_by_artist("Artist")
    assert len(tracks) == 1
    assert tracks[0]["uuid"] == flac_row["uuid"]
    assert index.stats()["dup_groups"] == 1


def test_filter_search_accent_insensitivity(tmp_path):
    root = tmp_path / "music"
    song = root / "Cafe" / "Album" / "song.mp3"
    _make_audio(
        song,
        title="Eres",
        artist="Café Tacvba",
        album="Cuatro Caminos",
        date="2004",
        genre="Rock en Español",
    )

    db_path = tmp_path / "lib.sqlite"
    index = LibraryIndex(db_path)
    scanner = Scanner(index)
    scanner.scan([root])

    # Search substring casefold and accent-insensitive
    res1 = index.search("cafe")
    assert len(res1) == 1
    assert res1[0]["title"] == "Eres"

    res2 = index.search("caminos")
    assert len(res2) == 1

    res3 = index.search("ERES")
    assert len(res3) == 1

    # Artists matching
    artists = index.artists_matching("cafe")
    assert len(artists) == 1
    assert artists[0]["display"] == "Café Tacvba"
    assert "Café Tacvba" in [a["display"] for a in artists]

    # Filters
    filtered1 = index.filter(artist="cafe tacvba")
    assert len(filtered1) == 1

    filtered2 = index.filter(years=(2000, 2010))
    assert len(filtered2) == 1

    filtered3 = index.filter(years=(2005, 2010))
    assert len(filtered3) == 0

    filtered4 = index.filter(genres=["rock en espanol"])
    assert len(filtered4) == 1

    uuid = res1[0]["uuid"]
    filtered_exclude = index.filter(exclude=[uuid])
    assert len(filtered_exclude) == 0

    vocab = index.tag_vocabulary(limit=10)
    assert any(item["tag"] == "rock en español" for item in vocab)


def test_enrich_with_mocked_httpx_and_rate_limiting(tmp_path):
    db_path = tmp_path / "lib.sqlite"
    index = LibraryIndex(db_path)
    with index._connect() as conn:
        conn.execute(
            "INSERT INTO artists (name_norm, display) VALUES (?, ?)",
            ("queen", "Queen"),
        )

    current_time = [100.0]
    slept = [0.0]

    def mock_clock() -> float:
        return current_time[0]

    def mock_sleeper(seconds: float) -> None:
        slept[0] += seconds
        current_time[0] += seconds

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/ws/2/artist?" in url:
            data = {
                "artists": [
                    {
                        "id": "0383dadf-2a4e-4d10-a46a-e6e041da9180",
                        "name": "Queen",
                        "score": 100,
                    }
                ]
            }
            return httpx.Response(200, json=data)
        if "/ws/2/artist/0383dadf-2a4e-4d10-a46a-e6e041da9180" in url:
            data = {
                "id": "0383dadf-2a4e-4d10-a46a-e6e041da9180",
                "name": "Queen",
                "tags": [{"name": "rock", "count": 100}],
                "genres": [{"name": "classic rock", "count": 80}],
            }
            return httpx.Response(200, json=data)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    enricher = Enricher(
        index,
        client=client,
        clock=mock_clock,
        sleeper=mock_sleeper,
    )

    res = enricher.enrich_all()
    assert res["enriched"] == 1
    assert res["failed"] == 0

    # Verify rate limiter slept between the search and details requests
    assert slept[0] >= 1.0

    with index._connect() as conn:
        row = conn.execute("SELECT * FROM artists WHERE name_norm = 'queen'").fetchone()
        assert row["mbid"] == "0383dadf-2a4e-4d10-a46a-e6e041da9180"
        tags = dict(conn.execute("SELECT tag, count FROM artist_tags WHERE name_norm = 'queen'"))
        assert tags["rock"] == 100
        assert tags["classicrock"] == 80
        assert row["enriched_at"] is not None
        assert row["enrich_error"] is None

    # Resuming: already enriched artist is not re-queried
    res2 = enricher.enrich_all()
    assert res2["processed"] == 0


def test_synonym_expand():
    tags, years = expand("eighties")
    assert "synthpop" in tags
    assert years == (1980, 1989)

    tags, years = expand("mellow")
    assert "ambient" in tags
    assert years is None

    tags, years = expand("unknown_term")
    assert tags == []
    assert years is None

    tags, years = expand("")
    assert tags == []
    assert years is None


@pytest.mark.parametrize(
    "credit", ["feat. Guest", "ft. Guest", "featuring Guest", "(with Guest)", "(feat. Guest)"]
)
def test_primary_artist(credit):
    assert primary_artist(f"Beyoncé {credit}") == "Beyoncé"
    assert primary_artist("Simon & Garfunkel") == "Simon & Garfunkel"
    assert primary_artist("Peter and Gordon") == "Peter and Gordon"


def test_normalized_tag_tables_and_featured_credits(tmp_path):
    root = tmp_path / "music"
    path = root / "song.mp3"
    _make_audio(
        path,
        title="Song",
        artist="Beyoncé feat. Guest",
        albumartist="The Album Artist",
        genre="J-Pop; Hip Hop, Disco / Blues/Other",
    )
    index = LibraryIndex(tmp_path / "lib.sqlite")
    scanner = Scanner(index)
    scanner.scan([root])
    assert len(index.tracks_by_artist("Beyonce")) == 1
    assert len(index.tracks_by_artist("The Album Artist")) == 1
    assert index.artists_matching("beyonce")[0]["display"] == "Beyoncé"
    assert not index.artists_matching("guest")
    for tag in ["jpop", "J-Pop", "hiphop", "HIP HÓP", "disco", "blues"]:
        assert len(index.filter(genres=[tag])) == 1
    assert not index.filter(genres=["pop"])
    with index._connect() as conn:
        assert conn.execute("SELECT artist_norm FROM tracks").fetchone()[0] == "beyonce"
        assert set(r[0] for r in conn.execute("SELECT tag FROM track_genres")) == {
            "jpop",
            "hiphop",
            "disco",
            "blues",
            "other",
        }
        store_artist_tags(conn, "beyonce", {"Synth-Pop": 3, "synthpop": 5})
    assert len(index.filter(genres=["synth pop"])) == 1
    assert tag_key("Híp-Hop") == "hiphop"
    assert {"tag": "synth-pop", "count": 5} in index.tag_vocabulary()
    _make_audio(path, title="Song", artist="Beyoncé", genre="Country")
    scanner.scan([root], full=True)
    assert not index.filter(genres=["jpop"])
    assert len(index.filter(genres=["country"])) == 1


def test_unversioned_migration_preserves_enrichment_and_rescans(tmp_path):
    db = tmp_path / "lib.sqlite"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            "CREATE TABLE tracks (uuid TEXT); INSERT INTO tracks VALUES ('old'); "
            "CREATE TABLE artists (name_norm TEXT, display TEXT, mbid TEXT, "
            "tags_json TEXT, enriched_at TEXT); "
            "INSERT INTO artists VALUES ('queen', 'Queen', 'mbid', '{\"Rock\": 4}', '2026-11-19');"
        )
    index = LibraryIndex(db)
    with index._connect() as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 0
        assert conn.execute("SELECT count FROM artist_tags WHERE tag = 'rock'").fetchone()[0] == 4
    root = tmp_path / "music"
    _make_audio(root / "song.mp3", artist="Queen")
    assert Scanner(index).scan([root])["added_or_updated"] == 1
    assert len(index.filter(genres=["rock"])) == 1
    assert LibraryIndex(db).stats()["artists_enriched"] == 1


@pytest.mark.parametrize("recover", [True, False])
@pytest.mark.parametrize("throttle_details", [True, False])
def test_enrichment_503_backoff(tmp_path, recover, throttle_details):
    index = LibraryIndex(tmp_path / "lib.sqlite")
    with index._connect() as conn:
        conn.execute("INSERT INTO artists (name_norm, display) VALUES ('queen', 'Queen')")
    requests = []
    sleeps = []
    now = [100.0]

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    def handler(request):
        requests.append(request)
        search = request.url.path.endswith("/artist")
        if throttle_details and search:
            return httpx.Response(200, json={"artists": [{"id": "mbid", "score": 100}]})
        throttled_request = 2 if throttle_details else 1
        if len(requests) == throttled_request or not recover:
            return httpx.Response(503)
        if search:
            return httpx.Response(200, json={"artists": [{"id": "mbid", "score": 100}]})
        return httpx.Response(200, json={"tags": [{"name": "J-Pop", "count": 5}]})

    enricher = Enricher(
        index,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: now[0],
        sleeper=sleep,
    )
    result = enricher.enrich_all()
    assert 5.0 in sleeps
    assert len(requests) == (3 if recover or throttle_details else 2)
    assert result["enriched"] == int(recover)
    with index._connect() as conn:
        row = conn.execute("SELECT * FROM artists").fetchone()
        assert row["enrich_error_at"] is None
        assert row["enrich_error"] is None
        assert enricher._should_process(row, datetime.datetime.now(datetime.UTC)) == (not recover)


def test_enrichment_error_timestamp_column(tmp_path):
    index = LibraryIndex(tmp_path / "lib.sqlite")
    with index._connect() as conn:
        conn.execute("INSERT INTO artists (name_norm, display) VALUES ('queen', 'Queen')")
    enricher = Enricher(
        index, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    )
    assert enricher.enrich_all()["failed"] == 1
    with index._connect() as conn:
        row = conn.execute("SELECT * FROM artists").fetchone()
        assert row["enrich_error_at"]
        assert not enricher._should_process(row, datetime.datetime.now(datetime.UTC))
        conn.execute("UPDATE artists SET enrich_error_at = '2000-01-01T00:00:00+00:00'")
    assert enricher.enrich_all()["processed"] == 1


@pytest.mark.parametrize(
    "query, years",
    [
        ("70s disco classics", (1970, 1979)),
        ("1970s disco", (1970, 1979)),
        ("'80s disco", (1980, 1989)),
        ("80s disco", (1980, 1989)),
        ("1985 disco", (1985, 1985)),
        ("eighties 1985 disco", (1985, 1985)),
    ],
)
def test_tokenized_expand(query, years):
    tags, actual_years = expand(query)
    assert "disco" in tags
    assert "classics" not in tags
    assert actual_years == years


def test_expand_phrases_and_vocabulary():
    tags, years = expand("rock en español classics cumbia ska")
    assert tags == ["rock en español", "latin rock", "cumbia", "ska"]
    assert years is None
    assert expand("unknown shoegaze", {"shoegaze"}) == (["shoegaze"], None)
    assert expand("dream pop classics", {"dreampop"}) == (["dreampop"], None)
    assert expand("unknown") == ([], None)
    for term in [
        "rock latino",
        "latin",
        "j-pop/anime",
        "city pop",
        "metal",
        "country",
        "soundtrack/score",
    ]:
        assert expand(term)[0]


def test_walk_error_does_not_mark_unseen_missing(tmp_path, monkeypatch):
    root = tmp_path / "music"
    _make_audio(root / "song.mp3", artist="Artist")
    index = LibraryIndex(tmp_path / "lib.sqlite")
    scanner = Scanner(index)
    scanner.scan([root])

    def broken_walk(root, onerror):
        onerror(PermissionError("Denied"))
        return iter(())

    monkeypatch.setattr("familiar_music.library.scanner.os.walk", broken_walk)
    scanner.scan([root])
    assert len(index.tracks_by_artist("Artist")) == 1
