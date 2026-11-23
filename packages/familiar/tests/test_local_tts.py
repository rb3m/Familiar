"""Offline contract tests for the leased local voice client."""

import asyncio

import httpx
import pytest
from familiar.backends.local.tts import LocalTTS
from familiar.interfaces import AudioChunk, BackendError, ConfigError


class Stream(httpx.AsyncByteStream):
    def __init__(self, parts=(b"\1", b"\0\2", b"\0"), *, clock=None, block=False):
        self.parts = parts
        self.clock = clock
        self.block = block
        self.closed = False

    async def __aiter__(self):
        if self.block:
            await asyncio.Event().wait()
        for index, part in enumerate(self.parts):
            if index and self.clock is not None:
                self.clock[0] += 2
            yield part

    async def aclose(self):
        self.closed = True


@pytest.fixture
def factory(monkeypatch):
    monkeypatch.setenv("FAMILIAR_VOICE_TOKEN", "secret")
    backends = []

    def create(handler, **settings):
        client = httpx.AsyncClient(
            base_url="http://voice.invalid",
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer secret"},
        )
        monkeypatch.setattr("familiar.backends.local.tts.make_client", lambda _: client)
        clock = settings.pop("clock", lambda: 0)
        backend = LocalTTS({"url": "http://voice.invalid", **settings}, clock=clock)
        backends.append(backend)
        return backend

    yield create
    assert all(backend._closed for backend in backends)


async def collect(backend):
    return [chunk async for chunk in backend.synthesize("Hello", "sarah", "en")]


def lease_response(request, state="ready"):
    return httpx.Response(201, json={"lease_id": "lease", "state": state, "ttl_s": 30})


@pytest.mark.asyncio
async def test_lease_renew_expiry_and_delete(factory):
    requests = []
    renewed = asyncio.Event()

    def handler(request):
        requests.append((request.method, request.url.path))
        assert request.headers["Authorization"] == "Bearer secret"
        if request.method == "PUT":
            if sum(method == "PUT" for method, _ in requests) == 1:
                return httpx.Response(404)
            renewed.set()
        return lease_response(request)

    backend = factory(handler, renew_s=0.001)
    await asyncio.wait_for(renewed.wait(), 1)
    assert backend._ready
    await backend.aclose()
    assert requests.count(("POST", "/v1/leases")) == 2
    assert requests[-1] == ("DELETE", "/v1/leases/lease")
    assert backend._client.is_closed
    await backend.aclose()


@pytest.mark.asyncio
async def test_not_ready_status_and_pcm(factory):
    stream = Stream()
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/v1/speech":
            return httpx.Response(200, stream=stream)
        if request.url.path == "/v1/status":
            return httpx.Response(200, json={"state": "ready"})
        return lease_response(request, "loading")

    backend = factory(handler)
    try:
        with pytest.raises(BackendError) as caught:
            await collect(backend)
        assert caught.value.retryable
        assert "/v1/speech" not in paths
        await asyncio.sleep(0)
        assert not backend._ready
        await backend.status()
        assert await collect(backend) == [AudioChunk(b"\1\0", 24000), AudioChunk(b"\2\0", 24000)]
        assert stream.closed
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_deadline_and_parking_window(factory):
    clock = [0.0]
    streams = []

    def handler(request):
        if request.url.path != "/v1/speech":
            return lease_response(request)
        stream = Stream(block=True)
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    backend = factory(
        handler,
        clock=lambda: clock[0],
        first_audio_deadline_ms=1,
        park_after_misses=2,
        park_window_s=10,
        park_s=20,
    )
    try:
        await asyncio.sleep(0)
        for when in (0, 11, 12):
            clock[0] = when
            with pytest.raises(BackendError, match="deadline") as caught:
                await collect(backend)
            assert caught.value.retryable
            assert streams[-1].closed
        with pytest.raises(BackendError, match="parked"):
            await collect(backend)
        assert len(streams) == 3
        clock[0] = 33
        with pytest.raises(BackendError, match="deadline"):
            await collect(backend)
        assert len(streams) == 4
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_stall_finishes_sentence_then_parks(factory):
    clock = [0.0]
    stream = Stream((b"\1\0", b"\2\0", b"\3\0"), clock=clock)
    backend = factory(
        lambda request: (
            httpx.Response(200, stream=stream)
            if request.url.path == "/v1/speech"
            else lease_response(request)
        ),
        clock=lambda: clock[0],
    )
    try:
        await asyncio.sleep(0)
        assert len(await collect(backend)) == 3
        with pytest.raises(BackendError, match="parked"):
            await collect(backend)
        # A single stall parks only for stall_park_s (the rest of the turn), not park_s.
        assert backend._parked_until == pytest.approx(clock[0] + 20)
    finally:
        await backend.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,retryable", [(400, False), (401, False), (503, True), (500, True), (502, True)]
)
async def test_error_mapping(factory, code, retryable):
    backend = factory(
        lambda request: (
            httpx.Response(code) if request.url.path == "/v1/speech" else lease_response(request)
        )
    )
    try:
        await asyncio.sleep(0)
        with pytest.raises(BackendError) as caught:
            await collect(backend)
        assert caught.value.retryable is retryable
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_transport_error(factory):
    def handler(request):
        if request.url.path == "/v1/speech":
            raise httpx.ConnectError("offline", request=request)
        return lease_response(request)

    backend = factory(handler)
    try:
        await asyncio.sleep(0)
        with pytest.raises(BackendError) as caught:
            await collect(backend)
        assert caught.value.retryable
    finally:
        await backend.aclose()


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"url": "ftp://server"},
        {"url": "http://server", "renew_s": 0},
        {"url": "http://server", "park_after_misses": 1.5},
        {"url": "http://server", "timeout_seconds": float("nan")},
        {"url": "http://server", "first_audio_deadline_ms": True},
        {"url": "http://server", "token_env": "MISSING_LOCAL_TOKEN"},
    ],
)
def test_invalid_settings(settings, monkeypatch):
    monkeypatch.setenv("FAMILIAR_VOICE_TOKEN", "secret")
    monkeypatch.delenv("MISSING_LOCAL_TOKEN", raising=False)
    with pytest.raises(ConfigError):
        LocalTTS(settings)


def test_lazy_lease_start(factory):
    backend = factory(lease_response)
    assert backend._renewal is None

    async def scenario():
        try:
            with pytest.raises(BackendError):
                await collect(backend)
            await asyncio.sleep(0)
            assert backend._ready
        finally:
            await backend.aclose()

    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_lease_connection_failure_retries_with_backoff(factory, monkeypatch, caplog):
    real_sleep = asyncio.sleep
    delays = []
    attempts = 0
    ready = asyncio.Event()

    async def sleep(delay):
        delays.append(delay)
        if ready.is_set():
            await asyncio.Event().wait()
        await real_sleep(0)

    def handler(request):
        nonlocal attempts
        if request.method == "DELETE":
            return httpx.Response(204)
        attempts += 1
        if attempts < 4:
            raise httpx.ConnectError("offline", request=request)
        ready.set()
        return lease_response(request)

    monkeypatch.setattr("familiar.backends.local.tts.asyncio.sleep", sleep)
    backend = factory(handler)
    try:
        await asyncio.wait_for(ready.wait(), 1)
        assert backend._ready
        assert delays[:3] == [1, 2, 4]
        assert not caplog.records
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_cancellation_after_audio_closes_response(factory):
    waiting = asyncio.Event()

    class HangingStream(Stream):
        async def __aiter__(self):
            yield b"\1\0"
            waiting.set()
            await asyncio.Event().wait()

    stream = HangingStream()
    backend = factory(
        lambda request: (
            httpx.Response(200, stream=stream)
            if request.url.path == "/v1/speech"
            else lease_response(request)
        )
    )
    try:
        await asyncio.sleep(0)
        task = asyncio.create_task(collect(backend))
        await asyncio.wait_for(waiting.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed
    finally:
        await backend.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("parts", [(), (b"\1",)])
async def test_empty_and_incomplete_streams(factory, parts):
    stream = Stream(parts)
    backend = factory(
        lambda request: (
            httpx.Response(200, stream=stream)
            if request.url.path == "/v1/speech"
            else lease_response(request)
        )
    )
    try:
        await asyncio.sleep(0)
        with pytest.raises(BackendError) as caught:
            await collect(backend)
        assert caught.value.retryable
        assert stream.closed
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_missing_persona_voice_falls_back_without_disabling(factory):
    """A persona without a local voice must not switch the local voice off for everyone."""
    backend = factory(lease_response)
    try:
        await asyncio.sleep(0)
        with pytest.raises(BackendError, match="No local voice") as info:
            async for _ in backend.synthesize("Hola", None, "es"):
                pass
        assert info.value.retryable
    finally:
        await backend.aclose()
