"""Voice orchestration without microphones, speakers, or vendor calls."""

import asyncio
from types import SimpleNamespace

import pytest
from familiar.conversation import Conversation
from familiar.interfaces import AudioChunk, AudioClip, BackendError, LLMChunk
from familiar.voice import VoiceSession
from m1_support import run


class Log:
    def __init__(self):
        self.records = []

    def record(self, kind, **fields):
        self.records.append((kind, fields))


class LLM:
    def __init__(self):
        self.closed = False

    async def stream(self, *args):
        yield LLMChunk("First sentence. Second sentence.")

    async def aclose(self):
        self.closed = True


class Recorder:
    def __init__(self, duration=1):
        self.duration = duration
        self.starts = 0
        self.stops = 0

    def start(self):
        self.starts += 1

    def stop(self):
        self.stops += 1
        return AudioClip(b"\0\0" * int(16000 * self.duration), 16000)


class Player:
    def __init__(self):
        self.audio = []
        self.stops = 0
        self.closed = False

    def stop(self):
        self.stops += 1

    async def play(self, chunks, pan=0.0):
        async for chunk in chunks:
            self.audio.append(chunk.pcm.decode())

    async def wait_idle(self):
        pass

    async def aclose(self):
        self.closed = True


class STT:
    def __init__(self, text="Hello"):
        self.text = text
        self.calls = 0
        self.closed = False

    async def transcribe(self, *args):
        self.calls += 1
        if isinstance(self.text, Exception):
            raise self.text
        return self.text

    async def aclose(self):
        self.closed = True


class TTS:
    name = "fake"

    def __init__(self):
        self.closed = False

    async def synthesize(self, text, voice, language):
        assert voice == "fake-voice"
        yield AudioChunk(text.encode(), 24000)

    async def aclose(self):
        self.closed = True


class PTT:
    def __init__(self):
        self.closed = False

    async def events(self):
        yield True
        yield False

    async def aclose(self):
        self.closed = True


def make_session(**overrides):
    persona = SimpleNamespace(
        display_name="Test",
        voice=lambda *args: "fake-voice",
        system_prompt=lambda _language, _game=None: "prompt",
    )
    log = Log()
    conversation = Conversation(LLM(), persona, "en", log)
    values = dict(
        ptt=PTT(),
        recorder=Recorder(),
        player=Player(),
        stt=STT(),
        tts=TTS(),
        conversation=conversation,
        persona=persona,
        language="en",
        transcript=log,
    )
    values.update(overrides)
    return VoiceSession(**values)


def test_voice_order_and_timings():
    async def scenario():
        session = make_session()
        await session.run()
        assert session.player.audio == ["First sentence.", "Second sentence."]
        timings = [fields for kind, fields in session.transcript.records if kind == "timing"]
        assert len(timings) == 1
        assert all(value is not None and value >= 0 for value in timings[0].values())
        assert timings[0]["first_audio_ms"] >= timings[0]["first_sentence_ms"]
        assert session.player.closed and session.ptt.closed
        assert session.stt.closed and session.tts.closed and session.conversation.llm.closed

    run(scenario())


def test_voice_short_clip_and_empty_transcript():
    async def scenario():
        short = make_session(recorder=Recorder(duration=0.2))
        await short.run()
        assert short.stt.calls == 0
        assert short.player.audio == []
        empty = make_session(stt=STT("  "))
        await empty.run()
        assert empty.player.audio == []
        assert any(kind == "timing" for kind, _ in empty.transcript.records)

    run(scenario())


def test_voice_barge_in_cancels_reply():
    async def scenario():
        started = asyncio.Event()
        cancelled = asyncio.Event()

        class SlowTTS(TTS):
            async def synthesize(self, text, voice, language):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
                yield AudioChunk(b"unused", 24000)

        class BargePTT(PTT):
            async def events(self):
                yield True
                yield False
                await asyncio.wait_for(started.wait(), 2)
                yield True
                assert cancelled.is_set()

        session = make_session(ptt=BargePTT(), tts=SlowTTS())
        await session.run()
        assert session.recorder.starts == 2
        assert session.player.stops >= 2
        assert session.player.audio == []
        assert cancelled.is_set()
        assert any(
            kind == "assistant" and fields.get("interrupted")
            for kind, fields in session.transcript.records
        )

    run(scenario())


def test_voice_backend_error_keeps_session_alive(capsys):
    async def scenario():
        class RetryPTT(PTT):
            async def events(self):
                yield True
                yield False
                await session._reply_task
                yield True
                yield False

        session = make_session(ptt=RetryPTT(), stt=STT(BackendError("busy", retryable=True)))
        await session.run()
        assert session.stt.calls == 2
        assert session.recorder.starts == 2

    run(scenario())
    assert "Try speaking again" in capsys.readouterr().err


def test_voice_microphone_failure_keeps_session_alive(capsys):
    class FlakyRecorder(Recorder):
        def start(self):
            super().start()
            if self.starts == 1:
                raise OSError("Internal PortAudio error")

    async def scenario():
        class TwicePTT(PTT):
            async def events(self):
                for pressed in (True, False, True, False):
                    yield pressed

        session = make_session(ptt=TwicePTT(), recorder=FlakyRecorder())
        await session.run()
        assert session.recorder.starts == 2
        assert session.player.audio == ["First sentence.", "Second sentence."]

    run(scenario())
    assert "microphone unavailable" in capsys.readouterr().out


def test_sentence_audio_diagnostics(caplog):
    async def scenario():
        class DiagnosticTTS(TTS):
            async def synthesize(self, text, voice, language):
                if text == "Missing audio.":
                    return
                if text == "Short audio.":
                    yield AudioChunk(b"00", 24000)
                    return
                yield AudioChunk(b"00" * 12000, 24000)
                yield AudioChunk(b"00" * 24000, 48000)

        async def sentences():
            for text in ("Missing audio.", "Short audio.", "Normal audio."):
                yield text

        session = make_session(tts=DiagnosticTTS())
        timings = dict.fromkeys(("first_sentence_ms", "first_audio_ms"))
        await session._speak(sentences(), 0, timings)

    run(scenario())
    warnings = [
        record.message for record in caplog.records if "Short sentence audio" in record.message
    ]
    assert len(warnings) == 2
    assert "speaker=test chars=14 seconds=0.000 text=Missing audio." in warnings[0]
    assert "Short audio." in warnings[1]


@pytest.mark.parametrize("markers", [None, "0", "1"])
@pytest.mark.parametrize("backend", [None, "local"])
def test_voice_markers(monkeypatch, capsys, markers, backend):
    monkeypatch.delenv("FAMILIAR_VOICE_MARKERS", raising=False)
    if markers is not None:
        monkeypatch.setenv("FAMILIAR_VOICE_MARKERS", markers)

    async def scenario():
        session = make_session()
        session.tts.last_backend = backend
        await session.run()
        assert len(session.player.audio) == 2

    run(scenario())
    output = capsys.readouterr().out
    expected = f"[voice] {backend or 'fake'}"
    assert output.count(expected) == (2 if markers == "1" else 0)


@pytest.mark.parametrize("empty_chunk", [False, True])
def test_voice_no_marker_without_audio(monkeypatch, capsys, empty_chunk):
    monkeypatch.setenv("FAMILIAR_VOICE_MARKERS", "1")

    class EmptyTTS(TTS):
        async def synthesize(self, *args):
            if empty_chunk:
                yield AudioChunk(b"", 24000)

    run(make_session(tts=EmptyTTS()).run())
    assert "[voice]" not in capsys.readouterr().out
