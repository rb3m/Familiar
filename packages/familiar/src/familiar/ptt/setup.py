"""Detect a push-to-talk button on the current platform."""

import asyncio
import sys
from typing import Any

from familiar.interfaces import PluginError

from .evdev import evdev_module


class DetectionTimeoutError(PluginError, TimeoutError):
    """No button arrived before the setup deadline."""


def _joystick(code: int, codes: Any) -> bool:
    return (
        codes.BTN_JOYSTICK <= code <= codes.BTN_THUMBR
        or codes.BTN_TRIGGER_HAPPY1 <= code <= codes.BTN_TRIGGER_HAPPY40
    )


def _devices(module: Any) -> list[Any]:
    devices = []
    for path in module.list_devices():
        device = None
        try:
            device = module.InputDevice(path)
            keys = device.capabilities().get(module.ecodes.EV_KEY, [])
            if any(_joystick(code, module.ecodes) for code in keys):
                devices.append(device)
                continue
        except OSError:
            pass
        if device is not None:
            device.close()
    return devices


def _held(device: Any) -> set[int]:
    """Buttons already down when setup starts (e.g. a quadrant lever resting in its detent)."""
    try:
        return set(device.active_keys())
    except OSError:
        return set()


async def _press(device: Any, codes: Any) -> tuple[str, str]:
    held = _held(device)
    async for event in device.async_read_loop():
        if event.type != codes.EV_KEY or not _joystick(event.code, codes):
            continue
        if event.value == 0:
            held.discard(event.code)
            continue
        if event.value != 1 or event.code in held:
            continue
        name = codes.BTN.get(event.code, str(event.code))
        if isinstance(name, (tuple, list)):
            name = name[0]
        return device.name, name
    raise PluginError("Joystick disconnected during PTT setup")


async def _first_success(tasks: list[asyncio.Task], timeout: float) -> tuple[str, str]:
    """First device to report a press wins; a device that drops out doesn't end setup."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    pending = set(tasks)
    while pending:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        done, pending = await asyncio.wait(
            pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
        )
        winners = [task for task in done if task.exception() is None]
        if winners:
            return winners[0].result()
    if not pending:
        raise PluginError("All joysticks disconnected during PTT setup")
    raise DetectionTimeoutError(f"No joystick button pressed within {timeout:g} seconds")


async def detect_button(timeout: float = 30) -> tuple[str, str]:
    module = evdev_module()
    devices = await asyncio.to_thread(_devices, module)
    if not devices:
        raise PluginError("No readable joysticks found; check /dev/input permissions")
    tasks = [asyncio.create_task(_press(device, module.ecodes)) for device in devices]
    try:
        return await _first_success(tasks, timeout)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(asyncio.to_thread(device.close) for device in devices))


async def detect_mouse_button(timeout: float = 30) -> int:
    """Consume spare mouse buttons while waiting for a press."""
    from .mouse import MousePTT

    class Detector(MousePTT):
        def classify(self, kind: int, event: Any) -> tuple[bool | None, bool]:
            q = self._quartz
            button = q.CGEventGetIntegerValueField(event, q.kCGMouseEventButtonNumber)
            if button < 2:
                return None, False
            if kind == q.kCGEventOtherMouseDown:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, button)
            return None, True

    detector = Detector({"button": 2})
    try:
        async with asyncio.timeout(timeout):
            await detector._start()
            button = await detector._queue.get()
            if button is None:
                raise PluginError("Mouse detector stopped before a button was pressed")
            return button
    except TimeoutError:
        raise DetectionTimeoutError(f"No mouse button pressed within {timeout:g} seconds") from None
    finally:
        await detector.aclose()


async def detect_key(timeout: float = 30) -> dict:
    """Consume allowed keys while waiting for a fresh press; returns key and held modifiers."""
    from .key import ALLOWED_KEYS, KeyPTT, held_modifiers

    class Detector(KeyPTT):
        def classify(self, kind: int, event: Any) -> tuple[bool | None, bool]:
            q = self._quartz
            key = q.CGEventGetIntegerValueField(event, q.kCGKeyboardEventKeycode)
            if key not in ALLOWED_KEYS:
                return None, False
            if kind == q.kCGEventKeyDown and not q.CGEventGetIntegerValueField(
                event, q.kCGKeyboardEventAutorepeat
            ):
                pressed = {"key": key}
                modifiers = held_modifiers(q, event)
                if modifiers:
                    pressed["modifiers"] = modifiers
                self._loop.call_soon_threadsafe(self._queue.put_nowait, pressed)
            return None, True

    detector = Detector({"key": 105})
    try:
        async with asyncio.timeout(timeout):
            await detector._start()
            pressed = await detector._queue.get()
            if pressed is None:
                raise PluginError("Key detector stopped before a key was pressed")
            return pressed
    except TimeoutError:
        raise DetectionTimeoutError(f"No key pressed within {timeout:g} seconds") from None
    finally:
        await detector.aclose()


DETECTABLE_KINDS = {"darwin": ("mouse", "key"), "linux": ("joystick",)}


def detectable_kinds() -> tuple[str, ...]:
    """Return detection kinds supported on this platform."""
    return DETECTABLE_KINDS.get(sys.platform, ())


async def detect(kind: str | None = None, timeout: float = 30) -> dict:
    """Detect a platform-appropriate push-to-talk control."""
    kinds = detectable_kinds()
    if kind is None and kinds:
        kind = kinds[0]
    if kind not in kinds:
        raise PluginError(f"Unsupported PTT detection kind: {kind}")
    if kind == "mouse":
        return {"backend": "mouse", "button": await detect_mouse_button(timeout)}
    if kind == "key":
        return {"backend": "key", **await detect_key(timeout)}
    device, button = await detect_button(timeout)
    return {"backend": "evdev", "device": device, "button": button}
