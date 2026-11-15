"""Curation tests use local files, fake researchers and mocked Wikipedia only."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from familiar import cli
from familiar.config import AppConfig
from familiar.games.scs import curation
from familiar.interfaces import ConfigError


@pytest.fixture
def entry() -> dict:
    return {
        "id": "test-place",
        "name": "Test Place",
        "state": "TX",
        "lat": 30.0,
        "lon": -100.0,
        "radius_km": 25,
        "kind": "historic",
        "facts": ["It opened in 1900.", "It is the largest."],
        "source": "https://en.wikipedia.org/wiki/test_place#History",
    }


class FakeLLM:
    def __init__(self, reply: str = '[{"n": 1, "verdict": "supported"}]') -> None:
        self.reply = reply
        self.calls = 0
        self.closed = False

    async def stream(self, messages, system, tools):
        assert "ARTICLE:" in str(messages[0])
        self.calls += 1
        yield SimpleNamespace(text=self.reply)

    async def aclose(self) -> None:
        self.closed = True


async def no_sleep(seconds: float) -> None:
    pass


def test_owned_states(tmp_path: Path) -> None:
    for name in (
        "dlc_tx.scs",
        "dlc_NM.scs",
        "dlc_arizona.scs",
        "dlc_nevada.scs",
        "dlc_goodyear.scs",
        "dlc_unknown.scs",
        "dlc_or.zip",
    ):
        (tmp_path / name).touch()
    (tmp_path / "dlc_wa.scs").mkdir()
    assert curation.owned_states(tmp_path) == {"CA", "TX", "NM", "AZ", "NV"}
    with pytest.raises(ConfigError):
        curation.owned_states(tmp_path / "missing")
    with pytest.raises(ConfigError):
        curation.owned_states(tmp_path / "dlc_tx.scs")


def test_pack_states(entry: dict) -> None:
    assert curation.pack_states([entry, {**entry, "state": "CA"}]) == {"TX", "CA"}
    assert curation.pack_states([SimpleNamespace(state="TX")]) == {"TX"}


def test_coordinates_normalization_redirects_and_statuses(entry: dict) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["User-Agent"] == curation.HEADERS["User-Agent"]
        assert request.url.params["colimit"] == "max"
        return httpx.Response(
            200,
            json={
                "query": {
                    "normalized": [{"from": "test place", "to": "Test place"}],
                    "redirects": [{"from": "Test place", "to": "Final"}],
                    "pages": {
                        "1": {"title": "Final", "coordinates": [{"lat": 30, "lon": -100}]},
                        "2": {"title": "No coords"},
                        "-1": {"title": "Missing", "missing": ""},
                    },
                }
            },
        )

    entries = [
        entry,
        {**entry, "id": "off", "lat": 0},
        {**entry, "id": "none", "source": "https://en.wikipedia.org/wiki/No_coords"},
        {**entry, "id": "missing", "source": "https://en.wikipedia.org/wiki/Missing"},
        {**entry, "id": "other", "source": "https://example.com"},
    ]
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = curation.verify_coordinates(entries, client)
    assert result[entry["id"]] == "OK"
    assert result["off"].startswith("OFF ")
    assert result["none"] == "NO-COORDS"
    assert result["missing"] == "NO-ARTICLE"
    assert result["other"] == "NOT-WIKIPEDIA"


def test_coordinate_batches(entry: dict) -> None:
    batches = []

    def respond(request: httpx.Request) -> httpx.Response:
        titles = request.url.params["titles"].split("|")
        batches.append(len(titles))
        pages = {
            str(n): {"title": title, "coordinates": [{"lat": 30, "lon": -100}]}
            for n, title in enumerate(titles)
        }
        return httpx.Response(200, json={"query": {"pages": pages}})

    entries = [
        {**entry, "id": str(n), "source": f"https://en.wikipedia.org/wiki/{n}"} for n in range(41)
    ]
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert set(curation.verify_coordinates(entries, client).values()) == {"OK"}
    assert batches == [40, 1]


def test_factcheck_retry_resume_and_persistence(entry: dict, tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    existing = {"id": "done", "facts": [], "verdicts": []}
    path.write_text(json.dumps([existing]))
    calls = 0
    waits = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        assert request.headers["User-Agent"] == curation.HEADERS["User-Agent"]
        calls += 1
        if calls == 1:
            return httpx.Response(200, text="<html>busy</html>")
        return httpx.Response(200, json={"query": {"pages": {"1": {"extract": "Opened 1900"}}}})

    llm = FakeLLM()
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        report = asyncio.run(
            curation.factcheck([{**entry, "id": "done"}, entry], llm, client, 0, path, sleep=sleep)
        )
        assert report[0] == existing
        assert report[1]["verdicts"] == [{"n": 1, "verdict": "supported"}]
        assert json.loads(path.read_text()) == report
        asyncio.run(curation.factcheck([entry], llm, client, 0, path, sleep=sleep))
    assert llm.calls == 1
    assert calls == 2
    assert waits == [5, 0]


def test_factcheck_errors_continue(entry: dict, tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params["titles"] == "Bad":
            return httpx.Response(429)
        return httpx.Response(200, json={"query": {"pages": {"1": {"extract": "Text"}}}})

    entries = [{**entry, "id": "bad", "source": "https://en.wikipedia.org/wiki/Bad"}, entry]
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        report = asyncio.run(
            curation.factcheck(
                entries, FakeLLM("not json"), client, 0, tmp_path / "report.json", sleep=no_sleep
            )
        )
    assert len(report) == 2
    assert all(item["verdicts"][0]["verdict"] == "error" for item in report)
    assert report[0]["verdicts"][0]["fix"] == "Article unavailable"


def test_apply_report(entry: dict) -> None:
    report = [
        {
            "id": entry["id"],
            "verdicts": [
                {"n": 1, "verdict": "supported"},
                {
                    "n": 2,
                    "verdict": "claim",
                    "fix": (
                        "The article states that locals claim it is largest, "
                        "but it does not mention size"
                    ),
                },
            ],
        },
        {"id": "error", "verdicts": [{"n": 0, "verdict": "error"}]},
    ]
    kept, left = curation.apply_report(
        [entry, {**entry, "id": "error"}, {**entry, "id": "unchecked"}], report
    )
    assert kept[0]["facts"] == ["It opened in 1900.", "Locals claim it is largest."]
    assert left == ["error", "unchecked"]
    assert curation.clean("According to the article, a tower") == "A tower."
    assert curation.clean("The article does not support this") == ""
    assert curation.clean("") == ""
    assert entry["facts"][1] == "It is the largest."


def test_merge_dedupes_preserves_header(entry: dict, tmp_path: Path) -> None:
    path = tmp_path / "pack.yaml"
    header = "# Human curated\n# Keep this exact text.\n\n"
    path.write_text(header + yaml.safe_dump([entry]))
    new = {**entry, "id": "new", "name": "New"}
    assert curation.merge(path, [entry, {**entry, "id": "same-name"}, new, new]) == (1, 3)
    assert path.read_text().startswith(header)
    assert len(yaml.safe_load(path.read_text())) == 2


def test_research_injectable_runner(entry: dict, tmp_path: Path) -> None:
    async def runner(command, workdir):
        assert command[:2] == ["agy", "-p"]
        assert "codes): TX." in command[2]
        assert "give 6 landmarks" in command[2]
        assert "{states}" not in command[2]
        curation.write_pack(workdir / "landmarks.yaml", [entry])

    assert asyncio.run(curation.research(["TX"], 6, tmp_path, runner=runner)) == [entry]
    with pytest.raises(ConfigError, match="already exists"):
        asyncio.run(curation.research(["TX"], 6, tmp_path, runner=runner))
    with pytest.raises(ConfigError):
        asyncio.run(curation.research(["ZZ"], 6, tmp_path, runner=runner))
    with pytest.raises(ConfigError):
        asyncio.run(curation.research(["TX"], 0, tmp_path, runner=runner))


@pytest.mark.parametrize(
    "arguments,expected",
    [
        (["status", "--game", "ats", "--game-dir", "/tmp/game"], "status"),
        (
            [
                "add",
                "tx",
                "NM",
                "--per-state",
                "8",
                "--dry-run",
                "--pack",
                "p.yaml",
                "--workdir",
                "/tmp/work",
            ],
            "add",
        ),
        (["check", "p.yaml"], "check"),
    ],
)
def test_cli_parsing(monkeypatch, arguments, expected) -> None:
    captured = []

    async def handler(args):
        captured.append(args)

    monkeypatch.setattr(cli, "_landmarks", handler)
    monkeypatch.setattr("sys.argv", ["familiar", "landmarks", *arguments])
    cli.main()
    args = captured[0]
    assert args.landmark_action == expected
    if expected == "add":
        assert args.states == ["TX", "NM"]
        assert args.per_state == 8
        assert args.dry_run
        assert args.pack == Path("p.yaml")
        assert args.workdir == Path("/tmp/work")


def test_cli_help(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.argv", ["familiar", "landmarks", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert "status,add,check" in capsys.readouterr().out


def test_status_config_directory(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(
        cli, "load_config", lambda _: AppConfig(games={"ats": {"game_dir": str(tmp_path)}})
    )
    from familiar.games.scs.knowledge import KnowledgePack

    monkeypatch.setattr(KnowledgePack, "for_game", lambda _: KnowledgePack())
    cli._landmark_status(SimpleNamespace(config=None, game="ats", game_dir=None))
    output = capsys.readouterr().out
    assert "CA (California)" in output
    assert "familiar landmarks add CA" in output


@pytest.mark.parametrize("dry_run", [True, False])
def test_add_pipeline(monkeypatch, tmp_path: Path, entry: dict, capsys, dry_run: bool) -> None:
    pack = tmp_path / "pack.yaml"
    pack.write_text("# Keep me\n[]\n")
    before = pack.read_text()
    workdir = tmp_path / "work"
    rejected = {**entry, "id": "wrong-location", "name": "Wrong"}
    llm = FakeLLM()
    monkeypatch.setattr(cli, "load_config", lambda _: AppConfig())

    def load(kind, name, settings):
        assert kind == "llm"
        assert settings["temperature"] == 0
        return llm

    async def research(states, per_state, workdir):
        assert states == ["TX"]
        workdir.mkdir()
        return [entry, rejected]

    async def factcheck(entries, backend, client, delay, report_path):
        assert [p["id"] for p in entries] == [entry["id"]]
        assert backend is llm
        return [{"id": entry["id"], "verdicts": [{"n": 1, "verdict": "supported"}]}]

    monkeypatch.setattr(cli, "load", load)
    monkeypatch.setattr(curation, "research", research)
    monkeypatch.setattr(
        curation,
        "verify_coordinates",
        lambda entries, client: {entry["id"]: "OK", rejected["id"]: "OFF 100 km"},
    )
    monkeypatch.setattr(curation, "factcheck", factcheck)
    args = SimpleNamespace(
        landmark_action="add",
        config=None,
        states=["TX"],
        per_state=6,
        pack=pack,
        workdir=workdir,
        dry_run=dry_run,
    )
    asyncio.run(cli._landmarks(args))
    assert llm.closed
    staged = curation.read_pack(workdir / "landmarks.checked.yaml")
    assert len(staged) == 1
    assert staged[0]["facts"] == [entry["facts"][0]]
    if dry_run:
        assert pack.read_text() == before
    if not dry_run:
        assert pack.read_text().startswith("# Keep me\n")
        assert curation.read_pack(pack) == staged
    output = capsys.readouterr().out
    assert "wrong-location: OFF 100 km" in output
    assert "TX: kept 1, dropped 1" in output
    assert "landmarks.checked.yaml" in output


def test_check_report_only(monkeypatch, tmp_path: Path, entry: dict, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    pack = tmp_path / "pack.yaml"
    curation.write_pack(pack, [entry])
    before = pack.read_bytes()
    monkeypatch.setattr(cli, "load_config", lambda _: AppConfig())
    llm = FakeLLM()
    monkeypatch.setattr(cli, "load", lambda *args: llm)
    monkeypatch.setattr(
        curation, "verify_coordinates", lambda entries, client: {entry["id"]: "NO-COORDS"}
    )

    async def factcheck(entries, llm, client, delay, report_path):
        assert entries == [entry]
        report_path.write_text("[]")
        return []

    monkeypatch.setattr(curation, "factcheck", factcheck)
    asyncio.run(cli._landmarks(SimpleNamespace(landmark_action="check", pack=pack, config=None)))
    assert pack.read_bytes() == before
    assert list(tmp_path.glob("data/landmarks/*/factcheck.json"))
    assert not list(tmp_path.glob("data/landmarks/*/landmarks.checked.yaml"))
    assert "NO-COORDS" in capsys.readouterr().out
    assert llm.closed
