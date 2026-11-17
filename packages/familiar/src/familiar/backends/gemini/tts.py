"""Streaming Gemini speech, PCM16 mono at 24 kHz."""

import base64
from collections.abc import AsyncIterator

from familiar.interfaces import PLUGIN_API_VERSION, AudioChunk

from .client import GeminiSpeechClient, backend_errors


class GeminiTTS(GeminiSpeechClient):
    name = "gemini"
    api_version = PLUGIN_API_VERSION
    capabilities = frozenset({"streaming"})

    model_kind = "tts"

    async def synthesize(
        self, text: str, voice: str | None, language: str
    ) -> AsyncIterator[AudioChunk]:
        with backend_errors():
            stream = await self._get_client().aio.interactions.create(
                model=self.model,
                input=[{"type": "user_input", "content": [{"type": "text", "text": text}]}],
                response_format={"type": "audio"},
                generation_config={"speech_config": [{"voice": voice or "Leda"}]},
                stream=True,
            )
            try:
                async for event in stream:
                    delta = getattr(event, "delta", None)
                    if getattr(delta, "type", None) != "audio":
                        continue
                    data = getattr(delta, "data", None)
                    if data:
                        pcm = base64.b64decode(data) if isinstance(data, str) else data
                        yield AudioChunk(pcm, 24000)
            finally:
                close = getattr(stream, "aclose", None) or getattr(stream, "close", None)
                if close is not None:
                    await close()
