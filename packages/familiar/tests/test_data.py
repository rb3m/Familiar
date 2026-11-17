"""Packaged defaults, isolated user overrides, and their core consumers."""

from contextlib import asynccontextmanager
from json import loads
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiohttp.test_utils import make_mocked_request
from familiar.backends.elevenlabs.stt import ElevenLabsSTT
from familiar.backends.elevenlabs.tts import ElevenLabsTTS
from familiar.backends.gemini.llm import GeminiLLM
from familiar.backends.gemini.stt import GeminiSTT
from familiar.backends.gemini.tts import GeminiTTS
from familiar.config import AppConfig
from familiar.gui.server import create_app, middleware
from familiar.hands import ActionKeeper, confirmation_words
from familiar.interfaces import (
    AudioClip,
    ConfigError,
    load_data,
    save_user_data,
    user_data_path,
)
from familiar.persona import language_names, languages


@pytest.fixture
def packaged(tmp_path, monkeypatch):
    directory = tmp_path / "package"
    (directory / "data").mkdir(parents=True)
    monkeypatch.setattr("familiar.interfaces.data.files", lambda package: directory)
    path = directory / "data" / "sample.yaml"
    path.write_text("nested: {keep: 1, change: 2}\nitems: [1, 2]\nremove: true\nscalar: 1\n")
    return path


def override(text, name="sample"):
    path = user_data_path("familiar", name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_merge_and_sources(packaged):
    user = override("nested: {change: 3, added: 4}\nitems: [9]\nremove: null\n")
    dataset = load_data("familiar", "sample")
    assert dataset.value == {
        "nested": {"keep": 1, "change": 3, "added": 4},
        "items": [9],
        "scalar": 1,
    }
    assert dataset.sources == (str(packaged), str(user))


@pytest.mark.parametrize(
    "text, expected",
    [
        ("nested: replaced", "replaced"),
        ("nested: [1]", [1]),
        ("nested: null", None),
    ],
)
def test_mapping_replacement(packaged, text, expected):
    override(text)
    assert load_data("familiar", "sample").value.get("nested") == expected


def test_new_mapping_null_deletion(packaged):
    override("scalar: {remove: null, keep: 1}\nnew: {remove: null}")
    value = load_data("familiar", "sample").value
    assert value["scalar"] == {"keep": 1}
    assert value["new"] == {}


@pytest.mark.parametrize("contents", ["", "# no changes\n", "null\n"])
def test_empty_override(packaged, contents):
    expected = load_data("familiar", "sample")
    path = override(contents)
    result = load_data("familiar", "sample")
    assert result.value == expected.value
    assert result.sources == (*expected.sources, str(path))


@pytest.mark.parametrize("contents", ["a: [", "a: 1\na: 2", "nested: {a: 1, a: 2}"])
def test_bad_user_yaml_names_file(packaged, contents):
    path = override(contents)
    with pytest.raises(ConfigError, match=str(path)):
        load_data("familiar", "sample")


def test_duplicate_packaged_keys(packaged):
    packaged.write_text("a: 1\na: 2")
    with pytest.raises(ConfigError, match=str(packaged)):
        load_data("familiar", "sample")


def test_unreadable_user_file(packaged):
    path = user_data_path("familiar", "sample")
    path.mkdir(parents=True)
    with pytest.raises(ConfigError, match=str(path)):
        load_data("familiar", "sample")


def test_required_packaged_file(packaged):
    packaged.unlink()
    with pytest.raises(ConfigError, match=str(packaged)):
        load_data("familiar", "sample")


@pytest.mark.parametrize(
    "package, name",
    [
        ("../familiar", "sample"),
        ("familiar", "../sample"),
        ("familiar.other", "sample"),
        ("", "sample"),
        ("familiar", "a/b"),
    ],
)
def test_traversal_rejected(package, name):
    for operation in (load_data, user_data_path):
        with pytest.raises(ConfigError, match="identifier"):
            operation(package, name)
    with pytest.raises(ConfigError, match="identifier"):
        save_user_data(package, name, {})


def test_save_round_trip_and_refresh(packaged):
    save_user_data("familiar", "sample", {"nested": {"change": "日本語"}})
    assert load_data("familiar", "sample").value["nested"]["change"] == "日本語"
    save_user_data("familiar", "sample", {"nested": {"change": "new"}})
    assert load_data("familiar", "sample").value["nested"]["change"] == "new"
    assert list(user_data_path("familiar", "sample").parent.iterdir()) == [
        user_data_path("familiar", "sample")
    ]


def test_user_root(monkeypatch, tmp_path):
    monkeypatch.delenv("FAMILIAR_DATA_DIR")
    assert user_data_path("familiar", "models") == (
        Path("~/.config/familiar/data").expanduser() / "familiar/models.yaml"
    )
    assert user_data_path("familiar", "models", tmp_path) == tmp_path / "familiar/models.yaml"


def test_language_names_and_persona(persona):
    assert language_names() == {"en": "English", "es": "Spanish", "ja": "Japanese"}
    save_user_data(
        "familiar",
        "languages",
        {"en": {"name": "Custom English"}, "pt-BR": {"name": "Portuguese", "native": "Português"}},
    )
    assert "Always reply in Custom English." in persona.system_prompt("en")
    assert language_names()["pt-BR"] == "Portuguese"
    assert languages()["en"]["native"] == "English"


@pytest.mark.parametrize(
    "value", [[], {"en": "English"}, {"bad-code": {"name": "Bad", "native": "Bad"}}]
)
def test_bad_language_data(value):
    save_user_data("familiar", "languages", value)
    with pytest.raises(ConfigError, match="languages.yaml"):
        language_names()


def test_confirmation_overrides_and_settings():
    save_user_data(
        "familiar",
        "confirmation_words",
        {
            "affirmative": {"en": {"yes": False, "yeah": None, "proceed": True}},
            "negative": {"en": {"no": False, "nope": None, "halt": True}},
        },
    )
    keeper = ActionKeeper(SimpleNamespace(), {"affirmatives": {"en": ["engage"]}}, "en")
    assert {"proceed", "engage"} <= set(keeper.affirmatives["en"])
    assert not {"yes", "yeah"} & set(keeper.affirmatives["en"])
    assert "halt" in keeper.negatives["en"]
    assert not {"no", "nope"} & set(keeper.negatives["en"])


def test_bare_confirmation_key_rejected():
    path = override("affirmatives:\n  en:\n    yes: true\n", "confirmation_words")
    with pytest.raises(ConfigError, match=str(path)):
        confirmation_words()


@pytest.fixture
def language_client():
    # Exercise registered routes and real auth middleware without opening a socket.
    app = create_app(AppConfig(), token="test-token")

    class Client:
        async def request(self, method, path, headers=None, json=None):
            request = make_mocked_request(method, path, headers=headers, app=app)
            request.json = AsyncMock(return_value=json)
            request.transport.get_extra_info.return_value = ("127.0.0.1", 8765)
            match = await app.router.resolve(request)
            match.add_app(app)
            request._match_info = match
            response = await middleware(request, match.handler)
            return SimpleNamespace(
                status=response.status,
                json=AsyncMock(return_value=loads(response.body)),
            )

        async def get(self, path, **kwargs):
            return await self.request("GET", path, **kwargs)

        async def post(self, path, **kwargs):
            return await self.request("POST", path, **kwargs)

    return Client()


async def test_languages_api(language_client):
    headers = {"X-Familiar-Token": "test-token"}
    response = await language_client.get("/api/languages", headers=headers)
    assert response.status == 200
    assert [item["id"] for item in await response.json()] == ["en", "es", "ja"]
    for code, name in [("pt-BR", "Portuguese"), ("fr", "French")]:
        response = await language_client.post(
            "/api/languages", headers=headers, json={"code": code, "name": name}
        )
        assert response.status == 200
        assert await response.json() == {"id": code, "name": name, "native": name}
    response = await language_client.get("/api/languages", headers=headers)
    assert [item["id"] for item in await response.json()] == ["en", "es", "ja", "pt-BR", "fr"]
    assert "en:" not in user_data_path("familiar", "languages").read_text()


@pytest.mark.parametrize(
    "payload",
    [
        {"code": "../en", "name": "Bad"},
        {"code": "engl", "name": "Bad"},
        {"code": "pt-br", "name": "Bad"},
        {"code": 123, "name": "Bad"},
        {"code": "en", "name": " "},
        {"code": "en", "name": 1},
        {"code": "en", "name": "English", "native": None},
        [],
    ],
)
async def test_languages_api_bad_input(language_client, payload):
    response = await language_client.post(
        "/api/languages", json=payload, headers={"X-Familiar-Token": "test-token"}
    )
    assert response.status == 400


@pytest.mark.parametrize("method", ["get", "post"])
async def test_languages_api_auth(language_client, method):
    response = await getattr(language_client, method)("/api/languages")
    assert response.status == 403
    response = await getattr(language_client, method)(
        "/api/languages",
        headers={
            "X-Familiar-Token": "test-token",
            "Origin": "http://untrusted.invalid",
        },
    )
    assert response.status == 403


def test_gemini_model_defaults_and_precedence():
    assert GeminiLLM({}).model == "gemini-3.5-flash-lite"
    assert GeminiLLM({}).fallback_models == ["gemini-3.8-flash"]
    assert GeminiSTT({}).model == "gemini-3.5-transcribe"
    assert GeminiTTS({}).model == "gemini-3.8-flash-lite-tts"
    save_user_data(
        "familiar",
        "models",
        {
            "gemini": {
                "llm": "custom-llm",
                "stt": "custom-stt",
                "tts": "custom-tts",
                "fallback_models": ["custom-fallback"],
            }
        },
    )
    assert GeminiLLM({}).model == "custom-llm"
    assert GeminiLLM({}).fallback_models == ["custom-fallback"]
    assert GeminiLLM({"model": "configured", "fallback_models": []}).model == "configured"
    assert GeminiLLM({"fallback_models": []}).fallback_models == []
    for backend, kind in [(GeminiSTT, "stt"), (GeminiTTS, "tts")]:
        assert backend({}).model == f"custom-{kind}"
        assert backend({"model": "configured"}).model == "configured"


@pytest.mark.parametrize(
    "custom, configured, expected",
    [
        (False, False, {"stt": "scribe_v2", "tts": "eleven_flash_v2_5"}),
        (True, False, {"stt": "custom-stt", "tts": "custom-tts"}),
        (True, True, {"stt": "configured", "tts": "configured"}),
    ],
)
async def test_elevenlabs_model_defaults(monkeypatch, custom, configured, expected):
    if custom:
        save_user_data(
            "familiar", "models", {"elevenlabs": {"stt": "custom-stt", "tts": "custom-tts"}}
        )
    settings = {"model": "configured"} if configured else {}
    response = httpx.Response(200, json={"text": "hello"})
    client = SimpleNamespace(post=AsyncMock(return_value=response))
    monkeypatch.setattr(ElevenLabsSTT, "_get_client", lambda self: client)
    await ElevenLabsSTT(settings).transcribe(AudioClip(b"\0\0", 24000), "en")
    assert client.post.call_args.kwargs["data"]["model_id"] == expected["stt"]

    @asynccontextmanager
    async def stream(*args, **kwargs):
        assert kwargs["json"]["model_id"] == expected["tts"]
        yield httpx.Response(200, content=b"\0\0")

    monkeypatch.setattr(ElevenLabsTTS, "_get_client", lambda self: SimpleNamespace(stream=stream))
    chunks = [chunk async for chunk in ElevenLabsTTS(settings).synthesize("hello", "voice", "en")]
    assert len(chunks) == 1
