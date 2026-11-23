"""Lease lifecycle and deadline-protected PCM streaming for the LAN voice server."""

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from urllib.parse import quote

from familiar.interfaces import PLUGIN_API_VERSION, AudioChunk, BackendError

from .client import backend_errors, check_response, make_client, validate


class LocalTTS:
    name = "local"
    api_version = PLUGIN_API_VERSION
    capabilities = frozenset({"streaming"})

    def __init__(self, settings: dict, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.settings = validate(settings)
        self._client = make_client(self.settings)
        self._clock = clock
        self._lease_id: str | None = None
        self._ready = False
        self._closed = False
        self._renewal: asyncio.Task | None = None
        self._misses: deque[float] = deque()
        self._parked_until = 0.0
        self._start_renewal()

    def _start_renewal(self) -> None:
        if self._renewal is not None or self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._renewal = loop.create_task(self._renew())

    def _state(self, data: dict) -> None:
        self._ready = data.get("state") == "ready"

    async def _lease(self) -> None:
        with backend_errors():
            if self._lease_id is not None:
                response = await self._client.put(f"/v1/leases/{quote(self._lease_id, safe='')}")
                if response.status_code == 404:
                    self._lease_id = None
            if self._lease_id is None:
                response = await self._client.post(
                    "/v1/leases", json={"client": self.settings["client_name"]}
                )
            await check_response(response)
            data = response.json()
            if self._lease_id is None:
                self._lease_id = data["lease_id"]
            self._state(data)

    async def _renew(self) -> None:
        backoff = 1.0
        while True:
            try:
                await self._lease()
            except (BackendError, ValueError, KeyError):
                self._ready = False
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.settings["renew_s"])
                continue
            backoff = 1.0
            await asyncio.sleep(self.settings["renew_s"])

    async def status(self) -> dict:
        with backend_errors():
            response = await self._client.get("/v1/status")
            await check_response(response)
            data = response.json()
            self._state(data)
            return data

    def _park(self, seconds: float) -> None:
        self._parked_until = max(self._parked_until, self._clock() + seconds)

    def _miss(self, *, stall: bool = False) -> None:
        """Count a miss; a stall also parks briefly so the rest of the turn uses the fallback."""
        now = self._clock()
        self._misses.append(now)
        while self._misses and self._misses[0] < now - self.settings["park_window_s"]:
            self._misses.popleft()
        if stall:
            self._park(self.settings["stall_park_s"])
        if len(self._misses) >= self.settings["park_after_misses"]:
            self._park(self.settings["park_s"])
            self._misses.clear()

    async def synthesize(
        self, text: str, voice: str | None, language: str
    ) -> AsyncIterator[AudioChunk]:
        self._start_renewal()
        if self._closed or not self._ready or self._clock() < self._parked_until:
            raise BackendError("Local voice is not ready or is parked", retryable=True)
        if not voice:
            # Retryable: this sentence falls back, but other personas keep the local voice.
            raise BackendError(
                "No local voice configured for this persona/language", retryable=True
            )
        with backend_errors():
            try:
                # Include response headers in the deadline: a server can stall before them too.
                async with asyncio.timeout(self.settings["first_audio_deadline_ms"] / 1000):
                    response = await self._client.send(
                        self._client.build_request(
                            "POST",
                            "/v1/speech",
                            json={"text": text, "voice": voice, "language": language},
                        ),
                        stream=True,
                    )
                    try:
                        await check_response(response)
                        chunks = response.aiter_bytes()
                        first = await anext(chunks, b"")
                    except BaseException:
                        await response.aclose()
                        raise
            except TimeoutError as exc:
                self._miss()
                raise BackendError(
                    "Local voice missed first-audio deadline", retryable=True
                ) from exc
            stalled = False
            try:
                if not first:
                    self._miss()
                    raise BackendError("Local voice returned no audio", retryable=True)
                start = self._clock()
                received = 0
                pending = b""
                data = first
                while True:
                    # Measure the gap before adding the newly arrived audio to the buffer.
                    if (
                        received
                        and received / 48000 - (self._clock() - start)
                        < -self.settings["stall_margin_ms"] / 1000
                    ):
                        stalled = True
                    if not data:
                        break
                    received += len(data)
                    pending += data
                    end = len(pending) // 2 * 2
                    if end:
                        yield AudioChunk(pending[:end], 24000)
                        pending = pending[end:]
                    data = await anext(chunks, b"")
                if pending:
                    stalled = True
                    raise BackendError(
                        "Local voice returned incomplete PCM16 audio", retryable=True
                    )
            finally:
                await response.aclose()
                if stalled:
                    self._miss(stall=True)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._ready = False
        if self._renewal is not None:
            self._renewal.cancel()
            with suppress(asyncio.CancelledError):
                await self._renewal
        try:
            if self._lease_id is not None:
                with suppress(Exception):
                    async with asyncio.timeout(1):
                        await self._client.delete(f"/v1/leases/{quote(self._lease_id, safe='')}")
        finally:
            await self._client.aclose()
