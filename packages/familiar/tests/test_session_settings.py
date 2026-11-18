"""Fail-closed launch selections, atomic writes and precedence."""

import argparse
from dataclasses import asdict, replace

import pytest
import yaml
from familiar import cli
from familiar import session_settings as session
from familiar.config import AppConfig, session_settings


@pytest.fixture
def roster(tmp_path, persona):
    for identifier in ("test", "other", "third"):
        member = replace(persona, id=identifier, display_name=identifier.title())
        (tmp_path / f"{identifier}.yaml").write_text(yaml.safe_dump(asdict(member)))
    return tmp_path


def settings(**changes):
    base: dict = {"crew": ("test", "other"), "language": "en", "carrier": "other", "banter": 7}
    return session.SessionSettings(**(base | changes))


def read(path, roster, **kwargs):
    return session.load(path, personas_dir=roster, user_personas_dir=roster, **kwargs)


def test_roundtrip_order_null_game_and_xdg(roster, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(roster))
    assert session.default_path() == roster / "familiar/session.toml"
    path = session.default_path()
    session.save(path, settings())
    assert read(path, roster) == settings()
    assert "game" not in path.read_text()
    session.save(path, settings(game="elite"))
    assert read(path, roster).game == "elite"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(roster))
    assert session.default_path() == roster / ".config/familiar/session.toml"


def test_atomic_failure_keeps_original_and_removes_temp(roster, monkeypatch):
    path = roster / "session.toml"
    session.save(path, settings())
    original = path.read_bytes()

    def fail(*args):
        assert path.read_bytes() == original
        raise OSError("replace failed")

    monkeypatch.setattr(session.os, "replace", fail)
    with pytest.raises(OSError, match="replace failed"):
        session.save(path, settings(banter=2))
    assert path.read_bytes() == original
    assert not list(roster.glob(".session.toml.*"))


def test_every_error_reported_together(roster):
    path = roster / "session.toml"
    path.write_text(
        'version = 3\ncrew = ["test", "missing", "third"]\n'
        'carrier = "outsider"\nlanguage = "ja"\nbanter = 11\napi_key = "bad"\n'
    )
    with pytest.raises(session.SessionError) as raised:
        read(path, roster)
    message = str(raised.value)
    for problem in ("version", "banter", "carrier", "api_key", "missing", "test", "third"):
        assert problem in message


@pytest.mark.parametrize(
    "line",
    [
        "crew = []",
        'crew = ["test", "test"]',
        'crew = ["test", "other", "third", "fourth"]',
        'crew = "test"',
        'crew = [4, "test"]',
        'crew = ["../bad"]',
        "carrier = 4",
        "language = []",
        'language = ""',
        "banter = true",
        "banter = 0.5",
        "game = 42",
        "version = true",
        "stereo = 1",
        "seats = 4",
        "seats = {ghost = 0.5}",
        "seats = {test = 2.0}",
        "seats = {test = -1.5}",
        "seats = {test = true}",
        'seats = {test = "left"}',
        'secret = "never allowed"',
    ],
)
def test_malformed_fields_fail_closed(roster, line):
    path = roster / "session.toml"
    session.save(path, settings())
    key = line.split(" = ")[0]
    body = [row for row in path.read_text().splitlines() if not row.startswith(key + " = ")]
    path.write_text("\n".join([*body, line]) + "\n")
    with pytest.raises(session.SessionError):
        read(path, roster)


def test_missing_fields_personas_and_empty_voices(roster, persona):
    path = roster / "session.toml"
    path.write_text('crew = ["missing"]')
    with pytest.raises(session.SessionError) as raised:
        read(path, roster)
    assert "Missing session key: language" in str(raised.value)
    assert "missing.yaml" in str(raised.value)
    empty = replace(persona, languages={"en": {"voices": {"gemini": None}}})
    (roster / "test.yaml").write_text(yaml.safe_dump(asdict(empty)))
    session.save(path, settings())
    with pytest.raises(session.SessionError, match="has no voices"):
        read(path, roster)


@pytest.mark.parametrize("body", ["version = [", "[broken", "version = 1\nversion = 1"])
def test_invalid_toml(roster, body):
    path = roster / "session.toml"
    path.write_text(body)
    with pytest.raises(session.SessionError, match="Cannot read session"):
        read(path, roster)


def test_missing_explicit_file(roster):
    with pytest.raises(session.SessionError, match="Cannot read session"):
        read(roster / "absent.toml", roster)


def test_precedence_and_single_persona_override(roster, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(roster))
    config = AppConfig(
        persona="third", language="ja", game="ats", personas_dir=roster, user_personas_dir=roster
    )
    assert session_settings(config) == settings(
        crew=("third",), carrier="third", language="ja", game="ats", banter=0
    )
    session.save(session.default_path(), settings())
    assert session_settings(config) == settings()
    assert session_settings(config, persona="test", game="ksp", language="en") == settings(
        crew=("test",), carrier="test", game="ksp", seats={"test": 0.6}
    )  # an overridden crew keeps each member's saved seat
    assert session_settings(config, use_session=False).crew == ("third",)


async def test_runtime_env_flags_and_sticky_session(roster, monkeypatch):
    path = roster / "session.toml"
    session.save(path, settings())
    config = AppConfig(personas_dir=roster, user_personas_dir=roster)
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setenv("FAMILIAR_SESSION", str(path))
    monkeypatch.setenv("FAMILIAR_PERSONA", "third")
    monkeypatch.setenv("FAMILIAR_LANGUAGE", "en")
    args = argparse.Namespace(
        config=None, command="voice", session=None, persona=None, game=None, language=None
    )
    async with cli._runtime(args) as runtime:
        assert [member.id for member in runtime.crew.members] == ["third"]
    args.persona = "test"
    async with cli._runtime(args) as runtime:
        assert runtime.crew.carrier.id == "test"
    monkeypatch.delenv("FAMILIAR_PERSONA")
    args.persona = None
    async with cli._runtime(args) as runtime:
        assert [member.id for member in runtime.crew.members] == ["test", "other"]
        assert runtime.crew.carrier.id == "other"
        assert runtime.session.banter == 7
    assert read(path, roster) == settings()


def test_persona_file_cannot_substitute_another_identity(roster, persona):
    path = roster / "session.toml"
    session.save(path, settings())
    (roster / "other.yaml").write_text(yaml.safe_dump(asdict(persona)))
    with pytest.raises(session.SessionError, match="does not match file id"):
        read(path, roster)


def test_version_one_file_loads_with_stereo_defaults(roster):
    path = roster / "session.toml"
    path.write_text(
        'version = 1\ncrew = ["test", "other"]\ncarrier = "other"\nlanguage = "en"\nbanter = 7\n'
    )
    loaded = read(path, roster)
    assert loaded == settings()
    assert loaded.version == 2
    assert loaded.stereo is False
    assert loaded.seats == {"test": 0.6, "other": 0.0}


def test_default_seats_alternate_around_the_carrier(roster):
    assert session.default_seats(("test", "other", "third"), "test") == {
        "test": 0.0,
        "other": 0.6,
        "third": -0.6,
    }
    assert session.default_seats(("test", "other"), "other") == {"test": 0.6, "other": 0.0}
    assert session.default_seats("not-a-list", None) == {}


def test_save_always_writes_version_two_with_seats(roster):
    path = roster / "session.toml"
    session.save(path, replace(settings(stereo=True), version=1))
    text = path.read_text()
    assert "version = 2" in text
    assert "stereo = true" in text
    loaded = read(path, roster)
    assert loaded.stereo is True
    assert loaded.seats == {"test": 0.6, "other": 0.0}


def test_custom_seats_roundtrip_and_missing_members_default(roster):
    path = roster / "session.toml"
    session.save(path, settings(stereo=True, seats={"other": -0.35}))
    loaded = read(path, roster)
    assert loaded.seats == {"test": 0.6, "other": -0.35}
    path.write_text(
        'version = 2\ncrew = ["test", "other"]\ncarrier = "other"\nlanguage = "en"\n'
        "banter = 7\nstereo = true\nseats = {test = 0.35}\n"
    )
    assert read(path, roster).seats == {"test": 0.35, "other": 0.0}


def test_seat_problems_reported_together(roster):
    path = roster / "session.toml"
    path.write_text(
        'version = 2\ncrew = ["test", "other"]\ncarrier = "other"\nlanguage = "en"\n'
        'banter = 7\nstereo = "yes"\nseats = {ghost = 0.5, test = 3.0}\n'
    )
    with pytest.raises(session.SessionError) as raised:
        read(path, roster)
    message = str(raised.value)
    assert "stereo must be true or false" in message
    assert "seats key 'ghost' is not a crew member" in message
    assert "seats pan for 'test' must be a number from -1.0 to 1.0" in message
