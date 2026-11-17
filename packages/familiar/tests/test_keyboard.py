"""uinput contract using a fake evdev module, never a real device."""

import asyncio
import sys
from types import SimpleNamespace

import pytest
from familiar.interfaces import BackendError, Keyboard
from familiar.keyboard.uinput import UinputKeyboard


@pytest.fixture
def fake_evdev(monkeypatch):
    class Device:
        def __init__(self, **kwargs):
            self.events = []
            self.closed = False
            self.fail = False

        def write(self, kind, code, value):
            self.events.append((code, value))
            if self.fail and value == 1 and code == 24:
                self.fail = False
                raise OSError("write failed")

        def syn(self):
            self.events.append("syn")

        def close(self):
            self.closed = True

    devices = []

    def create(**kwargs):
        device = Device(**kwargs)
        devices.append(device)
        return device

    module = SimpleNamespace(
        ecodes=SimpleNamespace(EV_KEY=1, KEY_O=24, KEY_LEFTCTRL=29), UInput=create
    )
    monkeypatch.setitem(sys.modules, "evdev", module)
    monkeypatch.setattr("familiar.keyboard.uinput.os.access", lambda *args: True)
    return module, devices


@pytest.mark.asyncio
async def test_chord_order_clamp_lazy_and_close(fake_evdev, monkeypatch):
    _, devices = fake_evdev
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("familiar.keyboard.uinput.asyncio.sleep", sleep)
    keyboard = UinputKeyboard({})
    assert isinstance(keyboard, Keyboard)
    assert not devices
    await keyboard.tap(["leftctrl", "o"], -1)
    assert sleeps == [0.3, 0.02]
    assert devices[0].events == [(29, 1), (24, 1), "syn", (24, 0), (29, 0), "syn"]
    await keyboard.tap(["o"], 20)
    assert sleeps[-1] == 3.0
    assert len(devices) == 1
    await keyboard.aclose()
    assert devices[0].closed
    await keyboard.aclose()
    with pytest.raises(BackendError, match="closed"):
        await keyboard.tap(["o"])


@pytest.mark.asyncio
async def test_unknown_key_before_device_creation(fake_evdev):
    _, devices = fake_evdev
    keyboard = UinputKeyboard({})
    for key in ("missing", "KEY_O", "O", ""):
        with pytest.raises(ValueError):
            await keyboard.tap([key])
    assert not devices


@pytest.mark.asyncio
async def test_release_on_write_failure(fake_evdev, monkeypatch):
    _, devices = fake_evdev

    async def sleep(seconds):
        pass

    monkeypatch.setattr("familiar.keyboard.uinput.asyncio.sleep", sleep)
    keyboard = UinputKeyboard({})
    await keyboard.tap(["o"])
    device = devices[0]
    device.events.clear()
    device.fail = True
    with pytest.raises(OSError):
        await keyboard.tap(["leftctrl", "o"])
    assert device.events == [(29, 1), (24, 1), (24, 0), (29, 0), "syn"]
    await keyboard.aclose()
    assert device.closed


@pytest.mark.asyncio
async def test_cancellation_releases_chord(fake_evdev, monkeypatch):
    _, devices = fake_evdev
    holding = asyncio.Event()

    async def sleep(seconds):
        if seconds == 0.3:
            return
        holding.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("familiar.keyboard.uinput.asyncio.sleep", sleep)
    keyboard = UinputKeyboard({})
    task = asyncio.create_task(keyboard.tap(["leftctrl", "o"]))
    await holding.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert devices[0].events[-3:] == [(24, 0), (29, 0), "syn"]
    await keyboard.aclose()
    assert devices[0].closed


@pytest.mark.asyncio
async def test_serialises_chords(fake_evdev, monkeypatch):
    _, devices = fake_evdev
    original_sleep = asyncio.sleep

    async def sleep(seconds):
        await original_sleep(0)

    monkeypatch.setattr("familiar.keyboard.uinput.asyncio.sleep", sleep)
    keyboard = UinputKeyboard({})
    await asyncio.gather(keyboard.tap(["o"]), keyboard.tap(["leftctrl"]))
    assert devices[0].events == [(24, 1), "syn", (24, 0), "syn", (29, 1), "syn", (29, 0), "syn"]
    await keyboard.aclose()


def test_unavailable_evdev(monkeypatch):
    monkeypatch.setitem(sys.modules, "evdev", None)
    with pytest.raises(BackendError, match="python-evdev"):
        UinputKeyboard({})


def test_unwritable_uinput(fake_evdev, monkeypatch):
    monkeypatch.setattr("familiar.keyboard.uinput.os.access", lambda *args: False)
    with pytest.raises(BackendError, match="/dev/uinput"):
        UinputKeyboard({})


@pytest.mark.asyncio
async def test_device_creation_failure(fake_evdev, monkeypatch):
    module, _ = fake_evdev

    def fail(**kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(module, "UInput", fail)
    keyboard = UinputKeyboard({})
    with pytest.raises(BackendError, match="/dev/uinput"):
        await keyboard.tap(["o"])
    await keyboard.aclose()
