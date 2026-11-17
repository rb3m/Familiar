"""Authenticated loopback HTTP API for the Familiar launcher."""

import asyncio
import json
import secrets
import time
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any

from aiohttp import web

from familiar.audio.player import Player
from familiar.config import AppConfig, BackendConfig, find_config_path, load_config
from familiar.fallback import close_backend
from familiar.interfaces import ConfigError, Message, read_user_data, save_user_data
from familiar.persona import (
    BUILTIN_PERSONAS_DIR,
    TRUTHFULNESS_RULES,
    languages,
    list_personas,
    load_persona,
    persona_directories,
    validate_id,
    validate_language,
    validate_persona,
)
from familiar.plugins import available, load, load_class

from .personas import save_persona
from .session import SessionConflictError, SessionManager


def sanitize_draft_voices(persona: dict) -> dict:
    """An LLM cannot know the user's voice library: keep only real Gemini voice names and leave
    ElevenLabs empty for the user to pick (drafts once invented ElevenLabs ids)."""
    languages = persona.get("languages")
    if not isinstance(languages, dict) or not languages:
        languages = {"en": {}}
    cleaned = {}
    for language, settings in languages.items():
        voices = settings.get("voices", {}) if isinstance(settings, dict) else {}
        gemini = voices.get("gemini") if isinstance(voices, dict) else None
        cleaned[language] = {
            "voices": {
                "gemini": gemini if gemini in GEMINI_VOICES else "Kore",
                "elevenlabs": None,
            }
        }
    return {**persona, "languages": cleaned}


GEMINI_VOICES = sorted(
    [
        "Zephyr",
        "Puck",
        "Charon",
        "Kore",
        "Fenrir",
        "Leda",
        "Orus",
        "Aoede",
        "Callirrhoe",
        "Autonoe",
        "Enceladus",
        "Iapetus",
        "Umbriel",
        "Algieba",
        "Despina",
        "Erinome",
        "Algenib",
        "Rasalgethi",
        "Laomedeia",
        "Achernar",
        "Alnilam",
        "Schedar",
        "Gacrux",
        "Pulcherrima",
        "Achird",
        "Zubenelgenubi",
        "Vindemiatrix",
        "Sadachbia",
        "Sadaltager",
        "Sulafat",
    ]
)
STATIC = Path(__file__).resolve().parent / "static"
LOGO = STATIC / "familiar.svg"
API = web.AppKey("api", object)


def summary(persona: dict, builtin: bool, overrides_builtin: bool = False) -> dict:
    return {
        **{
            key: persona[key]
            for key in ("id", "display_name", "source", "truthfulness", "chattiness")
        },
        "languages": list(persona["languages"]),
        "builtin": builtin,
        "overrides_builtin": overrides_builtin,
    }


async def body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except (ValueError, UnicodeError) as exc:
        raise ConfigError("Request body must be valid JSON") from exc
    if not isinstance(data, dict):
        raise ConfigError("Request body must be an object")
    return data


class APIService:
    def __init__(self, config: AppConfig, config_path: Path | None, token: str) -> None:
        self.config = config
        self.token = token
        self.sessions = SessionManager(config, config_path)
        self.shutdown = asyncio.Event()
        self.clients = 0
        self.last_client = time.monotonic()
        self._quota: dict | None = None
        self._quota_at = float("-inf")
        self._quota_lock = asyncio.Lock()
        self._audio_lock = asyncio.Lock()

    def personas(self) -> list[tuple[Any, bool]]:
        return list_personas(
            persona_directories(self.config.user_personas_dir, self.config.personas_dir)
        )

    def overrides_builtin(self, persona_id: str) -> bool:
        """A user persona with the same id as a shipped one (deleting it restores the original)."""
        user_copy = Path(self.config.user_personas_dir).expanduser() / f"{persona_id}.yaml"
        shipped = any(
            (directory / f"{persona_id}.yaml").is_file()
            for directory in persona_directories(
                self.config.user_personas_dir, self.config.personas_dir
            )[1:]
        )
        return user_copy.is_file() and shipped

    def persona(self, persona_id: str) -> tuple[dict, bool]:
        validate_id(persona_id)
        for persona, builtin in self.personas():
            if persona.id == persona_id:
                return asdict(persona), builtin
        raise web.HTTPNotFound(reason="Persona not found")

    def spec(self, backend: str) -> BackendConfig:
        spec = self.config.tts
        while spec is not None:
            if spec.backend == backend:
                return spec
            spec = spec.fallback
        return BackendConfig(backend, {"api_key_env": f"{backend.upper()}_API_KEY"})

    async def quota(self) -> dict | None:
        spec = self.config.tts
        while spec is not None and spec.backend != "elevenlabs":
            spec = spec.fallback
        if spec is None:
            return None
        async with self._quota_lock:
            if time.monotonic() - self._quota_at < 60:
                return self._quota
            backend = None
            try:
                backend = load("tts", spec.backend, spec.settings)
                used, limit = await backend.quota()
                self._quota = {"used": used, "limit": limit}
            except Exception:
                self._quota = None
            finally:
                self._quota_at = time.monotonic()
                if backend is not None:
                    await close_backend(backend)
            return self._quota

    async def state(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "games": [game_metadata(game) for game in available("games")],
                "personas": [
                    summary(asdict(p), builtin, self.overrides_builtin(p.id))
                    for p, builtin in self.personas()
                ],
                "languages": [{"id": code, **entry} for code, entry in languages().items()],
                "defaults": {
                    "game": self.config.game,
                    "persona": self.config.persona,
                    "language": self.config.language,
                },
                "session": self.sessions.state,
                "quota": await self.quota(),
            }
        )

    async def get_languages(self, request: web.Request) -> web.Response:
        return web.json_response([{"id": code, **entry} for code, entry in languages().items()])

    async def add_language(self, request: web.Request) -> web.Response:
        data = await body(request)
        code = data.get("code")
        entry = {"name": data.get("name"), "native": data.get("native", data.get("name"))}
        validate_language(code, entry)
        # Validate existing overrides before preserving just the user's changes.
        languages()
        overrides = read_user_data("familiar", "languages") or {}
        overrides[code] = {**(overrides.get(code) or {}), **entry}
        save_user_data("familiar", "languages", overrides)
        return web.json_response({"id": code, **entry})

    async def get_persona(self, request: web.Request) -> web.Response:
        persona, builtin = self.persona(request.match_info["id"])
        overrides = self.overrides_builtin(persona["id"])
        return web.json_response({**persona, "builtin": builtin, "overrides_builtin": overrides})

    async def put_persona(self, request: web.Request) -> web.Response:
        persona_id = request.match_info["id"]
        validate_id(persona_id)
        data = {**await body(request), "id": persona_id}
        data.pop("builtin", None)
        data.pop("overrides_builtin", None)
        data = save_persona(self.config.user_personas_dir, data)
        return web.json_response(summary(data, False, self.overrides_builtin(data["id"])))

    async def delete_persona(self, request: web.Request) -> web.Response:
        persona_id = request.match_info["id"]
        _, builtin = self.persona(persona_id)
        if builtin:
            raise ConfigError("Builtin personas cannot be deleted")
        (self.config.user_personas_dir / f"{persona_id}.yaml").unlink()
        return web.json_response({"ok": True})

    async def draft(self, request: web.Request) -> web.Response:
        data = await body(request)
        if not isinstance(data.get("character"), str) or not data["character"].strip():
            raise ConfigError("character must be a non-empty string")
        backend = None
        try:
            example = asdict(
                load_persona(
                    BUILTIN_PERSONAS_DIR,
                    "mira",
                    Path("/nonexistent"),
                )
            )
            schema = {
                "type": "object",
                "required": ["id", "display_name", "source", "prompt", "chattiness", "languages"],
                "properties": {
                    "id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{0,39}$"},
                    **{key: {"type": "string"} for key in ("display_name", "source", "prompt")},
                    "chattiness": {"type": "number", "minimum": 0, "maximum": 1},
                    "truthfulness": {"enum": list(TRUTHFULNESS_RULES)},
                    "style_rules": {"type": "array", "items": {"type": "string"}},
                    "game_hooks": {"type": "object", "additionalProperties": {"type": "string"}},
                    "languages": {
                        "type": "object",
                        "minProperties": 1,
                        "additionalProperties": {
                            "type": "object",
                            "properties": {
                                "voices": {
                                    "type": "object",
                                    "additionalProperties": {"type": ["string", "null"]},
                                }
                            },
                        },
                    },
                },
            }
            prompt = (
                "Draft a Familiar persona. Return only JSON matching this schema: "
                + json.dumps(schema)
                + "\nTruthfulness meanings: "
                + json.dumps(TRUTHFULNESS_RULES)
                + "\nComplete example: "
                + json.dumps(example, ensure_ascii=False)
            )
            spec = self.config.llm
            backend = load("llm", spec.backend, {**spec.settings, "temperature": 0.7})
            chunks = [
                chunk.text
                async for chunk in backend.stream(
                    [Message("user", json.dumps(data, ensure_ascii=False))], prompt, []
                )
            ]
            reply = "".join(chunks).strip()
            if reply.startswith("```"):
                reply = reply.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            persona = json.loads(reply)
            if not isinstance(persona, dict):
                raise ValueError("Expected a persona object")
            persona = validate_persona(sanitize_draft_voices(persona))
            return web.json_response(persona)
        except Exception as exc:
            raise web.HTTPBadGateway(reason=f"Cannot draft persona: {exc}") from exc
        finally:
            if backend is not None:
                await close_backend(backend)

    async def voices(self, request: web.Request) -> web.Response:
        backend = None
        voices = []
        error = None
        try:
            backend = load("tts", "elevenlabs", self.spec("elevenlabs").settings)
            voices = [
                {"id": item["voice_id"], "name": item["name"], "labels": item.get("labels", {})}
                for item in await backend.voices()
            ]
        except Exception as exc:
            error = str(exc)
        finally:
            if backend is not None:
                await close_backend(backend)
        return web.json_response(
            {"gemini": GEMINI_VOICES, "elevenlabs": voices, "elevenlabs_error": error}
        )

    async def test_voice(self, request: web.Request) -> web.Response:
        data = await body(request)
        name = data.get("backend")
        if name not in ("gemini", "elevenlabs"):
            raise ConfigError("backend must be gemini or elevenlabs")
        for key in ("voice", "language"):
            if not isinstance(data.get(key), str) or not data[key]:
                raise ConfigError(f"{key} must be a non-empty string")
        text = data.get("text", "Hello! I'm your Familiar companion.")
        if not isinstance(text, str) or not text.strip():
            raise ConfigError("text must be a non-empty string")
        async with self._audio_lock:
            backend = load("tts", name, self.spec(name).settings)
            player = Player()
            try:
                await player.play(backend.synthesize(text, data["voice"], data["language"]))
                await player.wait_idle()
            finally:
                await player.aclose()
                await close_backend(backend)
        return web.json_response({"ok": True})

    async def start(self, request: web.Request) -> web.Response:
        if self.sessions.state["running"]:
            raise SessionConflictError("A session is already running")
        data = await body(request)
        persona, _ = self.persona(data.get("persona", ""))
        language = data.get("language")
        if not isinstance(language, str) or language not in persona["languages"]:
            raise ConfigError("Persona does not support this language")
        game = data.get("game")
        if game is not None and (not isinstance(game, str) or game not in available("games")):
            raise ConfigError("Unknown game")
        return web.json_response(await self.sessions.start(persona["id"], language, game))

    async def stop(self, request: web.Request) -> web.Response:
        return web.json_response(await self.sessions.stop())

    async def exit(self, request: web.Request) -> web.Response:
        await self.sessions.stop()
        asyncio.get_running_loop().call_later(0.1, self.shutdown.set)
        return web.json_response({"ok": True})

    async def events(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
        )
        await response.prepare(request)
        queue = self.sessions.subscribe()
        self.clients += 1

        async def send(event: str, data: dict) -> None:
            await response.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())

        try:
            for line in list(self.sessions.lines):
                await send("line", line)
            await send("session", self.sessions.state)
            while not self.shutdown.is_set():
                try:
                    event, data = await asyncio.wait_for(queue.get(), 15)
                except TimeoutError:
                    event, data = "ping", {}
                await send(event, data)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.sessions.subscribers.discard(queue)
            self.clients -= 1
            self.last_client = time.monotonic()
        return response

    async def idle(self) -> None:
        while not self.shutdown.is_set():
            await asyncio.sleep(1)
            if (
                not self.clients
                and not self.sessions.state["running"]
                and time.monotonic() - self.last_client >= 600
            ):
                self.shutdown.set()


@web.middleware
async def middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    try:
        if request.path.startswith("/api/"):
            api = request.app[API]
            token = request.headers.get("X-Familiar-Token")
            if request.path == "/api/events":
                token = token or request.query.get("token")
            if not token or not secrets.compare_digest(token.encode(), api.token.encode()):
                raise web.HTTPForbidden(reason="Invalid or missing token")
            origin = request.headers.get("Origin")
            address = request.transport.get_extra_info("sockname")
            expected = f"http://127.0.0.1:{address[1]}"
            if origin is not None and origin != expected:
                raise web.HTTPForbidden(reason="Origin is not allowed")
        return await handler(request)
    except ConfigError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except SessionConflictError as exc:
        return web.json_response({"error": str(exc)}, status=409)
    except web.HTTPException as exc:
        return web.json_response({"error": exc.reason}, status=exc.status)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


def create_app(
    config: AppConfig | None = None, config_path: Path | None = None, token: str | None = None
) -> web.Application:
    """Construct the app without opening a socket or contacting backends."""
    api = APIService(
        config or load_config(config_path), config_path, token or secrets.token_urlsafe(32)
    )
    app = web.Application(middlewares=[middleware])
    app[API] = api
    app.add_routes(
        [
            web.get("/api/state", api.state),
            web.get("/api/languages", api.get_languages),
            web.post("/api/languages", api.add_language),
            web.post("/api/personas/draft", api.draft),
            web.get("/api/personas/{id}", api.get_persona),
            web.put("/api/personas/{id}", api.put_persona),
            web.delete("/api/personas/{id}", api.delete_persona),
            web.get("/api/voices", api.voices),
            web.post("/api/voices/test", api.test_voice),
            web.post("/api/session/start", api.start),
            web.post("/api/session/stop", api.stop),
            web.post("/api/shutdown", api.exit),
            web.get("/api/events", api.events),
        ]
    )

    async def index(request: web.Request) -> web.FileResponse:
        if not (STATIC / "index.html").is_file():
            raise web.HTTPNotFound(reason="Launcher index.html not found")
        return web.FileResponse(STATIC / "index.html")

    async def logo(request: web.Request) -> web.FileResponse:
        if not LOGO.is_file():
            raise web.HTTPNotFound(reason="Launcher logo not found")
        return web.FileResponse(LOGO)

    app.router.add_get("/", index)
    app.router.add_get("/familiar.svg", logo)
    STATIC.mkdir(exist_ok=True)
    app.router.add_static("/", STATIC)

    async def lifecycle(app: web.Application):
        task = asyncio.create_task(api.idle())
        yield
        api.shutdown.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await api.sessions.stop()

    app.cleanup_ctx.append(lifecycle)
    return app


async def serve(port: int, no_browser: bool, config_path: str | None) -> None:
    from . import runtime
    from .launcher import open_window

    existing = runtime.running_url()
    if existing is not None:
        print(f"Familiar is already running: {existing}", flush=True)
        if not no_browser:
            open_window(existing)
        return
    path = find_config_path(config_path)
    app = create_app(load_config(path), path)
    runner = web.AppRunner(app, shutdown_timeout=1)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        actual_port = runner.addresses[0][1]
        url = f"http://127.0.0.1:{actual_port}/?token={app[API].token}"
        print(url, flush=True)
        runtime.write(url, app[API].token)
        if not no_browser:
            open_window(url)
        await app[API].shutdown.wait()
    finally:
        runtime.remove()
        await runner.cleanup()


def game_metadata(game: str) -> dict:
    """Read display metadata without constructing or connecting an adapter."""
    plugin = load_class("games", game)
    return {
        "id": game,
        "name": getattr(plugin, "display_name", game.title()),
        "icon": getattr(plugin, "icon", "🎮"),
    }
