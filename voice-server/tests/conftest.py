import asyncio
import threading

import numpy as np
import pytest
import pytest_asyncio
import yaml
from familiar_voice_server.config import load_config
from familiar_voice_server.supervisor import SUPERVISOR, create_app
from familiar_voice_server.worker import WARMUP_TEXT
from familiar_voice_server.worker import create_app as worker_app

from .http_utils import LocalConnector, unused_port
from .http_utils import LocalTestClient as TestClient
from .http_utils import LocalTestServer as TestServer

TOKEN = "test-secret"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
SPEECH = {"text": "Hello", "voice": "sarah", "language": "en"}


async def until(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.fixture
def config(tmp_path):
    (tmp_path / "voice.wav").touch()
    data = {
        "model": {"id": "test-model"},
        "voices": {"sarah": {"mode": "clone", "ref_audio": "voice.wav", "ref_text": "Reference"}},
        "lease": {"ttl_s": 0.15, "grace_s": 0.06},
        "worker": {"stop_timeout_s": 0.03},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return load_config(path)


class FakeEngine:
    model_type = "base"
    sample_rate = 24000

    def __init__(self, config):
        self.release = threading.Event()
        self.first_release = threading.Event()
        self.started = threading.Event()
        self.finished = threading.Event()
        self.block = False
        self.block_before = False
        self.fail_before = False
        self.fail_after = False
        self.calls = []
        self.threads = []

    def generate(self, text, voice, language):
        if text == WARMUP_TEXT:  # the worker's start-up warm-up; not part of what tests observe
            yield np.zeros(1, dtype=np.float32)
            return
        self.calls.append((text, voice, language))
        self.threads.append(threading.get_ident())
        self.started.set()
        try:
            if self.block_before:
                assert self.first_release.wait(3), "test did not release first chunk"
            if self.fail_before:
                raise RuntimeError("fake pre-audio failure")
            yield np.array([-1, 0, 1], dtype=np.float32)
            if self.block:
                assert self.release.wait(3), "test did not release fake engine"
            if self.fail_after:
                raise RuntimeError("fake mid-audio failure")
            yield np.array([0.5], dtype=np.float32)
        finally:
            self.finished.set()


class FakeProcess:
    def __init__(self, server):
        self.server = server
        self.returncode = None
        self.done = asyncio.Event()
        self.terminated = False
        self.killed = False
        self.ignore_term = False
        self.close_task = None

    def exit(self, code):
        self.returncode = code
        self.done.set()
        self.close_task = asyncio.create_task(self.server.close())

    def terminate(self):
        self.terminated = True
        if self.ignore_term:
            return
        self.exit(-15)

    def kill(self):
        self.killed = True
        self.exit(-9)

    async def wait(self):
        await self.done.wait()
        return self.returncode


class Harness:
    def __init__(self, config):
        self.config = config
        self.loading = asyncio.Event()
        self.engines = []
        self.processes = []
        self.client = None
        self.spawns = 0
        self.fail_spawn = False

    async def spawn(self, config, port):
        self.spawns += 1
        await self.loading.wait()
        if self.fail_spawn:
            raise OSError("fake spawn failure")
        engine = FakeEngine(config)
        self.engines.append(engine)
        server = TestServer(worker_app(config, lambda cfg: engine), host="127.0.0.1", port=port)
        await server.start_server()
        process = FakeProcess(server)
        self.processes.append(process)
        return process

    @property
    def supervisor(self):
        return self.client.app[SUPERVISOR]

    async def lease(self):
        response = await self.client.post("/v1/leases", json={"client": "test"})
        assert response.status == 201
        return await response.json()

    async def ready(self):
        self.loading.set()
        lease = await self.lease()
        await until(lambda: self.supervisor.state == "ready")
        return lease

    async def close(self):
        self.loading.set()
        for engine in self.engines:
            engine.first_release.set()
            engine.release.set()
        await self.client.close()
        for process in self.processes:
            if process.close_task:
                await process.close_task


@pytest_asyncio.fixture
async def harness(config):
    harness = Harness(config)
    harness.client = TestClient(
        TestServer(create_app(config, TOKEN, harness.spawn)), headers=HEADERS
    )
    await harness.client.start_server()
    try:
        yield harness
    finally:
        await harness.close()


@pytest.fixture(autouse=True)
def local_http(monkeypatch):
    from aiohttp import ClientSession
    from familiar_voice_server import supervisor

    def session(**kwargs):
        return ClientSession(connector=LocalConnector(), **kwargs)

    monkeypatch.setattr(supervisor, "ClientSession", session)
    monkeypatch.setattr(supervisor, "free_port", unused_port)
