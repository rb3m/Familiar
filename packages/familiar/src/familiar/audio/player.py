"""Ordered PCM playback with bounded writes and interruptible output.

Only the worker task touches the PortAudio stream. Aborting a stream from one thread while
another thread is inside ``write`` can block that write forever (and crash PortAudio on exit),
which once left the companion silent after a barge-in. ``stop()`` therefore only bumps the
generation; the worker notices between 50 ms slices and aborts the stream itself.
"""

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from familiar.audio.pan import pan_stereo
from familiar.interfaces import AudioChunk, BackendError

SLICE_SECONDS = 0.05


def _in_daemon_thread(
    function: Callable, *args: Any, finally_: Callable[[], None] | None = None
) -> asyncio.Future:
    """Run a blocking call on its own daemon thread, then ``finally_`` on that same thread.

    Unlike ``asyncio.to_thread`` a call that never returns cannot exhaust the shared executor or
    keep the process from exiting, and ``finally_`` runs even if the event loop is gone by then.
    """
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def settle(result: Any, error: BaseException | None) -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
            return
        future.set_result(result)

    def target() -> None:
        try:
            outcome = (function(*args), None)
        except BaseException as exc:
            outcome = (None, exc)
        if finally_ is not None:
            finally_()
        with suppress(RuntimeError):  # the loop may be closed by the time a stuck call returns
            loop.call_soon_threadsafe(settle, *outcome)

    threading.Thread(target=target, daemon=True, name="familiar-audio").start()
    return future


def _dispose(stream: Any) -> None:
    for operation in (stream.abort, stream.close):
        with suppress(Exception):
            operation()


@dataclass
class _Drain:
    done: asyncio.Future[None]


class Player:
    def __init__(
        self,
        output_factory: Callable | None = None,
        write_timeout_s: float = 2.0,
        *,
        stereo: bool = False,
    ) -> None:
        self._factory = output_factory
        self._write_timeout = write_timeout_s
        self._stereo = stereo
        self._queue: asyncio.Queue[tuple[int, AudioChunk | _Drain | None, float]] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._stream: Any = None
        self._rate: int | None = None
        self._channels: int | None = None
        self._generation = 0
        self._closed = False
        self._error: BackendError | None = None

    async def play(self, chunks: AsyncIterator[AudioChunk], pan: float = 0.0) -> None:
        if self._closed:
            raise RuntimeError("Player is closed")
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())
        generation = self._generation
        async for chunk in chunks:
            if generation != self._generation:
                return
            self._raise_error()
            self._queue.put_nowait((generation, chunk, pan))

    def _raise_error(self) -> None:
        if self._error is not None:
            error, self._error = self._error, None
            raise error

    def _drop_queue(self) -> None:
        while not self._queue.empty():
            _, item, _ = self._queue.get_nowait()
            self._resolve_drain(item)
            self._queue.task_done()

    @staticmethod
    def _resolve_drain(item: Any) -> None:
        if isinstance(item, _Drain) and not item.done.done():
            item.done.set_result(None)

    def stop(self) -> None:
        """Drop queued audio; the worker aborts buffered output after its current slice."""
        self._generation += 1
        self._drop_queue()
        if self._worker is not None:
            self._queue.put_nowait((self._generation, None, 0.0))

    async def _close_stream(self, abort: bool = True) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            operation = stream.abort if abort else stream.stop
            await asyncio.to_thread(operation)
        finally:
            self._stream = None
            self._rate = None
            self._channels = None
            await asyncio.to_thread(stream.close)

    def _make_stream(self, rate: int, channels: int) -> Any:
        factory = self._factory
        if factory is None:
            import sounddevice

            factory = sounddevice.RawOutputStream
        stream = factory(samplerate=rate, channels=channels, dtype="int16")
        try:
            stream.start()
        except BaseException:
            stream.close()
            raise
        return stream

    async def _open(self, rate: int, channels: int) -> None:
        if self._stream is not None and self._rate == rate and self._channels == channels:
            return
        await self._close_stream(abort=False)
        self._stream = await asyncio.to_thread(self._make_stream, rate, channels)
        self._rate = rate
        self._channels = channels

    async def _write(self, generation: int, chunk: AudioChunk, pan: float) -> None:
        if generation != self._generation:
            return
        channels = 2 if self._stereo else 1
        await self._open(chunk.sample_rate, channels)
        pcm = pan_stereo(chunk.pcm, pan) if self._stereo else chunk.pcm
        size = max(2, int(chunk.sample_rate * SLICE_SECONDS) * 2 * channels)
        for offset in range(0, len(pcm), size):
            if generation != self._generation:
                await self._close_stream(abort=True)  # barge-in: silence now, from this task
                return
            await self._write_slice(pcm[offset : offset + size])

    async def _write_slice(self, data: bytes) -> None:
        """Write one slice; a write that never returns abandons the stream instead of hanging."""
        stream = self._stream
        lock = threading.Lock()
        state = {"finished": False, "abandoned": False}

        def finished() -> None:
            with lock:
                state["finished"] = True
                abandoned = state["abandoned"]
            if abandoned:
                _dispose(stream)

        write = _in_daemon_thread(stream.write, data, finally_=finished)
        done, _ = await asyncio.wait({write}, timeout=self._write_timeout)
        if done:
            write.result()
            return
        # The stuck thread still holds the stream, so closing it now could hang too. Whichever
        # side sees both flags closes it: the thread if the write ever returns, else us.
        self._stream = None
        self._rate = None
        self._channels = None
        with lock:
            state["abandoned"] = True
            returned = state["finished"]
        if returned:
            threading.Thread(target=_dispose, args=(stream,), daemon=True).start()
        write.add_done_callback(lambda f: f.cancelled() or f.exception())  # mark retrieved
        raise BackendError("Audio output stopped responding; reopening it")

    async def _run(self) -> None:
        while True:
            generation, item, pan = await self._queue.get()
            try:
                if isinstance(item, AudioChunk):
                    await self._write(generation, item, pan)
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
        self._queue.put_nowait((self._generation, _Drain(done), 0.0))
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
        await self._close_stream()
