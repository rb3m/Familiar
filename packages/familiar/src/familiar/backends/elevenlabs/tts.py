"""ElevenLabs PCM speech streaming and account diagnostics."""

from collections.abc import AsyncIterator
from urllib.parse import quote

from familiar.interfaces import PLUGIN_API_VERSION, AudioChunk, BackendError

from .client import ElevenLabsClient, backend_errors, check_response


class ElevenLabsTTS(ElevenLabsClient):
    name = "elevenlabs"
    api_version = PLUGIN_API_VERSION
    capabilities = frozenset({"streaming"})

    async def synthesize(
        self, text: str, voice: str | None, language: str
    ) -> AsyncIterator[AudioChunk]:
        if not voice:
            raise BackendError("No ElevenLabs voice configured for this persona/language")
        with backend_errors():
            async with self._get_client().stream(
                "POST",
                f"/v1/text-to-speech/{quote(voice, safe='')}/stream",
                params={"output_format": "pcm_24000"},
                json={
                    "text": text,
                    "model_id": self.settings.get("model", "eleven_flash_v2_5"),
                    "language_code": language,
                },
            ) as response:
                await check_response(response)
                pending = b""
                async for data in response.aiter_bytes():
                    pending += data
                    end = len(pending) // 2 * 2
                    if end:
                        yield AudioChunk(pending[:end], 24000)
                        pending = pending[end:]
                if pending:
                    raise BackendError("ElevenLabs returned incomplete PCM16 audio")

    async def _get(self, path: str) -> dict:
        with backend_errors():
            response = await self._get_client().get(path)
            await check_response(response)
            return response.json()

    async def quota(self) -> tuple[int, int]:
        data = await self._get("/v1/user/subscription")
        return data["character_count"], data["character_limit"]

    async def voices(self) -> list[dict]:
        data = await self._get("/v2/voices?page_size=100")
        return data["voices"]
