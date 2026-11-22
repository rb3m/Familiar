"""Subprocess boundaries, permission profiles, parsing and cancellation."""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from familiar_research.agy import AgyRunner, ResearchError, settings_for
from familiar_research.masking import Masker, mask


@pytest.fixture
def runner():
    return AgyRunner(settings_for())


def process(output=b"", returncode=0):
    return Mock(
        pid=4321,
        returncode=returncode,
        communicate=AsyncMock(return_value=(output, None)),
        wait=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_argv_environment_and_private_cwd(monkeypatch, runner):
    output = {
        "status": "SUCCESS",
        "conversation_id": "thread",
        "structured_output": {"short_answer": "Answer", "details": "More", "sources": []},
        "response": "Ignore this free text",
    }
    spawn = AsyncMock(return_value=process(json.dumps(output).encode()))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    for key in runner.settings["strip_env"]:
        monkeypatch.setenv(key, "secret")
    monkeypatch.setenv("RESEARCH_TEST_KEEP", "kept")
    result, cid = await runner.run("question; $(touch nope)", "vault", "previous")
    assert result == output["structured_output"]
    assert cid == "thread"
    args, kwargs = spawn.call_args
    assert args[0] == "agy"
    assert args[-2:] == ("--conversation", "previous")
    assert "--sandbox" in args
    assert args[args.index("--model") + 1] == runner.settings["model"]
    assert args[args.index("--print-timeout") + 1] == "90s"
    assert "question; $(touch nope)" in args[2]
    schema = json.loads(args[args.index("--json-schema") + 1])
    assert schema["required"] == ["short_answer", "details", "sources"]
    permissions = json.loads(args[args.index("--settings") + 1])["permissions"]
    assert permissions["deny"] == runner.settings["deny"] + ["read_url(*)"]
    assert permissions["allow"] == runner.settings["scopes"]["vault"]["allow"]
    assert kwargs["start_new_session"] is True
    assert kwargs["env"]["RESEARCH_TEST_KEEP"] == "kept"
    assert not any(key in kwargs["env"] for key in runner.settings["strip_env"])
    assert not Path(kwargs["cwd"]).exists()
    assert "shell" not in kwargs
    assert "--conversation" not in runner.argv("prompt", "web")
    assert "read_url(*)" not in runner.argv("prompt", "web")[-1]


@pytest.mark.parametrize(
    "payload",
    [
        "bad",
        "[]",
        '{"status":"ERROR"}',
        '{"status":"SUCCESS","response":"x"}',
        '{"status":"SUCCESS","structured_output":{"short_answer":1}}',
        json.dumps(
            {
                "status": "SUCCESS",
                "structured_output": {"short_answer": "a", "details": "b", "sources": [3]},
            }
        ),
    ],
)
def test_invalid_outputs_do_not_expose_stdout(runner, payload):
    with pytest.raises(ResearchError) as error:
        runner._parse(payload.encode())
    assert payload not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_kill_group(monkeypatch, runner, cancel):
    entered = asyncio.Event()

    async def communicate():
        entered.set()
        await asyncio.Event().wait()

    child = process()
    child.communicate = communicate
    spawn = AsyncMock(return_value=child)
    kill = Mock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr("familiar_research.agy.os.killpg", kill)
    if not cancel:
        runner.settings["timeout_s"] = -14.99
    task = asyncio.create_task(runner.run("question", "web"))
    await entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises((asyncio.CancelledError, TimeoutError)):
        await task
    kill.assert_called_once_with(child.pid, signal.SIGKILL)
    child.wait.assert_awaited_once()
    assert not Path(spawn.call_args.kwargs["cwd"]).exists()


def test_masking():
    masker = Masker(settings_for())
    assert mask("QX7RT2") == "[masked]"
    assert masker.mask("QX7RT2 ZK4PLM 1234567890 1234567890123456") == " ".join(["[masked]"] * 4)
    safe = "GK 102 / NH 179, 12:30, 2027-01-23, $123.45, Hotel Monterey Sapporo ABCDEF 123456"
    assert masker.mask(safe) == safe


def test_environment_drops_our_virtualenv(monkeypatch, runner):
    """MCP servers started by agy must get the user's interpreter, not ours."""
    ours = str(Path(sys.prefix) / "bin")
    monkeypatch.setenv("VIRTUAL_ENV", sys.prefix)
    monkeypatch.setenv("PATH", os.pathsep.join([ours, "/usr/local/bin", "/usr/bin"]))
    env = runner.environment()
    assert "VIRTUAL_ENV" not in env
    assert env["PATH"].split(os.pathsep) == ["/usr/local/bin", "/usr/bin"]
