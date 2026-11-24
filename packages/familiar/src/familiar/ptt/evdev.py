"""Linux joystick PTT without exclusive grabs."""

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from familiar.interfaces import PLUGIN_API_VERSION, PluginError

RECONNECT_POLL_S = 2.0


def evdev_module() -> Any:
    if not sys.platform.startswith("linux"):
        raise PluginError("evdev PTT is available only on Linux; use terminal PTT")
    try:
        import evdev
    except ImportError as exc:
        raise PluginError("evdev is required for joystick PTT") from exc
    return evdev


def parse_button(button: str | int, codes: Any = None) -> int:
    codes = codes or evdev_module().ecodes
    if isinstance(button, int):
        return button
    code = getattr(codes, button, None)
    if not isinstance(code, int):
        raise PluginError(f"Unknown evdev button {button!r}")
    return code


def _match_rank(device: Any, name: str, button: int, codes: Any) -> int | None:
    """0 = exact name, 1 = substring, None = no match or the node lacks the button.

    One device can expose several nodes with the same name (e.g. a trackball's keyboard and
    mouse nodes), so the node must also report the configured button.
    """
    device_name = device.name.casefold()
    if name not in device_name:
        return None
    if button not in device.capabilities().get(codes.EV_KEY, []):
        return None
    return int(name != device_name)


def _find_device(module: Any, selector: str, button: int) -> Any:
    if selector.startswith("/dev/input/"):
        try:
            return module.InputDevice(selector)
        except OSError:
            return None
    fallback = None
    name = selector.casefold()
    for path in module.list_devices():
        device = None
        try:
            device = module.InputDevice(path)
            rank = _match_rank(device, name, button, module.ecodes)
        except OSError:
            if device is not None:
                device.close()
            continue
        if rank == 0:
            return device
        if fallback is None and rank == 1:
            fallback = path
        device.close()
    if fallback is None:
        return None
    try:
        return module.InputDevice(fallback)
    except OSError:
        return None


def _permission_hint() -> str:
    if any(not os.access(path, os.R_OK) for path in Path("/dev/input").glob("event*")):
        return "; check /dev/input permissions"
    return ""


class EvdevPTT:
    name = "evdev"
    api_version = PLUGIN_API_VERSION
    capabilities = frozenset()

    def __init__(self, settings: dict) -> None:
        self.settings = settings
        self._device = None

    async def _wait_device(self, module: Any, selector: str, button: int, announce: bool) -> Any:
        device = await asyncio.to_thread(_find_device, module, selector, button)
        if device is not None:
            return device
        if announce:
            self._status("waiting for device" + _permission_hint())
        while device is None:
            await asyncio.sleep(RECONNECT_POLL_S)
            device = await asyncio.to_thread(_find_device, module, selector, button)
        return device

    def _status(self, message: str) -> None:
        print(
            f"PTT {self.settings.get('button', 'BTN_TRIGGER')} on "
            f"{self.settings['device']}: {message}",
            file=sys.stderr,
            flush=True,
        )

    async def _presses(self, module: Any, button: int) -> AsyncIterator[bool]:
        """Press/release of one open device; ends when the device goes away."""
        # A button already down on (re)connect is ignored until released: no phantom recording.
        ignored = button in self._device.active_keys()
        self._status("connected")
        async for event in self._device.async_read_loop():
            if event.type != module.ecodes.EV_KEY or event.code != button:
                continue
            if event.value == 0:
                ignored = False
            if ignored or event.value not in {0, 1}:
                continue
            yield bool(event.value)

    async def events(self) -> AsyncIterator[bool]:
        module = evdev_module()
        selector = self.settings.get("device")
        if not isinstance(selector, str) or not selector:
            raise PluginError("evdev PTT requires a device name or /dev/input path")
        button = parse_button(self.settings.get("button", "BTN_TRIGGER"), module.ecodes)
        announce = True
        try:
            while True:
                self._device = await self._wait_device(module, selector, button, announce)
                announce = False
                held = False
                try:
                    async for held in self._presses(module, button):
                        yield held
                except OSError:
                    pass
                await self.aclose()
                self._status("disconnected; will resume when it is plugged back in")
                if held:
                    yield False
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        device, self._device = self._device, None
        if device is not None:
            await asyncio.to_thread(device.close)
