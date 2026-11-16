"""Linux joystick PTT without exclusive grabs."""

import asyncio
import sys
from collections.abc import AsyncIterator
from typing import Any

from familiar.interfaces import PLUGIN_API_VERSION, PluginError


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


def _find_device(module: Any, selector: str) -> Any:
    if selector.startswith("/dev/input/"):
        try:
            return module.InputDevice(selector)
        except OSError as exc:
            raise PluginError(
                f"Cannot read {selector}; check /dev/input permissions: {exc}"
            ) from exc
    for path in module.list_devices():
        try:
            device = module.InputDevice(path)
        except OSError:
            continue
        if selector.casefold() in device.name.casefold():
            return device
        device.close()
    raise PluginError(f"PTT device {selector!r} not found/readable; check /dev/input permissions")


class EvdevPTT:
    name = "evdev"
    api_version = PLUGIN_API_VERSION
    capabilities = frozenset()

    def __init__(self, settings: dict) -> None:
        self.settings = settings
        self._device = None

    async def events(self) -> AsyncIterator[bool]:
        module = evdev_module()
        selector = self.settings.get("device")
        if not isinstance(selector, str) or not selector:
            raise PluginError("evdev PTT requires a device name or /dev/input path")
        button = parse_button(self.settings.get("button", "BTN_TRIGGER"), module.ecodes)
        self._device = await asyncio.to_thread(_find_device, module, selector)
        try:
            async for event in self._device.async_read_loop():
                if event.type != module.ecodes.EV_KEY or event.code != button:
                    continue
                if event.value in {0, 1}:
                    yield bool(event.value)
        except OSError as exc:
            raise PluginError(
                f"PTT device disconnected; check /dev/input permissions: {exc}"
            ) from exc
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        device, self._device = self._device, None
        if device is not None:
            await asyncio.to_thread(device.close)
