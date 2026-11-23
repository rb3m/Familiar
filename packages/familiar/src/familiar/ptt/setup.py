"""Detect a push-to-talk button on the current platform."""

import asyncio
import sys
from collections.abc import Callable
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


def _code_name(code: int, codes: Any) -> str:
    name = getattr(codes, "KEY", {}).get(code, codes.BTN.get(code, str(code)))
    if isinstance(name, (tuple, list)):
        return name[0]
    return name


def _keyboard(code: int, codes: Any) -> bool:
    name = _code_name(code, codes)
    return (
        name in {f"KEY_F{number}" for number in range(1, 25)}
        or name.startswith("KEY_KP")
        or name in {"KEY_SCROLLLOCK", "KEY_PAUSE"}
    )


def _mouse(code: int, codes: Any) -> bool:
    return codes.BTN_SIDE <= code <= codes.BTN_TASK


def _mouse_device(code: int, codes: Any) -> bool:
    return code == codes.BTN_LEFT


CodePredicate = Callable[[int, Any], bool]
CODE_PREDICATES = {"joystick": _joystick, "key": _keyboard, "mouse": _mouse}
DEVICE_PREDICATES = {**CODE_PREDICATES, "mouse": _mouse_device}
DEVICE_LABELS = {"joystick": "joysticks", "key": "keyboards", "mouse": "mice"}
PRESS_LABELS = {"joystick": "joystick button", "key": "key", "mouse": "mouse button"}


def _devices(module: Any, accepts: CodePredicate) -> list[Any]:
    devices = []
    for path in module.list_devices():
        device = None
        try:
            device = module.InputDevice(path)
            keys = device.capabilities().get(module.ecodes.EV_KEY, [])
            if any(accepts(code, module.ecodes) for code in keys):
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


async def _press(device: Any, codes: Any, accepts: CodePredicate) -> tuple[str, str]:
    held = _held(device)
    async for event in device.async_read_loop():
        if event.type != codes.EV_KEY or not accepts(event.code, codes):
            continue
        if event.value == 0:
            held.discard(event.code)
            continue
        if event.value != 1 or event.code in held:
            continue
        return device.name, _code_name(event.code, codes)
    raise PluginError("Device disconnected during PTT setup")


async def _first_success(tasks: list[asyncio.Task], timeout: float, kind: str) -> tuple[str, str]:
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
        raise PluginError(f"All {DEVICE_LABELS[kind]} disconnected during PTT setup")
    raise DetectionTimeoutError(f"No {PRESS_LABELS[kind]} pressed within {timeout:g} seconds")


async def detect_button(timeout: float = 30, *, kind: str = "joystick") -> tuple[str, str]:
    module = evdev_module()
    devices = await asyncio.to_thread(_devices, module, DEVICE_PREDICATES[kind])
    if not devices:
        raise PluginError(f"No readable {DEVICE_LABELS[kind]} found; check /dev/input permissions")
    tasks = [
        asyncio.create_task(_press(device, module.ecodes, CODE_PREDICATES[kind]))
        for device in devices
    ]
    try:
        return await _first_success(tasks, timeout, kind)
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


DETECTABLE_KINDS = {"darwin": ("mouse", "key"), "linux": ("joystick", "key", "mouse")}


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
    if sys.platform == "linux":
        device, button = await detect_button(timeout, kind=kind)
        return {"backend": "evdev", "device": device, "button": button}
    if kind == "mouse":
        return {"backend": "mouse", "button": await detect_mouse_button(timeout)}
    return {"backend": "key", **await detect_key(timeout)}
