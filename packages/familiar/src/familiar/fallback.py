"""Speech fallback before output starts; permanent failures disable the primary."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from familiar.interfaces import (
    PLUGIN_API_VERSION,
    AudioChunk,
    AudioClip,
    BackendError,
    SpeechToText,
    TextToSpeech,
)

_LOG = logging.getLogger(__name__)


async def close_backend(backend: Any) -> None:
    close = getattr(backend, "aclose", None)
    if close is not None:
        await close()


class _Fallback:
    name = "fallback"
    api_version = PLUGIN_API_VERSION
    capabilities = frozenset()

    def __init__(self, primary: Any, secondary: Any) -> None:
        self.primary = primary
        self.secondary = secondary
        self._disabled = False
        self._warned = False

    def _failed(self, error: BackendError) -> None:
        if not error.retryable:
            self._disabled = True
        if self._warned:
            return
        self._warned = True
        _LOG.warning("%s failed; using %s: %s", self.primary.name, self.secondary.name, error)

    async def aclose(self) -> None:
        await asyncio.gather(close_backend(self.primary), close_backend(self.secondary))


class FallbackSTT(_Fallback):
    def __init__(self, primary: SpeechToText, secondary: SpeechToText) -> None:
        super().__init__(primary, secondary)

    async def transcribe(self, audio: AudioClip, language: str) -> str:
        if self._disabled:
            return await self.secondary.transcribe(audio, language)
        try:
            return await self.primary.transcribe(audio, language)
        except BackendError as exc:
            self._failed(exc)
        return await self.secondary.transcribe(audio, language)


class FallbackTTS(_Fallback):
    """Ignore the caller's voice; resolve each backend's voice via voice_for."""

    capabilities = frozenset({"streaming"})

    def __init__(
        self,
        primary: TextToSpeech,
        secondary: TextToSpeech,
        voice_for: Callable[[str], str | None],
    ) -> None:
        super().__init__(primary, secondary)
        self.voice_for = voice_for

    async def _synthesize(
        self, backend: TextToSpeech, text: str, language: str
    ) -> AsyncIterator[AudioChunk]:
        async for chunk in backend.synthesize(text, self.voice_for(backend.name), language):
            yield chunk

    async def synthesize(
        self, text: str, voice: str | None, language: str
    ) -> AsyncIterator[AudioChunk]:
        if self._disabled:
            async for chunk in self._synthesize(self.secondary, text, language):
                yield chunk
            return
        started = False
        try:
            async for chunk in self._synthesize(self.primary, text, language):
                started = True
                yield chunk
            return
        except BackendError as exc:
            if not started or not exc.retryable:
                self._failed(exc)
            if started:
                raise
        async for chunk in self._synthesize(self.secondary, text, language):
            yield chunk
