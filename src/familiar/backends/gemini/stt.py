"""Gemini clip transcription through the interactions API."""

import base64

from familiar.audio.wav import pcm_to_wav
from familiar.interfaces import PLUGIN_API_VERSION, AudioClip

from .client import GeminiSpeechClient, backend_errors


class GeminiSTT(GeminiSpeechClient):
    name = "gemini"
    api_version = PLUGIN_API_VERSION
    capabilities = frozenset()

    default_model = "gemini-3.5-transcribe"

    async def transcribe(self, audio: AudioClip, language: str) -> str:
        with backend_errors():
            result = await self._get_client().aio.interactions.create(
                model=self.model,
                input=[
                    {
                        "type": "text",
                        "text": f"Transcribe this speech verbatim in {language}. "
                        "Output only the transcript.",
                    },
                    {
                        "type": "audio",
                        "data": base64.b64encode(pcm_to_wav(audio)).decode("ascii"),
                        "mime_type": "audio/wav",
                    },
                ],
            )
            return result.output_text
