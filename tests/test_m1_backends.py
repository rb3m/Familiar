"""Speech vendor requests with fake SDK and HTTP transports."""

import asyncio
import base64
import io
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from google.genai import errors
from m1_support import run

from familiar.backends.elevenlabs.stt import ElevenLabsSTT
from familiar.backends.elevenlabs.tts import ElevenLabsTTS
from familiar.backends.gemini.stt import GeminiSTT
from familiar.backends.gemini.tts import GeminiTTS
from familiar.interfaces import AudioChunk, AudioClip, BackendError


async def collect(stream):
    return [chunk async for chunk in stream]


def test_gemini_transcription_and_audio_deltas():
    async def scenario():
        create = AsyncMock(return_value=SimpleNamespace(output_text="Hello"))
        client = SimpleNamespace(
            aio=SimpleNamespace(interactions=SimpleNamespace(create=create), aclose=AsyncMock())
        )
        stt = GeminiSTT({})
        stt._client = client
        assert await stt.transcribe(AudioClip(b"\0\0" * 5, 16000), "Japanese") == "Hello"
        args = create.call_args.kwargs
        assert args["model"] == "gemini-3.5-transcribe"
        assert "Japanese" in args["input"][0]["text"]
        with wave.open(io.BytesIO(base64.b64decode(args["input"][1]["data"]))) as wav:
            assert wav.getframerate() == 16000

        async def events():
            yield SimpleNamespace(event_type="interaction.created")
            yield SimpleNamespace(delta=SimpleNamespace(type="text", data="not audio"))
            yield SimpleNamespace(delta=SimpleNamespace(type="audio", data="AQI="))
            yield SimpleNamespace(delta=SimpleNamespace(type="audio", data=b"\3\4"))
            yield SimpleNamespace(event_type="interaction.completed")

        create.return_value = events()
        tts = GeminiTTS({})
        tts._client = client
        assert await collect(tts.synthesize("Hi", "Leda", "en")) == [
            AudioChunk(b"\1\2", 24000),
            AudioChunk(b"\3\4", 24000),
        ]
        args = create.call_args.kwargs
        assert args["stream"] is True
        assert args["response_format"] == {"type": "audio"}
        assert args["generation_config"]["speech_config"] == [{"voice": "Leda"}]
        await stt.aclose()
        await tts.aclose()

    run(scenario())


@pytest.mark.parametrize("code,retryable", [(401, False), (429, True), (503, True)])
def test_gemini_errors(code, retryable):
    async def scenario():
        create = AsyncMock(side_effect=errors.APIError(code, {"error": {"message": "failed"}}))
        backend = GeminiSTT({})
        backend._client = SimpleNamespace(
            aio=SimpleNamespace(interactions=SimpleNamespace(create=create))
        )
        with pytest.raises(BackendError) as caught:
            await backend.transcribe(AudioClip(b"\0\0", 16000), "en")
        assert caught.value.retryable is retryable

    run(scenario())


def test_elevenlabs_success_requests():
    async def scenario():
        requests = []

        def handle(request):
            requests.append(request)
            routes = {
                "/v1/speech-to-text": {"text": "Hello"},
                "/v1/user/subscription": {"character_count": 12, "character_limit": 100},
                "/v2/voices": {"voices": [{"voice_id": "voice", "name": "Leda", "labels": {}}]},
            }
            if "text-to-speech" in request.url.path:
                assert request.url.params["output_format"] == "pcm_24000"
                return httpx.Response(200, content=b"\1\2\3\4")
            return httpx.Response(200, json=routes[request.url.path])

        async with httpx.AsyncClient(
            base_url="https://api.elevenlabs.io",
            transport=httpx.MockTransport(handle),
            headers={"xi-api-key": "fake"},
        ) as client:
            tts, stt = ElevenLabsTTS({}), ElevenLabsSTT({})
            tts._client = stt._client = client
            assert await stt.transcribe(AudioClip(b"\0\0", 16000), "en") == "Hello"
            assert b"scribe_v2" in requests[-1].content
            assert b"RIFF" in requests[-1].content
            assert await collect(tts.synthesize("Hello", "voice", "en")) == [
                AudioChunk(b"\1\2\3\4", 24000)
            ]
            assert await tts.quota() == (12, 100)
            assert (await tts.voices())[0]["voice_id"] == "voice"
            assert requests[-1].url.params["page_size"] == "100"
            assert all(r.headers["xi-api-key"] == "fake" for r in requests)

    run(scenario())


@pytest.mark.parametrize(
    "code,body,retryable",
    [
        (401, "bad key", False),
        (401, "quota exceeded", False),
        (402, "Quota exhausted", False),
        (429, "slow down", True),
        (500, "failed", True),
    ],
)
@pytest.mark.parametrize("operation", ["stt", "tts", "quota", "voices"])
def test_elevenlabs_errors(code, body, retryable, operation):
    async def scenario():
        async with httpx.AsyncClient(
            base_url="https://api.elevenlabs.io",
            transport=httpx.MockTransport(lambda _: httpx.Response(code, text=body)),
        ) as client:
            tts, stt = ElevenLabsTTS({}), ElevenLabsSTT({})
            tts._client = stt._client = client
            operations = {
                "stt": lambda: stt.transcribe(AudioClip(b"\0\0", 16000), "en"),
                "tts": lambda: collect(tts.synthesize("Hi", "voice", "en")),
                "quota": tts.quota,
                "voices": tts.voices,
            }
            with pytest.raises(BackendError) as caught:
                await operations[operation]()
            assert caught.value.retryable is retryable
            if "quota" in body.lower():
                assert str(caught.value) == "ElevenLabs quota exhausted"

    run(scenario())


@pytest.mark.parametrize("code,retryable", [(401, False), (429, True), (503, True)])
def test_gemini_interactions_status_errors(code, retryable):
    from google.genai._interactions import APIStatusError

    async def scenario():
        request = httpx.Request("POST", "https://example.invalid")
        error = APIStatusError("failed", response=httpx.Response(code, request=request), body={})
        create = AsyncMock(side_effect=error)
        backend = GeminiTTS({})
        backend._client = SimpleNamespace(
            aio=SimpleNamespace(interactions=SimpleNamespace(create=create))
        )
        with pytest.raises(BackendError) as caught:
            await collect(backend.synthesize("Hi", "Leda", "en"))
        assert caught.value.retryable is retryable

    run(scenario())


def test_gemini_interactions_connection_error():
    from google.genai._interactions import APITimeoutError

    async def scenario():
        create = AsyncMock(
            side_effect=APITimeoutError(request=httpx.Request("POST", "https://example.invalid"))
        )
        backend = GeminiSTT({})
        backend._client = SimpleNamespace(
            aio=SimpleNamespace(interactions=SimpleNamespace(create=create))
        )
        with pytest.raises(BackendError) as caught:
            await backend.transcribe(AudioClip(b"\0\0", 16000), "en")
        assert caught.value.retryable

    run(scenario())


def test_gemini_stream_closed_on_cancellation():
    async def scenario():
        entered = asyncio.Event()

        class Stream:
            close = AsyncMock()

            def __aiter__(self):
                return self

            async def __anext__(self):
                entered.set()
                await asyncio.Event().wait()

        stream = Stream()
        create = AsyncMock(return_value=stream)
        backend = GeminiTTS({})
        backend._client = SimpleNamespace(
            aio=SimpleNamespace(interactions=SimpleNamespace(create=create))
        )
        task = asyncio.create_task(collect(backend.synthesize("Hi", "Leda", "en")))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        stream.close.assert_awaited_once()

    run(scenario())


def test_shared_gemini_client_options(monkeypatch):
    from familiar.backends.gemini.client import make_client

    calls = []
    monkeypatch.setenv("SPEECH_TEST_KEY", "fake")
    monkeypatch.setattr(
        "familiar.backends.gemini.client.genai.Client", lambda **kw: calls.append(kw)
    )
    make_client({"api_key_env": "SPEECH_TEST_KEY", "timeout_seconds": 1.5})
    assert calls[0]["api_key"] == "fake"
    assert calls[0]["http_options"].api_version == "v1beta"
    assert calls[0]["http_options"].timeout == 1500
    make_client({"api_key_env": "SPEECH_TEST_KEY"}, api_version=None)
    assert calls[1]["http_options"].api_version is None
