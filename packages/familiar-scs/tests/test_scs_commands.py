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
