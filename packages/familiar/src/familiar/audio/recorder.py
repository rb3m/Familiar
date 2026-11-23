"""Synchronous recorder; callers run device operations in a worker thread.

PortAudio snapshots the device list when it initialises. If devices change mid-session (a
Bluetooth headset switching to its mic profile, a new default input), opening the stale device
fails with CoreAudio's '!obj'. ``start`` then refreshes the device list and tries once more.
"""

from collections.abc import Callable
from threading import Lock
from typing import Any

from familiar.interfaces import AudioClip


class Recorder:
    def __init__(
        self,
        sample_rate: int = 16000,
        stream_factory: Callable | None = None,
        refresh_devices: Callable[[], None] | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self._factory = stream_factory
        self._refresh = refresh_devices
        self._stream: Any = None
        self._parts: list[bytes] = []
        self._lock = Lock()

    def _append(self, data: Any, *_: Any) -> None:
        with self._lock:
            self._parts.append(bytes(data))

    def start(self) -> None:
        if self._stream is not None:
            return
        with self._lock:
            self._parts.clear()
        try:
            self._open()
        except Exception:
            self._refresh_devices()
            self._open()

    def _open(self) -> None:
        factory = self._factory
        if factory is None:
            import sounddevice

            factory = sounddevice.RawInputStream
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

    def _refresh_devices(self) -> None:
        if self._refresh is not None:
            self._refresh()
            return
        if self._factory is not None:
            return
        import sounddevice

        # sounddevice has no public API for this; re-initialising rereads the device list.
        sounddevice._terminate()
        sounddevice._initialize()

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
