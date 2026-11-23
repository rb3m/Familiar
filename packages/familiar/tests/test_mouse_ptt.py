"""Offline event-tap and setup coverage, including platforms without PyObjC."""

import asyncio
import sys
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from familiar.interfaces import PluginError
from familiar.ptt import key, macos_tap, mouse, setup


@pytest.fixture
def native(monkeypatch):
    stopped = Event()
    q = SimpleNamespace(
        kCGEventKeyDown=10,
        kCGEventKeyUp=11,
        kCGKeyboardEventKeycode=9,
        kCGKeyboardEventAutorepeat=8,
        kCGEventOtherMouseDown=25,
        kCGEventOtherMouseUp=26,
        kCGEventOtherMouseDragged=27,
        kCGEventTapDisabledByTimeout=99,
        kCGEventTapDisabledByUserInput=100,
        kCGMouseEventButtonNumber=1,
        kCGSessionEventTap=1,
        kCGHeadInsertEventTap=0,
        kCGEventTapOptionDefault=0,
        kCGEventFlagMaskShift=0x20000,
        kCGEventFlagMaskControl=0x40000,
        kCGEventFlagMaskAlternate=0x80000,
        kCGEventFlagMaskCommand=0x100000,
        CGEventGetFlags=lambda event: event.get("flags", 0) if isinstance(event, dict) else 0,
        kCFRunLoopCommonModes="common",
        CGEventGetIntegerValueField=lambda event, field: (
            event.get(field, 0) if isinstance(event, dict) else event
        ),
        CGEventTapCreate=Mock(return_value=object()),
        CGEventTapEnable=Mock(),
        CFRunLoopGetCurrent=Mock(return_value=object()),
        CFMachPortCreateRunLoopSource=Mock(return_value=object()),
        CFRunLoopAddSource=Mock(),
        CFRunLoopRemoveSource=Mock(),
        CFMachPortInvalidate=Mock(),
        CFRunLoopRun=stopped.wait,
        CFRunLoopStop=lambda loop: stopped.set(),
    )
    ax = SimpleNamespace(
        AXIsProcessTrusted=Mock(return_value=True),
        AXIsProcessTrustedWithOptions=Mock(),
        kAXTrustedCheckOptionPrompt="prompt",
    )
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(macos_tap, "_prompted", False)
    monkeypatch.setitem(sys.modules, "Quartz", q)
    monkeypatch.setitem(sys.modules, "ApplicationServices", ax)
    return q, ax


@pytest.mark.asyncio
@pytest.mark.parametrize("consume", [True, False])
async def test_mouse_events(native, consume):
    q, _ = native
    ptt = mouse.MousePTT({"button": 4, "consume": consume})
    await ptt._start()
    try:
        callback = q.CGEventTapCreate.call_args.args[4]
        for kind in (25, 26, 27):
            assert callback(None, kind, 3, None) == 3
            assert callback(None, kind, 4, None) == (None if consume else 4)
        await asyncio.sleep(0)
        assert ptt._queue.get_nowait() is True
        assert ptt._queue.get_nowait() is False
        assert ptt._queue.empty()
        assert q.CGEventTapCreate.call_args.args[:3] == (1, 0, 0)
    finally:
        await ptt.aclose()
    q.CFMachPortInvalidate.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", [99, 100])
@pytest.mark.parametrize("backend", ["mouse", "key"])
async def test_disabled_releases_and_reenables(native, disabled, backend):
    q, _ = native
    ptt = {"mouse": mouse.MousePTT({"button": 2}), "key": key.KeyPTT({"key": 105})}[backend]
    await ptt._start()
    try:
        kind, event = (25, 2) if backend == "mouse" else (10, {9: 105})
        ptt._callback(None, kind, event, None)
        assert ptt._callback(None, disabled, None, None) is None
        await asyncio.sleep(0)
        assert ptt._queue.get_nowait() is True
        assert ptt._queue.get_nowait() is False
        q.CGEventTapEnable.assert_called_with(ptt._tap, True)
    finally:
        await ptt.aclose()


@pytest.mark.asyncio
async def test_close_releases(native):
    ptt = mouse.MousePTT({"button": 2})
    await ptt._start()
    thread = ptt._thread
    ptt._callback(None, 25, 2, None)
    await ptt.aclose()
    assert not thread.is_alive()
    assert await ptt._queue.get() is True
    assert await ptt._queue.get() is False
    await ptt.aclose()


def test_permission_prompts_once(native):
    _, ax = native
    ax.AXIsProcessTrusted.return_value = False
    for _ in range(2):
        with pytest.raises(PluginError, match="Terminal or Familiar.app.*Accessibility"):
            mouse.mouse_modules()
    ax.AXIsProcessTrustedWithOptions.assert_called_once_with({"prompt": True})


def test_non_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(PluginError, match="available only on macOS; use evdev or terminal PTT"):
        mouse.mouse_modules()


@pytest.mark.parametrize("button", [0, 1, -1, True, "4", None])
def test_invalid_buttons(button):
    with pytest.raises(PluginError, match="integer >= 2"):
        mouse.MousePTT({"button": button})


@pytest.mark.asyncio
async def test_detector_consumes_spare_buttons(native):
    q, _ = native
    task = asyncio.create_task(setup.detect_mouse_button())
    try:
        while not q.CGEventTapCreate.called:
            await asyncio.sleep(0)
        callback = q.CGEventTapCreate.call_args.args[4]
        for button in (0, 1):
            assert callback(None, 25, button, None) == button
        await asyncio.sleep(0)
        assert not task.done()
        assert callback(None, 27, 4, None) is None
        assert callback(None, 26, 4, None) is None
        assert callback(None, 25, 4, None) is None
        assert await task == 4
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_detector_timeout_cleans_up(native):
    q, _ = native
    with pytest.raises(TimeoutError):
        await setup.detect_mouse_button(0.02)
    q.CFMachPortInvalidate.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["darwin", "linux"])
async def test_dispatcher(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(setup, "detect_mouse_button", AsyncMock(return_value=4))
    monkeypatch.setattr(setup, "detect_button", AsyncMock(return_value=("Stick", "BTN_TRIGGER")))
    result = await setup.detect(timeout=12)
    if platform == "darwin":
        assert result == {"backend": "mouse", "button": 4}
        setup.detect_mouse_button.assert_awaited_once_with(12)
        return
    assert result == {"backend": "evdev", "device": "Stick", "button": "BTN_TRIGGER"}
    setup.detect_button.assert_awaited_once_with(12, kind="joystick")


@pytest.mark.asyncio
async def test_tap_creation_failure(native):
    q, _ = native
    q.CGEventTapCreate.return_value = None
    ptt = mouse.MousePTT({"button": 2})
    with pytest.raises(PluginError, match="Accessibility"):
        await anext(ptt.events())
    assert ptt._thread is None


@pytest.mark.asyncio
async def test_events_iterator_wakes_on_close(native):
    q, _ = native
    ptt = mouse.MousePTT({"button": 2})
    events = ptt.events()
    press = asyncio.create_task(anext(events))
    while not q.CGEventTapCreate.called:
        await asyncio.sleep(0)
    ptt._callback(None, 25, 2, None)
    assert await press is True
    await ptt.aclose()
    assert await anext(events) is False
    with pytest.raises(StopAsyncIteration):
        await anext(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmation", [4, 5])
async def test_cli_mouse_confirmation(monkeypatch, tmp_path, capsys, confirmation):
    from argparse import Namespace

    from familiar.cli import _ptt_setup
    from familiar.config import load_config

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        setup,
        "detect",
        AsyncMock(
            side_effect=[
                {"backend": "mouse", "button": 4},
                {"backend": "mouse", "button": confirmation},
            ]
        ),
    )
    path = tmp_path / "config.toml"
    path.write_text("")
    if confirmation != 4:
        with pytest.raises(SystemExit):
            await _ptt_setup(Namespace(config=str(path)))
        assert path.read_text() == ""
        return
    await _ptt_setup(Namespace(config=str(path)))
    assert load_config(path).ptt.settings == {"button": 4}
    assert "Press the mouse button" in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize("consume", [True, False])
async def test_key_events(native, consume):
    q, _ = native
    ptt = key.KeyPTT({"key": 105, "consume": consume})
    await ptt._start()
    try:
        callback = q.CGEventTapCreate.call_args.args[4]
        assert q.CGEventTapCreate.call_args.args[3] == (1 << 10) | (1 << 11)
        other = {9: 0}
        assert callback(None, 10, other, None) is other
        assert callback(None, 11, other, None) is other
        for kind, event in [(10, {9: 105}), (10, {9: 105}), (10, {9: 105, 8: 1}), (11, {9: 105})]:
            assert callback(None, kind, event, None) == (None if consume else event)
        await asyncio.sleep(0)
        assert ptt._queue.get_nowait() is True
        assert ptt._queue.get_nowait() is False
        assert ptt._queue.empty()
    finally:
        await ptt.aclose()


@pytest.mark.parametrize("code", [0, 1, True, "105", None, 999])
def test_invalid_key(code):
    with pytest.raises(PluginError, match="Only function keys F1-F20 and keypad keys"):
        key.KeyPTT({"key": code})


def test_invalid_key_consume():
    with pytest.raises(PluginError, match="boolean"):
        key.KeyPTT({"key": 105, "consume": 1})


@pytest.mark.asyncio
async def test_key_non_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(PluginError, match="key PTT is available only on macOS"):
        await anext(key.KeyPTT({"key": 105}).events())


@pytest.mark.asyncio
async def test_key_detector(native):
    q, _ = native
    task = asyncio.create_task(setup.detect_key())
    try:
        while not q.CGEventTapCreate.called:
            await asyncio.sleep(0)
        callback = q.CGEventTapCreate.call_args.args[4]
        for kind in (10, 11):
            event = {9: 0}
            assert callback(None, kind, event, None) is event
        assert callback(None, 10, {9: 105, 8: 1}, None) is None
        assert callback(None, 11, {9: 105}, None) is None
        await asyncio.sleep(0)
        assert not task.done()
        assert callback(None, 10, {9: 87}, None) is None
        assert await task == {"key": 87}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_key_detector_timeout(native):
    q, _ = native
    with pytest.raises(TimeoutError):
        await setup.detect_key(0.02)
    q.CFMachPortInvalidate.assert_called_once()


@pytest.mark.asyncio
async def test_key_dispatcher(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    detector = AsyncMock(return_value={"key": 105})
    monkeypatch.setattr(setup, "detect_key", detector)
    assert setup.detectable_kinds() == ("mouse", "key")
    assert await setup.detect("key", 12) == {"backend": "key", "key": 105}
    detector.assert_awaited_once_with(12)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform,kind",
    [("darwin", "joystick"), ("linux", "unknown"), ("win32", None), ("darwin", "unknown")],
)
async def test_unsupported_detection(monkeypatch, platform, kind):
    monkeypatch.setattr(sys, "platform", platform)
    with pytest.raises(PluginError, match="Unsupported"):
        await setup.detect(kind)


@pytest.mark.asyncio
async def test_cli_key_confirmation(monkeypatch, tmp_path, capsys):
    from argparse import Namespace

    from familiar.cli import _ptt_setup
    from familiar.config import load_config

    monkeypatch.setattr(sys, "platform", "darwin")
    detector = AsyncMock(return_value={"backend": "key", "key": 105})
    monkeypatch.setattr(setup, "detect", detector)
    path = tmp_path / "config.toml"
    await _ptt_setup(Namespace(config=str(path), kind="key"))
    assert load_config(path).ptt.settings == {"key": 105}
    assert detector.await_count == 2
    detector.assert_awaited_with(kind="key")
    assert "Press the key" in capsys.readouterr().out


CTRL = 0x40000
SHIFT = 0x20000
FN = 0x800000  # Mac function keys always carry the Fn flag; it must not matter.


@pytest.mark.asyncio
async def test_key_modifiers_match_exactly(native):
    q, _ = native
    ptt = key.KeyPTT({"key": 111, "modifiers": ["control"]})
    await ptt._start()
    try:
        callback = q.CGEventTapCreate.call_args.args[4]
        for flags in (FN, FN | CTRL | SHIFT):
            event = {9: 111, "flags": flags}
            assert callback(None, 10, event, None) is event  # plain/extra-modifier F12 passes
            assert callback(None, 11, event, None) is event  # and so does its key-up
        assert callback(None, 10, {9: 111, "flags": FN | CTRL}, None) is None
        assert callback(None, 11, {9: 111, "flags": FN}, None) is None  # Ctrl let go first
        await asyncio.sleep(0)
        assert ptt._queue.get_nowait() is True
        assert ptt._queue.get_nowait() is False
        assert ptt._queue.empty()
    finally:
        await ptt.aclose()


@pytest.mark.asyncio
async def test_key_detector_records_modifiers(native):
    q, _ = native
    task = asyncio.create_task(setup.detect_key())
    try:
        while not q.CGEventTapCreate.called:
            await asyncio.sleep(0)
        callback = q.CGEventTapCreate.call_args.args[4]
        assert callback(None, 10, {9: 111, "flags": FN | CTRL}, None) is None
        assert await task == {"key": 111, "modifiers": ["control"]}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "settings",
    [
        {"key": 111, "modifiers": "control"},
        {"key": 111, "modifiers": ["hyper"]},
        {"key": 111, "extra": 1},
    ],
)
def test_invalid_key_settings(settings):
    with pytest.raises(PluginError, match="key PTT"):
        key.KeyPTT(settings)


def test_key_label_orders_modifiers():
    assert key.key_label({"key": 111, "modifiers": ["shift", "control"]}) == "Ctrl+Shift+F12"
    assert key.key_label({"key": 105}) == "F13"
