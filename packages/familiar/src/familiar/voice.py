"""Push-to-talk voice orchestration and barge-in."""

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import aclosing
from time import monotonic

from familiar.audio.player import Player
from familiar.audio.recorder import Recorder
from familiar.conversation import Conversation
from familiar.crew import persona_id
from familiar.director import Director, feed_events
from familiar.fallback import close_backend
from familiar.interfaces import (
    AudioChunk,
    AudioClip,
    BackendError,
    GameAdapter,
    GameEvent,
    PushToTalk,
    SpeechToText,
    TextToSpeech,
)
from familiar.persona import Persona
from familiar.transcript import TranscriptLog


class VoiceSession:
    def __init__(
        self,
        ptt: PushToTalk,
        recorder: Recorder,
        player: Player,
        stt: SpeechToText,
        tts: TextToSpeech,
        conversation: Conversation,
        persona: Persona,
        language: str,
        transcript: TranscriptLog,
        adapter: GameAdapter | None = None,
        director: Director | None = None,
        seats: dict[str, float] | None = None,
    ) -> None:
        self.ptt = ptt
        self.recorder = recorder
        self.player = player
        self.stt = stt
        self.tts = tts
        self.conversation = conversation
        self.conversation.playback_ack_required = True
        self._sentence_audio = False
        self.persona = persona
        self.language = language
        self.transcript = transcript
        self.adapter = adapter
        self.director = director
        self.seats = seats
        self._interjection: GameEvent | None = None
        self._reply_task: asyncio.Task | None = None
        self._recording = False
        self._reply_lock = asyncio.Lock()
        self._user_busy = False

    def _busy(self, reason: str, busy: bool) -> None:
        if reason == "user":
            self._user_busy = busy
        if self.director is not None:
            self.director.set_busy(reason, busy)

    async def _cancel_reply(self) -> None:
        self.player.stop()
        if self._reply_task is None:
            return
        self._reply_task.cancel()
        await asyncio.gather(self._reply_task, return_exceptions=True)
        self._reply_task = None
        self._busy("speaking", False)
        self._busy("critical", False)
        self._busy("reply", False)

    async def _event(self, pressed: bool) -> None:
        handler = self._press if pressed else self._release
        await handler()

    async def _press(self) -> None:
        """Barge in: silence the companion and start recording."""
        if self._recording:
            return
        self._busy("user", True)
        async with self._reply_lock:
            await self._cancel_reply()
            self._busy("user", True)
        try:
            await asyncio.to_thread(self.recorder.start)
        except BaseException:
            self._busy("user", False)
            raise
        self._recording = True
        print("listening…", flush=True)

    async def _release(self) -> None:
        """Stop recording and answer, unless the clip is too short to be speech."""
        if not self._recording:
            return
        released = monotonic()
        try:
            clip = await asyncio.to_thread(self.recorder.stop)
        except BaseException:
            self._busy("user", False)
            raise
        self._recording = False
        if len(clip.pcm) / (2 * clip.sample_rate) < 0.3:
            self._busy("user", False)
            return
        self._interjection = None
        self._reply_task = asyncio.create_task(self._reply(clip, released))

    def _pan(self) -> float:
        """The current speaker's stereo seat; centre when stereo placement is off."""
        if self.seats is None:
            return 0.0
        return self.seats.get(persona_id(self.conversation.speaker), 0.0)

    async def _audio(
        self, sentence: str, released: float, timings: dict[str, float | None]
    ) -> AsyncIterator[AudioChunk]:
        voice = self.conversation.speaker.voice(self.tts.name, self.language)
        async with aclosing(self.tts.synthesize(sentence, voice, self.language)) as chunks:
            async for chunk in chunks:
                self._sentence_audio = True
                if timings["first_audio_ms"] is None:
                    timings["first_audio_ms"] = (monotonic() - released) * 1000
                yield chunk

    async def _reply(self, clip: AudioClip, released: float) -> None:
        timings: dict[str, float | None] = dict.fromkeys(
            ("stt_ms", "first_sentence_ms", "first_audio_ms")
        )
        started = monotonic()
        self._busy("reply", True)
        try:
            text = (await self.stt.transcribe(clip, self.language)).strip()
            timings["stt_ms"] = (monotonic() - started) * 1000
            if not text:
                return
            self._busy("user", False)
            print(f"You: {text}", flush=True)
            await self._speak(self.conversation.ask(text), released, timings)
        except BackendError as exc:
            print(f"Voice error: {exc}. Try speaking again.", file=sys.stderr)
        finally:
            self._busy("user", False)
            self._busy("reply", False)
            self.transcript.record("timing", **timings)

    async def _speak(
        self,
        sentences: AsyncIterator[str],
        released: float,
        timings: dict[str, float | None],
        event: GameEvent | None = None,
    ) -> None:
        self._busy("critical", event is not None and event.priority == "critical")
        self._busy("speaking", True)
        try:
            async with aclosing(sentences):
                async for sentence in sentences:
                    if timings["first_sentence_ms"] is None:
                        timings["first_sentence_ms"] = (monotonic() - released) * 1000
                    label = self.conversation.speaker.display_name
                    if event is not None:
                        label += f" (event: {event.kind})"
                    print(f"{label}: {sentence}", flush=True)
                    self._sentence_audio = False
                    await self.player.play(
                        self._audio(sentence, released, timings), pan=self._pan()
                    )
                    # Resuming the sentence generator acknowledges actual playback.
                    await self.player.wait_idle()
                    if self._sentence_audio:
                        self.conversation.acknowledge_playback()
            await self.player.wait_idle()
        finally:
            if timings["first_audio_ms"] is not None and self.director is not None:
                self.director.notify_spoke()
            self._busy("speaking", False)
            self._busy("critical", False)

    async def _interject(self, event: GameEvent) -> None:
        timings: dict[str, float | None] = dict.fromkeys(
            ("stt_ms", "first_sentence_ms", "first_audio_ms")
        )
        try:
            sentences = self.conversation.interject(
                event, spoken=lambda: timings["first_audio_ms"] is not None
            )
            await self._speak(sentences, monotonic(), timings, event)
        except BackendError as exc:
            print(f"Voice error: {exc}. Try speaking again.", file=sys.stderr)
        finally:
            self.transcript.record("timing", **timings)

    async def _game_replies(self) -> None:
        assert self.director is not None
        while True:
            event = await self.director.next()
            async with self._reply_lock:
                if self._reply_task is not None and not self._reply_task.done():
                    if self._interjection is None or not self.director.preempts(event):
                        self.director.submit(event)
                        continue
                    await self._cancel_reply()
                if self._user_busy:
                    self.director.submit(event)
                    continue
                self._interjection = event
                self._busy("speaking", True)
                self._busy("critical", self.director.preempts(event))
                self._reply_task = asyncio.create_task(self._interject(event))

    async def run(self) -> None:
        tasks: list[asyncio.Task] = []
        try:
            if self.adapter is not None and self.director is not None:
                tasks.append(
                    asyncio.create_task(
                        feed_events(self.adapter, self.director, self.conversation.memory)
                    )
                )
            if self.director is not None:
                tasks.append(asyncio.create_task(self._game_replies()))
            if self.director is not None and self.conversation.alerts is not None:
                tasks.append(
                    asyncio.create_task(self.conversation.alerts.run(self.director.submit))
                )
            async for pressed in self.ptt.events():
                await self._event(pressed)
            if self._reply_task is not None:
                await self._reply_task
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._cancel_reply()
            self._busy("user", False)
            await asyncio.to_thread(self.recorder.stop)
            await self.conversation.aclose()
            await asyncio.gather(
                *(
                    close_backend(backend)
                    for backend in (
                        self.ptt,
                        self.player,
                        self.stt,
                        self.tts,
                        self.conversation.llm,
                        self.adapter,
                    )
                    if backend is not None
                )
            )
