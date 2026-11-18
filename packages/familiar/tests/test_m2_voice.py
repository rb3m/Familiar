"""Game integration without audio hardware, network, or a real game plugin."""

import asyncio
from collections.abc import AsyncIterator

import pytest
from familiar.conversation import Conversation
from familiar.director import Director
from familiar.interfaces import AudioChunk, AudioClip, GameEvent, LLMChunk, ToolSpec
from familiar.voice import VoiceSession
from m1_support import run
from test_m2_director import Log, event, persona


class FakeGameAdapter:
    name = "fake"
    game = "ats"
    api_version = "2.0"
    capabilities = frozenset()

    def __init__(self, settings: dict) -> None:
        self.queue: asyncio.Queue[GameEvent] = asyncio.Queue()
        self.connected = True
        self.paused = False
        self.closed = False
        self.snapshots = 0

    async def events(self) -> AsyncIterator[GameEvent]:
        while True:
            yield await self.queue.get()

    async def snapshot(self) -> dict:
        self.snapshots += 1
        return {"connected": self.connected, "paused": self.paused, "driving": True}

    def describe(self) -> str:
        return "Fuel: 20 L"

    def tools(self) -> list[ToolSpec]:
        return []

    async def aclose(self) -> None:
        self.closed = True


class TrackingDirector(Director):
    def __init__(self) -> None:
        super().__init__(0.5, {})
        self.changes: list[tuple[str, bool]] = []
        self.spoken = 0

    def set_busy(self, reason: str, busy: bool) -> None:
        self.changes.append((reason, busy))
        super().set_busy(reason, busy)

    def notify_spoke(self) -> None:
        self.spoken += 1
        super().notify_spoke()


class PTT:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bool | None] = asyncio.Queue()

    async def events(self) -> AsyncIterator[bool]:
        while True:
            pressed = await self.queue.get()
            if pressed is None:
                return
            yield pressed


class Recorder:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    def start(self) -> None:
        # Recording is performed in a thread; state is polled from the event loop.
        self.started_flag = True

    def stop(self) -> AudioClip:
        return AudioClip(b"\x00" * 16000, 16000)


class STT:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.fail = False

    async def transcribe(self, audio, language) -> str:
        self.started.set()
        await self.release.wait()
        if self.fail:
            from familiar.interfaces import BackendError

            raise BackendError("transcription failed")
        return "question"


class LLM:
    async def stream(self, messages, system, tools) -> AsyncIterator[LLMChunk]:
        text = messages[-1].text
        reply = "Answer."
        if "landmark" in text:
            reply = "Landmark."
        if "danger" in text:
            reply = "Critical."
        yield LLMChunk(reply)


class TTS:
    name = "fake"

    def __init__(self, blocked: str | None = None) -> None:
        self.blocked = blocked
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def synthesize(self, text, voice, language) -> AsyncIterator[AudioChunk]:
        if text == self.blocked:
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        yield AudioChunk(text.encode(), 24000)


class Player:
    def __init__(self) -> None:
        self.audio: list[str] = []
        self.pans: list[float] = []
        self.played = asyncio.Event()
        self.stops = 0

    def stop(self) -> None:
        self.stops += 1

    async def play(self, chunks, pan: float = 0.0) -> None:
        async for chunk in chunks:
            self.audio.append(chunk.pcm.decode())
        self.pans.append(pan)
        self.played.set()

    async def wait_idle(self) -> None:
        pass


def session(blocked: str | None = None) -> VoiceSession:
    log = Log()
    character = persona()
    return VoiceSession(
        PTT(),
        Recorder(),
        Player(),
        STT(),
        TTS(blocked),
        Conversation(LLM(), character, "en", log),
        character,
        "en",
        log,
        adapter=FakeGameAdapter({}),
        director=TrackingDirector(),
    )


async def until(predicate) -> None:
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


def test_each_speaker_plays_at_their_seat() -> None:
    class NamingSTT(STT):
        async def transcribe(self, audio, language) -> str:
            return "Other, what do you think?"

    async def scenario() -> None:
        from dataclasses import replace

        from familiar.crew import Crew

        log = Log()
        carrier = persona()
        other = replace(carrier, id="other", display_name="Other")
        voice = VoiceSession(
            PTT(),
            Recorder(),
            Player(),
            NamingSTT(),
            TTS(),
            Conversation(LLM(), carrier, "en", log, crew=Crew([carrier, other], "test")),
            carrier,
            "en",
            log,
            seats={"test": 0.0, "other": -0.6},
        )
        task = asyncio.create_task(voice.run())
        await voice.ptt.queue.put(True)
        await until(lambda: voice._recording)
        await voice.ptt.queue.put(False)
        await until(lambda: voice.player.audio == ["Answer."])
        assert voice.player.pans == [-0.6]
        await finish(voice, task)

    run(scenario())


async def finish(voice: VoiceSession, task: asyncio.Task) -> None:
    await voice.ptt.queue.put(None)
    await asyncio.wait_for(task, 2)


def test_event_speaks_and_closes(capsys) -> None:
    async def scenario() -> None:
        voice = session()
        task = asyncio.create_task(voice.run())
        await voice.adapter.queue.put(event("landmark"))
        await until(lambda: voice.director.spoken == 1)
        assert voice.player.audio == ["Landmark."]
        assert voice.adapter.snapshots >= 2
        assert not voice.director._busy
        assert ("speaking", True) in voice.director.changes
        assert ("speaking", False) in voice.director.changes
        await finish(voice, task)
        assert voice.adapter.closed

    run(scenario())
    assert "Test (event: landmark): Landmark." in capsys.readouterr().out


def test_ptt_cancels_interjection_and_holds_through_transcription() -> None:
    async def scenario() -> None:
        voice = session("Landmark.")
        task = asyncio.create_task(voice.run())
        await voice.adapter.queue.put(event("landmark"))
        await asyncio.wait_for(voice.tts.started.wait(), 2)
        await voice.ptt.queue.put(True)
        await until(lambda: voice._recording)
        assert voice.tts.cancelled.is_set()
        assert "user" in voice.director._busy
        assert "speaking" not in voice.director._busy
        assert voice.director.spoken == 0
        await voice.ptt.queue.put(False)
        await asyncio.wait_for(voice.stt.started.wait(), 2)
        await voice.adapter.queue.put(event("danger", "critical"))
        await asyncio.sleep(0.02)
        assert voice.player.audio == []
        assert "user" in voice.director._busy
        voice.stt.release.set()
        await until(lambda: voice.player.audio == ["Answer.", "Critical."])
        await until(lambda: not voice.director._busy)
        await finish(voice, task)

    run(scenario())


def test_critical_preempts_notable_interjection() -> None:
    async def scenario() -> None:
        voice = session("Landmark.")
        task = asyncio.create_task(voice.run())
        await voice.adapter.queue.put(event("landmark"))
        await asyncio.wait_for(voice.tts.started.wait(), 2)
        await voice.adapter.queue.put(event("danger", "critical"))
        await until(lambda: voice.player.audio == ["Critical."])
        assert voice.tts.cancelled.is_set()
        assert voice.player.stops >= 1
        await finish(voice, task)

    run(scenario())


def test_critical_waits_for_player_reply() -> None:
    async def scenario() -> None:
        voice = session("Answer.")
        voice.stt.release.set()
        task = asyncio.create_task(voice.run())
        await voice.ptt.queue.put(True)
        await until(lambda: voice._recording)
        await voice.ptt.queue.put(False)
        await asyncio.wait_for(voice.tts.started.wait(), 2)
        await voice.adapter.queue.put(event("danger", "critical"))
        await asyncio.sleep(0.02)
        assert voice.player.audio == []
        assert not voice.tts.cancelled.is_set()
        voice.tts.release.set()
        await until(lambda: voice.player.audio == ["Answer.", "Critical."])
        await finish(voice, task)

    run(scenario())


@pytest.mark.parametrize("fail", [False, True])
def test_user_busy_clears_after_transcription(fail: bool) -> None:
    async def scenario() -> None:
        voice = session()
        voice.stt.fail = fail
        voice.stt.release.set()
        task = asyncio.create_task(voice.run())
        await voice.ptt.queue.put(True)
        await until(lambda: voice._recording)
        await voice.ptt.queue.put(False)
        await until(lambda: voice._reply_task is not None and voice._reply_task.done())
        assert not voice.director._busy
        assert ("user", True) in voice.director.changes
        assert ("user", False) in voice.director.changes
        await finish(voice, task)

    run(scenario())


def test_critical_does_not_preempt_another_critical() -> None:
    async def scenario() -> None:
        voice = session("Critical.")
        task = asyncio.create_task(voice.run())
        await voice.adapter.queue.put(event("danger", "critical"))
        await voice.adapter.queue.put(event("danger2", "critical"))
        await asyncio.wait_for(voice.tts.started.wait(), 2)
        await asyncio.sleep(0.02)
        assert not voice.tts.cancelled.is_set()
        voice.tts.release.set()
        await until(lambda: voice.player.audio == ["Critical.", "Critical."])
        await finish(voice, task)

    run(scenario())


def test_ptt_during_player_reply_keeps_user_busy() -> None:
    async def scenario() -> None:
        voice = session("Answer.")
        voice.stt.release.set()
        task = asyncio.create_task(voice.run())
        await voice.ptt.queue.put(True)
        await until(lambda: voice._recording)
        await voice.ptt.queue.put(False)
        await asyncio.wait_for(voice.tts.started.wait(), 2)
        await voice.ptt.queue.put(True)
        await until(lambda: voice._recording and voice.tts.cancelled.is_set())
        await voice.adapter.queue.put(event("danger", "critical"))
        await asyncio.sleep(0.02)
        assert "user" in voice.director._busy
        assert voice.player.audio == []
        await finish(voice, task)

    run(scenario())
