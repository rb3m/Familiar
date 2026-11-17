"""ElevenLabs Scribe clip transcription."""

from familiar.audio.wav import pcm_to_wav
from familiar.backends.models import model_defaults
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
                    "model_id": self.settings.get("model", model_defaults("elevenlabs")["stt"]),
                    "language_code": language,
                    # Tags like "[outro jingle]" replace misheard speech; we want words.
                    "tag_audio_events": "false",
                },
                files={"file": ("audio.wav", pcm_to_wav(audio), "audio/wav")},
            )
            await check_response(response)
            return response.json()["text"]
