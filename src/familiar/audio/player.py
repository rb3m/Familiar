"""Ordered PCM playback with bounded writes and interruptible output."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from familiar.interfaces import AudioChunk, BackendError


@dataclass
class _Drain:
    done: asyncio.Future[None]


class Player:
    def __init__(self, output_factory: Callable | None = None) -> None:
        self._factory = output_factory
        self._queue: asyncio.Queue[tuple[int, AudioChunk | _Drain | None]] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._interrupt: asyncio.Task | None = None
        self._stream: Any = None
        self._rate: int | None = None
        self._generation = 0
        self._closed = False
        self._error: BackendError | None = None

    async def play(self, chunks: AsyncIterator[AudioChunk]) -> None:
        if self._closed:
            raise RuntimeError("Player is closed")
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())
        generation = self._generation
        async for chunk in chunks:
            if generation != self._generation:
                return
            self._raise_error()
            self._queue.put_nowait((generation, chunk))

    def _raise_error(self) -> None:
        if self._error is not None:
            error, self._error = self._error, None
            raise error

    def _drop_queue(self) -> None:
        while not self._queue.empty():
            _, item = self._queue.get_nowait()
            self._resolve_drain(item)
            self._queue.task_done()

    @staticmethod
    def _resolve_drain(item: Any) -> None:
        if isinstance(item, _Drain) and not item.done.done():
            item.done.set_result(None)

    async def _abort(self, stream: Any, previous: asyncio.Task | None) -> None:
        if previous is not None:
            await previous
        try:
            await asyncio.to_thread(stream.abort)
        except Exception as exc:
            self._error = BackendError(f"Cannot stop audio output: {exc}")

    def stop(self) -> None:
        """Drop queued audio and abort buffered output without blocking the event loop."""
        self._generation += 1
        self._drop_queue()
        if self._stream is not None:
            self._interrupt = asyncio.create_task(self._abort(self._stream, self._interrupt))
        if self._worker is not None:
            self._queue.put_nowait((self._generation, None))

    async def _settle_interrupt(self) -> None:
        if self._interrupt is not None:
            await self._interrupt

    async def _close_stream(self, abort: bool = True) -> None:
        stream = self._stream
        if stream is None:
            return
        await self._settle_interrupt()
        try:
            operation = stream.abort if abort else stream.stop
            await asyncio.to_thread(operation)
            await self._settle_interrupt()
        finally:
            self._stream = None
            self._rate = None
            await asyncio.to_thread(stream.close)

    def _make_stream(self, rate: int) -> Any:
        factory = self._factory
        if factory is None:
            import sounddevice

            factory = sounddevice.RawOutputStream
        stream = factory(samplerate=rate, channels=1, dtype="int16")
        try:
            stream.start()
        except BaseException:
            stream.close()
            raise
        return stream

    async def _open(self, rate: int) -> None:
        if self._stream is not None and self._rate == rate:
            return
        await self._close_stream(abort=False)
        self._stream = await asyncio.to_thread(self._make_stream, rate)
        self._rate = rate

    async def _write(self, generation: int, chunk: AudioChunk) -> None:
        if generation != self._generation:
            return
        await self._open(chunk.sample_rate)
        size = max(2, int(chunk.sample_rate * 0.05) * 2)
        for offset in range(0, len(chunk.pcm), size):
            if generation != self._generation:
                return
            await asyncio.to_thread(self._stream.write, chunk.pcm[offset : offset + size])

    async def _run(self) -> None:
        while True:
            generation, item = await self._queue.get()
            try:
                if isinstance(item, AudioChunk):
                    await self._write(generation, item)
                    continue
                await self._close_stream(abort=item is None)
            except Exception as exc:
                if generation == self._generation:
                    self._error = BackendError(f"Audio playback failed: {exc}")
                    self._drop_queue()
                    with suppress(Exception):
                        await self._close_stream()
            finally:
                self._resolve_drain(item)
                self._queue.task_done()

    async def wait_idle(self) -> None:
        if self._worker is None:
            return
        # Drain PortAudio too: queue completion alone would truncate the final
        # buffered samples when say/voice closes or the sample rate changes.
        done = asyncio.get_running_loop().create_future()
        self._queue.put_nowait((self._generation, _Drain(done)))
        await done
        self._raise_error()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stop()
        await self._queue.join()
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        await self._settle_interrupt()
        await self._close_stream()
