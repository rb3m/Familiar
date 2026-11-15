"""Detect a pressed button on readable joystick devices."""

import asyncio
from typing import Any

from familiar.interfaces import PluginError

from .evdev import evdev_module


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
    raise PluginError(f"No joystick button pressed within {timeout:g} seconds")


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
