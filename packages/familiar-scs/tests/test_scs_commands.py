import pytest
from familiar import cli
from familiar_scs import commands


@pytest.mark.parametrize(
    "command,arguments",
    [
        ("watch", ["--game", "ats", "--replay", "drive.jsonl"]),
        ("telemetry-record", ["drive.jsonl", "--game", "ats"]),
    ],
)
def test_lazy_command_argument_parsing(monkeypatch, command: str, arguments: list[str]) -> None:
    captured = []

    async def handler(args, config):
        captured.append(args)

    attribute = {"watch": "_watch", "telemetry-record": "_telemetry_record"}[command]
    monkeypatch.setattr(commands, attribute, handler)
    monkeypatch.setattr("sys.argv", ["familiar", command, *arguments])
    cli.main()
    assert captured[0].game == "ats"
    field = {"watch": "replay", "telemetry-record": "file"}[command]
    assert getattr(captured[0], field) == "drive.jsonl"


@pytest.mark.parametrize("action,regions", [("add", ["ru", "de"]), ("check", [])])
@pytest.mark.parametrize("game", ["ats", "ets2"])
def test_landmark_game_selection(monkeypatch, action, regions, game):
    captured = []

    async def handler(args, config):
        captured.append(args)

    monkeypatch.setattr(commands, "_landmarks", handler)
    monkeypatch.setattr("sys.argv", ["familiar", "landmarks", action, *regions, "--game", game])
    cli.main()
    assert captured[0].game == game
    if action == "add":
        assert captured[0].states == ["RU", "DE"]
        assert captured[0].per_state is None


def test_landmark_status_ats_only(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["familiar", "landmarks", "--help"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "ATS only" in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["familiar", "landmarks", "status", "--game", "ets2"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
