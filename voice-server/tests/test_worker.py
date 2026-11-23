import threading
from types import SimpleNamespace

import numpy as np
import pytest
from familiar_voice_server.worker import WORKER, create_app, pcm16, validate_engine

from .conftest import SPEECH, FakeEngine, until
from .http_utils import LocalTestClient as TestClient
from .http_utils import LocalTestServer as TestServer


def test_pcm16_clipping_and_little_endian():
    audio = np.array([-2, -1, -0.5, 0, 0.5, 1, 2, np.nan, np.inf, -np.inf], dtype=np.float32)
    assert pcm16(audio) == bytes.fromhex("0080 0080 00c0 0000 0040 ff7f ff7f 0000 ff7f 0080")
    assert pcm16(np.array([], dtype=np.float32)) == b""
    assert pcm16(np.array([0.5], dtype=np.float32)) == b"\x00\x40"
    with pytest.raises(ValueError, match="mono"):
        pcm16(np.zeros((2, 4)))


@pytest.mark.parametrize(
    "model_type,rate,message",
    [
        ("custom_voice", 24000, "requires model type 'base'"),
        ("voice_design", 24000, "requires model type 'base'"),
        ("base", 48000, "24000 Hz"),
    ],
)
def test_reject_incompatible_engine(config, model_type, rate, message):
    engine = FakeEngine(config)
    engine.model_type = model_type
    engine.sample_rate = rate
    with pytest.raises(ValueError, match=message):
        validate_engine(config, engine)


def test_missing_reference(config):
    config.voices["sarah"].ref_audio.unlink()
    with pytest.raises(ValueError, match="reference audio does not exist"):
        validate_engine(config, FakeEngine(config))


async def test_worker_streams_on_background_thread_and_rejects_busy(config):
    engine = FakeEngine(config)
    engine.block = True
    construction_threads = []

    def factory(config):
        construction_threads.append(threading.get_ident())
        return engine

    async with TestClient(TestServer(create_app(config, factory))) as client:
        assert (await client.get("/ready")).status == 200
        response = await client.post("/speech", json=SPEECH)
        assert response.status == 200
        assert await response.content.readexactly(6) == bytes.fromhex("0080 0000 ff7f")
        assert (await client.post("/speech", json=SPEECH)).status == 503
        engine.release.set()
        assert await response.read() == b"\x00\x40"
        assert engine.threads == construction_threads
        assert construction_threads[0] != threading.get_ident()
        await until(lambda: not client.app[WORKER].busy)


async def test_worker_failure_before_audio_is_json(config):
    engine = FakeEngine(config)
    engine.fail_before = True
    async with TestClient(TestServer(create_app(config, lambda cfg: engine))) as client:
        response = await client.post("/speech", json=SPEECH)
        assert response.status == 500
        assert await response.json() == {"error": "synthesis failed", "state": "ready"}
        await until(lambda: not client.app[WORKER].busy)


async def test_worker_empty_audio_is_failure(config):
    engine = FakeEngine(config)
    engine.generate = lambda *args: iter([np.array([], dtype=np.float32)])
    async with TestClient(TestServer(create_app(config, lambda cfg: engine))) as client:
        response = await client.post("/speech", json=SPEECH)
        assert response.status == 500
        assert (await response.json())["error"] == "synthesis returned no audio"


async def test_worker_bad_request(config):
    async with TestClient(TestServer(create_app(config, FakeEngine))) as client:
        assert (await client.post("/speech", json={**SPEECH, "voice": "missing"})).status == 400
        assert (await client.post("/speech", data="bad-json")).status == 400


async def test_failed_startup_releases_executor(config):
    def fail(config):
        raise RuntimeError("fake load failure")

    app = create_app(config, fail)
    server = TestServer(app)
    with pytest.raises(RuntimeError, match="fake load failure"):
        await server.start_server()
    assert app[WORKER].executor._shutdown
    await server.close()


@pytest.mark.parametrize("mode,model_type", [("clone", "base"), ("design", "voice_design")])
def test_qwen_engine_boundary(config, monkeypatch, mode, model_type):
    import sys
    from dataclasses import replace
    from types import ModuleType, SimpleNamespace

    from familiar_voice_server.config import VoiceConfig
    from familiar_voice_server.worker import QwenEngine

    evaluated = []
    calls = []
    loads = []
    audio = np.array([0.25, -0.25], dtype=np.float32)

    def generate(**kwargs):
        calls.append(kwargs)
        yield SimpleNamespace(audio=audio, sample_rate=24000)

    model = SimpleNamespace(
        config=SimpleNamespace(tts_model_type=model_type), sample_rate=24000, generate=generate
    )

    def load_model(model_id):
        loads.append(model_id)
        return model

    modules = {
        name: ModuleType(name)
        for name in ("mlx", "mlx.core", "mlx_audio", "mlx_audio.tts", "mlx_audio.tts.utils")
    }
    modules["mlx"].core = modules["mlx.core"]
    modules["mlx.core"].eval = evaluated.append
    modules["mlx_audio.tts.utils"].load_model = load_model
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    voice = config.voices["sarah"]
    if mode == "design":
        voice = VoiceConfig(mode="design", description="Warm and low")
        config = replace(config, voices={"sarah": voice})
    engine = QwenEngine(config)
    validate_engine(config, engine)
    chunks = list(engine.generate("Hello", voice, "english"))
    assert loads == [config.model.id]
    assert evaluated == [audio]
    assert chunks[0].dtype == np.float32
    options = {"instruct": "Warm and low"}
    if mode == "clone":
        options = {"ref_audio": str(voice.ref_audio), "ref_text": voice.ref_text}
    assert calls == [
        {
            "text": "Hello",
            "lang_code": "english",
            "stream": True,
            "streaming_interval": 0.32,
            **options,
        }
    ]


def test_warm_up_speaks_each_voice_and_survives_failures(caplog):
    from familiar_voice_server.worker import WARMUP_TEXT, warm_up

    class Engine:
        calls = []

        def generate(self, text, voice, language):
            self.calls.append((text, voice))
            if len(self.calls) == 1:
                raise RuntimeError("first voice broke")
            yield np.zeros(4, dtype=np.float32)

    config = SimpleNamespace(voices={"a": "voice-a", "b": "voice-b"}, languages={"en": "english"})
    engine = Engine()
    warm_up(config, engine)
    assert engine.calls == [(WARMUP_TEXT, "voice-a"), (WARMUP_TEXT, "voice-b")]
    assert "Warm-up failed" in caplog.text
