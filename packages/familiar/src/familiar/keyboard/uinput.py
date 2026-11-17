"""Serialised Linux uinput chords with cancellation-safe key release."""

import asyncio
import os
from collections.abc import Sequence

from familiar.interfaces import PLUGIN_API_VERSION, BackendError


class UinputKeyboard:
    name = "uinput"
    api_version = PLUGIN_API_VERSION
    capabilities = frozenset()

    def __init__(self, settings: dict) -> None:
        try:
            import evdev
        except ImportError as exc:
            raise BackendError("uinput keyboard requires Linux and python-evdev") from exc
        if not os.access("/dev/uinput", os.W_OK):
            raise BackendError("Cannot write /dev/uinput; check Linux uinput device permissions")
        self._module = evdev
        self._device = None
        self._pressed: list[int] = []
        self._lock = asyncio.Lock()
        self._closed = False

    def _release(self) -> None:
        if self._device is None:
            return
        first_error = None
        for code in reversed(self._pressed):
            try:
                self._device.write(self._module.ecodes.EV_KEY, code, 0)
            except Exception as exc:
                first_error = first_error or exc
        self._pressed.clear()
        try:
            self._device.syn()
        except Exception as exc:
            first_error = first_error or exc
        if first_error is not None:
            raise first_error

    async def tap(self, keys: Sequence[str], hold_s: float = 0.05) -> None:
        codes = []
        for key in keys:
            code = getattr(self._module.ecodes, "KEY_" + key.upper(), None)
            if not key or key != key.lower() or not isinstance(code, int):
                raise ValueError(f"Unknown keyboard key: {key!r}")
            if code not in codes:
                codes.append(code)
        async with self._lock:
            if self._closed:
                raise BackendError("uinput keyboard is closed")
            if self._device is None:
                try:
                    self._device = self._module.UInput(name="Familiar keyboard")
                except OSError as exc:
                    raise BackendError(
                        f"Cannot create uinput keyboard; check /dev/uinput: {exc}"
                    ) from exc
                await asyncio.sleep(0.3)
            try:
                for code in codes:
                    self._pressed.append(code)
                    self._device.write(self._module.ecodes.EV_KEY, code, 1)
                self._device.syn()
                await asyncio.sleep(max(0.02, min(3.0, hold_s)))
            finally:
                self._release()

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            try:
                self._release()
            finally:
                if self._device is not None:
                    self._device.close()
                    self._device = None
