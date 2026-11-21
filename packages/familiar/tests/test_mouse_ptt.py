"""Offline event-tap and setup coverage, including platforms without PyObjC."""

import asyncio
import sys
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from familiar.interfaces import PluginError
from familiar.ptt import mouse, setup


@pytest.fixture
def native(monkeypatch):
    stopped = Event()
    q = SimpleNamespace(
        kCGEventOtherMouseDown=25,
        kCGEventOtherMouseUp=26,
        kCGEventOtherMouseDragged=27,
        kCGEventTapDisabledByTimeout=99,
        kCGEventTapDisabledByUserInput=100,
        kCGMouseEventButtonNumber=1,
        kCGSessionEventTap=1,
        kCGHeadInsertEventTap=0,
        kCGEventTapOptionDefault=0,
        kCFRunLoopCommonModes="common",
        CGEventGetIntegerValueField=lambda event, field: event,
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
    monkeypatch.setattr(mouse, "_prompted", False)
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
async def test_disabled_releases_and_reenables(native, disabled):
    q, _ = native
    ptt = mouse.MousePTT({"button": 2})
    await ptt._start()
    try:
        ptt._callback(None, 25, 2, None)
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
    result = await setup.detect(12)
    if platform == "darwin":
        assert result == {"backend": "mouse", "button": 4}
        setup.detect_mouse_button.assert_awaited_once_with(12)
        return
    assert result == {"backend": "evdev", "device": "Stick", "button": "BTN_TRIGGER"}
    setup.detect_button.assert_awaited_once_with(12)


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
