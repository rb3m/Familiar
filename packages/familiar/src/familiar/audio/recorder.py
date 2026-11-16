"""Synchronous recorder; callers run device operations in a worker thread."""

from collections.abc import Callable
from threading import Lock
from typing import Any

from familiar.interfaces import AudioClip


class Recorder:
    def __init__(self, sample_rate: int = 16000, stream_factory: Callable | None = None) -> None:
        self.sample_rate = sample_rate
        self._factory = stream_factory
        self._stream: Any = None
        self._parts: list[bytes] = []
        self._lock = Lock()

    def _append(self, data: Any, *_: Any) -> None:
        with self._lock:
            self._parts.append(bytes(data))

    def start(self) -> None:
        if self._stream is not None:
            return
        factory = self._factory
        if factory is None:
            import sounddevice

            factory = sounddevice.RawInputStream
        with self._lock:
            self._parts.clear()
        stream = factory(
            samplerate=self.sample_rate, channels=1, dtype="int16", callback=self._append
        )
        self._stream = stream
        try:
            stream.start()
        except BaseException:
            self._stream = None
            stream.close()
            raise

    def stop(self) -> AudioClip:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()
        with self._lock:
            pcm = b"".join(self._parts)
            self._parts.clear()
        return AudioClip(pcm, self.sample_rate)
