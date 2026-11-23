"""Private localhost synthesis worker; all MLX access is isolated in QwenEngine."""

import argparse
import asyncio
import contextlib
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

import numpy as np
from aiohttp import web

from .config import Config, VoiceConfig, load_config

LOG = logging.getLogger(__name__)
AUDIO_TYPE = "audio/L16; rate=24000; channels=1"


class Engine(Protocol):
    model_type: str
    sample_rate: int

    def generate(self, text: str, voice: VoiceConfig, language: str) -> Iterator[np.ndarray]: ...


class QwenEngine:
    """The single MLX boundary; construction and iteration run on one dedicated thread."""

    def __init__(self, config: Config):
        import mlx.core as mx
        from mlx_audio.tts.utils import load_model

        self.mx = mx
        self.model = load_model(config.model.id)
        self.model_type = self.model.config.tts_model_type
        self.sample_rate = self.model.sample_rate
        self.interval = config.model.streaming_interval_s

    def generate(self, text: str, voice: VoiceConfig, language: str) -> Iterator[np.ndarray]:
        options = {"instruct": voice.description}
        if voice.mode == "clone":
            options = {"ref_audio": str(voice.ref_audio), "ref_text": voice.ref_text}
        for result in self.model.generate(
            text=text,
            lang_code=language,
            stream=True,
            streaming_interval=self.interval,
            **options,
        ):
            self.mx.eval(result.audio)
            if result.sample_rate != 24000:
                raise ValueError("model generated audio at a rate other than 24000 Hz")
            yield np.asarray(result.audio, dtype=np.float32)


def validate_engine(config: Config, engine: Engine) -> None:
    if engine.sample_rate != 24000:
        raise ValueError("model must produce 24000 Hz audio")
    required = {"clone": "base", "design": "voice_design"}
    for name, voice in config.voices.items():
        if engine.model_type != required[voice.mode]:
            raise ValueError(
                f"voice {name!r}: mode {voice.mode!r} requires model type "
                f"{required[voice.mode]!r}, got {engine.model_type!r}"
            )
        if voice.ref_audio is not None and not voice.ref_audio.is_file():
            raise ValueError(f"voice {name!r}: reference audio does not exist: {voice.ref_audio}")


WARMUP_TEXT = "Ready."


def warm_up(config: Config, engine: Engine) -> None:
    """Speak one short phrase per voice so the first real sentence isn't a cold start."""
    language = next(iter(config.languages.values()))
    for name, voice in config.voices.items():
        try:
            for _ in engine.generate(WARMUP_TEXT, voice, language):
                pass
        except Exception:
            LOG.exception("Warm-up failed for voice %r; continuing", name)


def pcm16(audio: np.ndarray) -> bytes:
    """Convert mono floating point samples to clipped, little-endian signed PCM16."""
    samples = np.asarray(audio, dtype=np.float32).squeeze()
    if samples.ndim > 1:
        raise ValueError("expected mono audio")
    samples = np.nan_to_num(samples, nan=0.0, posinf=1.0, neginf=-1.0)
    return np.clip(samples * 32768.0, -32768, 32767).astype("<i2").tobytes()


def next_chunk(iterator: Iterator[np.ndarray]) -> bytes | None:
    for audio in iterator:
        chunk = pcm16(audio)
        if chunk:
            return chunk
    return None


class Worker:
    def __init__(self, config: Config, engine_factory):
        self.config = config
        self.engine_factory = engine_factory
        self.engine: Engine | None = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qwen")
        self.busy = False

    async def context(self, app: web.Application):
        try:
            loop = asyncio.get_running_loop()
            self.engine = await loop.run_in_executor(
                self.executor, self.engine_factory, self.config
            )
            validate_engine(self.config, self.engine)
            await loop.run_in_executor(self.executor, warm_up, self.config, self.engine)
            yield
        finally:
            self.executor.shutdown(wait=True, cancel_futures=True)

    def error(self, message: str, status: int) -> web.Response:
        return web.json_response({"error": message, "state": "ready"}, status=status)

    async def ready(self, request: web.Request) -> web.Response:
        return web.json_response({"state": "ready"})

    async def speech(self, request: web.Request) -> web.StreamResponse:
        try:
            body = self.config.speech(await request.json())
        except ValueError as exc:
            return self.error(str(exc), 400)
        if self.busy:
            return self.error("busy", 503)
        self.busy = True
        iterator = None
        pending = None
        output = None
        loop = asyncio.get_running_loop()
        try:
            iterator = iter(
                self.engine.generate(
                    body["text"],
                    self.config.voices[body["voice"]],
                    self.config.languages[body["language"]],
                )
            )
            while True:
                pending = loop.run_in_executor(self.executor, next_chunk, iterator)
                chunk = await asyncio.shield(pending)
                if chunk is None:
                    break
                if output is None:
                    output = web.StreamResponse(headers={"Content-Type": AUDIO_TYPE})
                    await output.prepare(request)
                await output.write(chunk)
            if output is None:
                return self.error("synthesis returned no audio", 500)
            await output.write_eof()
            return output
        except Exception:
            LOG.exception("Synthesis failed")
            if output is None:
                return self.error("synthesis failed", 500)
            # Do not send a successful chunked terminator after a partial failure.
            if request.transport is not None:
                request.transport.close()
            return output
        finally:
            try:
                if pending is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.shield(pending)
            finally:
                try:
                    if iterator is not None and hasattr(iterator, "close"):
                        await loop.run_in_executor(self.executor, iterator.close)
                finally:
                    self.busy = False


def create_app(config: Config, engine_factory=QwenEngine) -> web.Application:
    worker = Worker(config, engine_factory)
    app = web.Application()
    app[WORKER] = worker
    app.cleanup_ctx.append(worker.context)
    app.add_routes([web.get("/ready", worker.ready), web.post("/speech", worker.speech)])
    return app


WORKER = web.AppKey("worker", Worker)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    config = load_config(args.config)
    web.run_app(create_app(config), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
