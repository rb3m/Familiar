"""Push-to-talk voice orchestration and barge-in."""

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import aclosing
from time import monotonic

from familiar.audio.player import Player
from familiar.audio.recorder import Recorder
from familiar.conversation import Conversation
from familiar.fallback import close_backend
from familiar.interfaces import (
    AudioChunk,
    AudioClip,
    BackendError,
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
    ) -> None:
        self.ptt = ptt
        self.recorder = recorder
        self.player = player
        self.stt = stt
        self.tts = tts
        self.conversation = conversation
        self.persona = persona
        self.language = language
        self.transcript = transcript
        self._reply_task: asyncio.Task | None = None
        self._recording = False

    async def _cancel_reply(self) -> None:
        self.player.stop()
        if self._reply_task is None:
            return
        self._reply_task.cancel()
        await asyncio.gather(self._reply_task, return_exceptions=True)
        self._reply_task = None

    async def _event(self, pressed: bool) -> None:
        handler = self._press if pressed else self._release
        await handler()

    async def _press(self) -> None:
        """Barge in: silence the companion and start recording."""
        if self._recording:
            return
        await self._cancel_reply()
        await asyncio.to_thread(self.recorder.start)
        self._recording = True
        print("listening…", flush=True)

    async def _release(self) -> None:
        """Stop recording and answer, unless the clip is too short to be speech."""
        if not self._recording:
            return
        released = monotonic()
        clip = await asyncio.to_thread(self.recorder.stop)
        self._recording = False
        if len(clip.pcm) / (2 * clip.sample_rate) < 0.3:
            return
        self._reply_task = asyncio.create_task(self._reply(clip, released))

    async def _audio(
        self, sentence: str, released: float, timings: dict[str, float | None]
    ) -> AsyncIterator[AudioChunk]:
        voice = self.persona.voice(self.tts.name, self.language)
        async with aclosing(self.tts.synthesize(sentence, voice, self.language)) as chunks:
            async for chunk in chunks:
                if timings["first_audio_ms"] is None:
                    timings["first_audio_ms"] = (monotonic() - released) * 1000
                yield chunk

    async def _reply(self, clip: AudioClip, released: float) -> None:
        timings: dict[str, float | None] = dict.fromkeys(
            ("stt_ms", "first_sentence_ms", "first_audio_ms")
        )
        started = monotonic()
        try:
            text = (await self.stt.transcribe(clip, self.language)).strip()
            timings["stt_ms"] = (monotonic() - started) * 1000
            if not text:
                return
            print(f"You: {text}", flush=True)
            async with aclosing(self.conversation.ask(text)) as sentences:
                async for sentence in sentences:
                    if timings["first_sentence_ms"] is None:
                        timings["first_sentence_ms"] = (monotonic() - released) * 1000
                    print(f"{self.persona.display_name}: {sentence}", flush=True)
                    await self.player.play(self._audio(sentence, released, timings))
            await self.player.wait_idle()
        except BackendError as exc:
            print(f"Voice error: {exc}. Try speaking again.", file=sys.stderr)
        finally:
            self.transcript.record("timing", **timings)

    async def run(self) -> None:
        try:
            async for pressed in self.ptt.events():
                await self._event(pressed)
            if self._reply_task is not None:
                await self._reply_task
        finally:
            await self._cancel_reply()
            await asyncio.to_thread(self.recorder.stop)
            await asyncio.gather(
                *(
                    close_backend(backend)
                    for backend in (
                        self.ptt,
                        self.player,
                        self.stt,
                        self.tts,
                        self.conversation.llm,
                    )
                )
            )
