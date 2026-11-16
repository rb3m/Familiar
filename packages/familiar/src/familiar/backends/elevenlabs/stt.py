"""ElevenLabs Scribe clip transcription."""

from familiar.audio.wav import pcm_to_wav
from familiar.interfaces import PLUGIN_API_VERSION, AudioClip

from .client import ElevenLabsClient, backend_errors, check_response


class ElevenLabsSTT(ElevenLabsClient):
    name = "elevenlabs"
    api_version = PLUGIN_API_VERSION
    capabilities = frozenset()

    async def transcribe(self, audio: AudioClip, language: str) -> str:
        with backend_errors():
            response = await self._get_client().post(
                "/v1/speech-to-text",
                data={
                    "model_id": self.settings.get("model", "scribe_v2"),
                    "language_code": language,
                },
                files={"file": ("audio.wav", pcm_to_wav(audio), "audio/wav")},
            )
            await check_response(response)
            return response.json()["text"]
