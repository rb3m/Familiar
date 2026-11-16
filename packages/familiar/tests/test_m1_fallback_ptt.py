"""Fallback state, configuration, and joystick event filtering."""

import asyncio
from types import SimpleNamespace

import pytest
from familiar.config import load_config
from familiar.fallback import FallbackSTT, FallbackTTS
from familiar.interfaces import AudioChunk, AudioClip, BackendError, ConfigError, PluginError
from familiar.ptt.evdev import EvdevPTT, parse_button
from familiar.ptt.setup import detect_button
from m1_support import run


class Speech:
    def __init__(self, name, error=None, after_output=False):
        self.name = name
        self.error = error
        self.after_output = after_output
        self.calls = []
        self.closed = False

    async def transcribe(self, clip, language):
        self.calls.append(language)
        if self.error:
            raise self.error
        return self.name

    async def synthesize(self, text, voice, language):
        self.calls.append(voice)
        if self.after_output:
            yield AudioChunk(b"\1\0", 24000)
        if self.error:
            raise self.error
        yield AudioChunk(b"\2\0", 24000)

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("retryable,expected_calls", [(True, 2), (False, 1)])
def test_fallback_sticky_and_voice_resolution(retryable, expected_calls, caplog):
    async def scenario():
        primary = Speech("primary", BackendError("failed", retryable=retryable))
        secondary = Speech("secondary")
        tts = FallbackTTS(primary, secondary, lambda name: name + "-voice")
        for _ in range(2):
            result = [c async for c in tts.synthesize("Hi", "ignored", "en")]
            assert result == [AudioChunk(b"\2\0", 24000)]
        assert primary.calls == ["primary-voice"] * expected_calls
        assert secondary.calls == ["secondary-voice"] * 2
        assert len(caplog.records) == 1
        await tts.aclose()
        assert primary.closed and secondary.closed

        primary = Speech("primary", BackendError("failed", retryable=retryable))
        stt = FallbackSTT(primary, Speech("secondary"))
        for _ in range(2):
            assert await stt.transcribe(AudioClip(b"\0\0", 16000), "en") == "secondary"
        assert len(primary.calls) == expected_calls

    run(scenario())


def test_fallback_does_not_restart_after_output():
    async def scenario():
        primary = Speech("primary", BackendError("late failure"), after_output=True)
        secondary = Speech("secondary")
        tts = FallbackTTS(primary, secondary, lambda _: None)
        stream = tts.synthesize("Hi", None, "en")
        assert await anext(stream) == AudioChunk(b"\1\0", 24000)
        with pytest.raises(BackendError, match="late failure"):
            await anext(stream)
        assert secondary.calls == []

    run(scenario())


class Device:
    name = "Fake HOTAS"

    def __init__(self, events=(), keys=(288,), held=()):
        self.events = events
        self.keys = keys
        self.held = list(held)
        self.closed = False

    def active_keys(self):
        return self.held

    def capabilities(self):
        return {1: self.keys}

    async def async_read_loop(self):
        for event in self.events:
            yield SimpleNamespace(type=event[0], code=event[1], value=event[2])

    def close(self):
        self.closed = True


CODES = SimpleNamespace(
    EV_KEY=1,
    BTN_TRIGGER=288,
    BTN_BASE3=294,
    BTN_JOYSTICK=288,
    BTN_THUMBR=318,
    BTN_TRIGGER_HAPPY1=704,
    BTN_TRIGGER_HAPPY40=743,
    BTN={288: "BTN_TRIGGER"},
)


def test_evdev_filter_and_button_parsing(monkeypatch):
    async def scenario():
        device = Device([(2, 288, 1), (1, 294, 1), (1, 288, 2), (1, 288, 1), (1, 288, 0)])
        module = SimpleNamespace(
            ecodes=CODES, list_devices=lambda: ["/dev/input/fake"], InputDevice=lambda _: device
        )
        monkeypatch.setattr("familiar.ptt.evdev.evdev_module", lambda: module)
        ptt = EvdevPTT({"device": "HOTAS", "button": "BTN_TRIGGER"})
        assert [pressed async for pressed in ptt.events()] == [True, False]
        assert device.closed
        assert parse_button(288, CODES) == 288
        assert parse_button("BTN_BASE3", CODES) == 294
        with pytest.raises(PluginError, match="Unknown evdev button"):
            parse_button("NOPE", CODES)

    run(scenario())


def test_setup_skips_unreadable_and_keyboard(monkeypatch):
    async def scenario():
        keyboard = Device(keys=[30])
        joystick = Device([(1, 288, 2), (1, 288, 1)])

        def open_device(path):
            if path == "unreadable":
                raise PermissionError()
            return {"keyboard": keyboard, "joystick": joystick}[path]

        module = SimpleNamespace(
            ecodes=CODES,
            list_devices=lambda: ["unreadable", "keyboard", "joystick"],
            InputDevice=open_device,
        )
        monkeypatch.setattr("familiar.ptt.setup.evdev_module", lambda: module)
        assert await detect_button() == ("Fake HOTAS", "BTN_TRIGGER")
        assert keyboard.closed and joystick.closed

    run(scenario())


def test_setup_ignores_buttons_held_at_start(monkeypatch):
    """A quadrant lever resting in its detent keeps a button down; it must not win setup."""

    async def scenario():
        quadrant = Device([(1, 294, 1), (1, 294, 1)], held=[294])
        hotas = Device([(1, 288, 1)])
        hotas.name = "Real HOTAS"
        devices = {"quadrant": quadrant, "hotas": hotas}
        module = SimpleNamespace(
            ecodes=CODES, list_devices=lambda: list(devices), InputDevice=devices.__getitem__
        )
        monkeypatch.setattr("familiar.ptt.setup.evdev_module", lambda: module)
        assert await detect_button() == ("Real HOTAS", "BTN_TRIGGER")

    run(scenario())


def test_setup_accepts_held_button_after_release(monkeypatch):
    async def scenario():
        device = Device([(1, 288, 0), (1, 288, 1)], held=[288])
        module = SimpleNamespace(
            ecodes=CODES, list_devices=lambda: ["j"], InputDevice=lambda _: device
        )
        monkeypatch.setattr("familiar.ptt.setup.evdev_module", lambda: module)
        assert await detect_button() == ("Fake HOTAS", "BTN_TRIGGER")

    run(scenario())


def test_setup_timeout_closes_devices(monkeypatch):
    async def scenario():
        class SilentDevice(Device):
            async def async_read_loop(self):
                await asyncio.Event().wait()
                yield None

        device = SilentDevice()
        module = SimpleNamespace(
            ecodes=CODES, list_devices=lambda: ["joystick"], InputDevice=lambda _: device
        )
        monkeypatch.setattr("familiar.ptt.setup.evdev_module", lambda: module)
        with pytest.raises(PluginError, match="No joystick button pressed"):
            await detect_button(timeout=0.01)
        assert device.closed

    run(scenario())


def test_config_fallback_ptt(tmp_path):
    path = tmp_path / "voice.toml"
    path.write_text("""
[stt]
backend = "elevenlabs"
fallback = "gemini"
[stt.gemini]
model = "transcribe"
[tts]
backend = "elevenlabs"
fallback = "gemini"
[tts.elevenlabs]
api_key_env = "OTHER_KEY"
[ptt]
backend = "evdev"
[ptt.evdev]
device = "HOTAS"
button = 288
""")
    config = load_config(path)
    assert config.stt.fallback.backend == "gemini"
    assert config.stt.fallback.settings == {"model": "transcribe"}
    assert config.tts.settings == {"api_key_env": "OTHER_KEY"}
    assert config.tts.fallback.backend == "gemini"
    assert config.ptt.settings == {"device": "HOTAS", "button": 288}
    path.write_text("")
    assert load_config(path).ptt.backend == "terminal"
    for invalid in ["fallback = 42", 'fallback = "elevenlabs"', 'fallback = ""']:
        path.write_text('[tts]\nbackend = "elevenlabs"\n' + invalid)
        with pytest.raises(ConfigError):
            load_config(path)


def test_nonretryable_failure_after_audio_uses_secondary_next_time():
    async def scenario():
        primary = Speech("primary", BackendError("quota"), after_output=True)
        secondary = Speech("secondary")
        tts = FallbackTTS(primary, secondary, lambda name: name)
        with pytest.raises(BackendError):
            _ = [c async for c in tts.synthesize("Hi", None, "en")]
        assert [c async for c in tts.synthesize("Again", None, "en")] == [
            AudioChunk(b"\2\0", 24000)
        ]
        assert primary.calls == ["primary"]

    run(scenario())


def test_terminal_enter_toggle_and_eof(monkeypatch, capsys):
    import io

    from familiar.ptt.terminal import TerminalPTT

    async def scenario():
        monkeypatch.setattr("sys.stdin", io.StringIO("\n\n"))
        ptt = TerminalPTT({})
        assert [pressed async for pressed in ptt.events()] == [True, False]
        await ptt.aclose()

    run(scenario())
    assert "Enter" in capsys.readouterr().out
