import asyncio
import subprocess
import sys
from uuid import UUID

import pytest
from aiohttp import ClientPayloadError
from familiar_voice_server.supervisor import create_app, main, spawn_worker

from .conftest import SPEECH, TOKEN, until
from .http_utils import LocalTestClient as TestClient
from .http_utils import LocalTestServer as TestServer


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/v1/status"),
        ("post", "/v1/leases"),
        ("put", "/v1/leases/unknown"),
        ("delete", "/v1/leases/unknown"),
        ("post", "/v1/speech"),
        ("get", "/unknown"),
    ],
)
async def test_auth(harness, method, path):
    response = await getattr(harness.client, method)(
        path, headers={"Authorization": "Bearer wrong"}
    )
    assert response.status == 401
    assert await response.json() == {"error": "unauthorized", "state": "idle"}


async def test_missing_auth(config):
    async with TestClient(TestServer(create_app(config, TOKEN))) as client:
        assert (await client.get("/v1/status")).status == 401


async def test_lease_lifecycle_and_status(harness):
    status = await (await harness.client.get("/v1/status")).json()
    assert status == {
        "state": "idle",
        "leases": 0,
        "model": "test-model",
        "voices": ["sarah"],
        "busy": False,
    }
    assert harness.spawns == 0
    lease = await harness.lease()
    assert UUID(lease["lease_id"]).version == 4
    assert lease["state"] == "loading"
    assert lease["ttl_s"] == 0.15
    await until(lambda: harness.spawns == 1)
    renewed = await harness.client.put(f"/v1/leases/{lease['lease_id']}")
    assert renewed.status == 200
    assert await renewed.json() == {"ttl_s": 0.15, "state": "loading"}
    second = await harness.lease()
    assert second["lease_id"] != lease["lease_id"]
    assert (await (await harness.client.get("/v1/status")).json())["leases"] == 2
    assert (await harness.client.delete(f"/v1/leases/{lease['lease_id']}")).status == 204
    assert (await harness.client.delete("/v1/leases/missing")).status == 204
    assert (await harness.client.put(f"/v1/leases/{lease['lease_id']}")).status == 404
    assert (await harness.client.put("/v1/leases/missing")).status == 404


async def test_renew_moves_expiry_from_last_renew(harness):
    lease = await harness.lease()
    await asyncio.sleep(0.09)
    assert (await harness.client.put(f"/v1/leases/{lease['lease_id']}")).status == 200
    await asyncio.sleep(0.09)
    assert lease["lease_id"] in harness.supervisor.leases
    await until(lambda: not harness.supervisor.leases)
    assert (await harness.client.put(f"/v1/leases/{lease['lease_id']}")).status == 404


async def test_loading_then_streaming_and_busy(harness):
    await harness.lease()
    response = await harness.client.post("/v1/speech", json=SPEECH)
    assert response.status == 503
    assert (await response.json())["state"] == "loading"
    harness.loading.set()
    await until(lambda: harness.supervisor.state == "ready")
    engine = harness.engines[0]
    engine.block = True
    response = await harness.client.post("/v1/speech", json=SPEECH)
    assert response.status == 200
    assert response.headers["Content-Type"] == "audio/L16; rate=24000; channels=1"
    assert await response.content.readexactly(6) == b"\x00\x80\x00\x00\xff\x7f"
    assert not engine.finished.is_set()  # First chunk really arrived before generation finished.
    second = await harness.client.post("/v1/speech", json=SPEECH)
    assert second.status == 503
    assert (await second.json())["error"] == "busy"
    assert (await (await harness.client.get("/v1/status")).json())["busy"] is True
    engine.release.set()
    assert await response.read() == b"\x00\x40"
    await until(lambda: not harness.supervisor.busy)
    assert engine.calls[0][2] == "english"


@pytest.mark.parametrize(
    "body",
    [
        {**SPEECH, "voice": "unknown"},
        {**SPEECH, "language": "unknown"},
        {**SPEECH, "text": " "},
        {**SPEECH, "text": 12},
        {**SPEECH, "voice": []},
        {**SPEECH, "language": {}},
        [],
        {},
        None,
    ],
)
async def test_bad_speech(harness, body):
    response = await harness.client.post("/v1/speech", json=body)
    assert response.status == 400
    assert set(await response.json()) == {"error", "state"}


async def test_invalid_json(harness):
    for path in ("/v1/speech", "/v1/leases"):
        response = await harness.client.post(
            path, data="invalid", headers={"Content-Type": "application/json"}
        )
        assert response.status == 400


async def test_idle_speech(harness):
    response = await harness.client.post("/v1/speech", json=SPEECH)
    assert response.status == 503
    assert (await response.json())["state"] == "idle"


async def test_expiry_then_grace_unloads(harness):
    await harness.ready()
    process = harness.processes[0]
    await until(lambda: not harness.supervisor.leases)
    assert not process.terminated
    await until(lambda: harness.supervisor.state == "idle")
    assert process.terminated
    assert not process.killed


async def test_lease_in_grace_cancels_stop(harness):
    await harness.ready()
    await until(lambda: not harness.supervisor.leases)
    lease = await harness.lease()
    await asyncio.sleep(0.08)
    assert harness.supervisor.state == "ready"
    assert not harness.processes[0].terminated
    assert harness.spawns == 1
    assert (await harness.client.delete(f"/v1/leases/{lease['lease_id']}")).status == 204
    await until(lambda: harness.supervisor.state == "idle")


async def test_does_not_unload_inflight(harness):
    await harness.ready()
    engine = harness.engines[0]
    engine.block = True
    response = await harness.client.post("/v1/speech", json=SPEECH)
    await response.content.readexactly(6)
    await until(lambda: not harness.supervisor.leases)
    await asyncio.sleep(0.09)
    assert harness.supervisor.busy
    assert not harness.processes[0].terminated
    engine.release.set()
    await response.read()
    await until(lambda: harness.supervisor.state == "idle")


async def test_disconnect_does_not_release_busy_early(harness):
    await harness.ready()
    engine = harness.engines[0]
    engine.block = True
    response = await harness.client.post("/v1/speech", json=SPEECH)
    await response.content.readexactly(6)
    response.close()
    await until(lambda: not harness.supervisor.leases)
    await asyncio.sleep(0.09)
    assert harness.supervisor.busy
    assert not harness.processes[0].terminated
    engine.release.set()
    await until(lambda: harness.supervisor.state == "idle")


async def test_worker_crash_and_next_lease_restarts(harness):
    await harness.ready()
    harness.processes[0].exit(1)
    await until(lambda: harness.supervisor.state == "failed")
    assert harness.spawns == 1
    lease = await harness.lease()
    assert lease["state"] == "loading"
    await until(lambda: harness.supervisor.state == "ready")
    assert harness.spawns == 2


async def test_spawn_failure_retry(harness):
    harness.loading.set()
    harness.fail_spawn = True
    await harness.lease()
    await until(lambda: harness.supervisor.state == "failed")
    harness.fail_spawn = False
    await harness.lease()
    await until(lambda: harness.supervisor.state == "ready")


async def test_kill_after_stop_timeout(harness):
    await harness.ready()
    process = harness.processes[0]
    process.ignore_term = True
    await until(lambda: harness.supervisor.state == "idle")
    assert process.terminated and process.killed
    assert process.returncode == -9


async def test_lease_while_stopping_restarts(harness):
    await harness.ready()
    harness.processes[0].ignore_term = True
    await until(lambda: harness.supervisor.state == "stopping")
    lease = await harness.lease()
    assert lease["state"] == "stopping"
    await until(lambda: harness.spawns == 2 and harness.supervisor.state == "ready")


@pytest.mark.parametrize("late", [False, True])
async def test_synthesis_failure(harness, late):
    await harness.ready()
    engine = harness.engines[0]
    engine.fail_before = not late
    engine.fail_after = late
    response = await harness.client.post("/v1/speech", json=SPEECH)
    if not late:
        assert response.status == 500
        assert set(await response.json()) == {"error", "state"}
        return
    assert response.status == 200
    with pytest.raises(ClientPayloadError):
        await response.read()
    await until(lambda: not harness.supervisor.busy)


async def test_spawn_command(config, monkeypatch):
    calls = []

    async def spawn(*args):
        calls.append(args)
        return "process"

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert await spawn_worker(config, 12345) == "process"
    assert calls == [
        (
            sys.executable,
            "-m",
            "familiar_voice_server.worker",
            "--config",
            str(config.path),
            "--port",
            "12345",
        )
    ]


def test_supervisor_import_does_not_import_mlx():
    code = (
        "import sys; import familiar_voice_server.supervisor; "
        "assert 'mlx' not in sys.modules; assert 'numpy' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_main_requires_token(config, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["familiar-voice-server", "--config", str(config.path)])
    monkeypatch.delenv(config.token_env, raising=False)
    with pytest.raises(SystemExit, match="2"):
        main()
    assert config.token_env in capsys.readouterr().err


async def test_disconnect_before_first_audio_keeps_worker_protected(harness):
    await harness.ready()
    engine = harness.engines[0]
    engine.block = True
    engine.block_before = True
    request = asyncio.create_task(harness.client.post("/v1/speech", json=SPEECH))
    await until(engine.started.is_set)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    await until(lambda: not harness.supervisor.leases)
    await asyncio.sleep(0.09)
    assert not harness.processes[0].terminated
    engine.first_release.set()
    await asyncio.sleep(0.03)
    assert harness.supervisor.busy
    assert not harness.processes[0].terminated
    engine.release.set()
    await until(lambda: harness.supervisor.state == "idle")


async def test_renewal_restarts_crashed_worker_after_backoff(harness):
    """A running client only renews; a crash must not leave it on the fallback forever."""
    lease = await harness.ready()
    harness.processes[0].exit(1)
    await until(lambda: harness.supervisor.state == "failed")
    path = f"/v1/leases/{lease['lease_id']}"
    response = await harness.client.put(path)
    assert (await response.json())["state"] == "failed"  # within the backoff: no restart yet
    assert harness.spawns == 1
    harness.supervisor.retry_failed_s = 0
    response = await harness.client.put(path)
    assert response.status == 200
    await until(lambda: harness.supervisor.state == "ready")
    assert harness.spawns == 2
