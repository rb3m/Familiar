"""HTTP setup, settings validation and voice server error translation."""

import math
import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager

import httpx

from familiar.interfaces import BackendError, ConfigError

DEFAULTS = {
    "token_env": "FAMILIAR_VOICE_TOKEN",
    "client_name": socket.gethostname(),
    "first_audio_deadline_ms": 1000,
    "patient_first_audio_deadline_ms": 2000,
    "patient_ready_wait_s": 15,
    "stall_margin_ms": 300,
    "renew_s": 10,
    "park_after_misses": 3,
    "park_window_s": 120,
    "park_s": 300,
    "stall_park_s": 20,
    "timeout_seconds": 30,
}


def validate(settings: dict) -> dict:
    settings = {**DEFAULTS, **settings}
    for key in ("url", "token_env", "client_name"):
        value = settings.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"local.{key} must be a non-empty string")
    try:
        url = httpx.URL(settings["url"])
    except httpx.InvalidURL as exc:
        raise ConfigError("local.url must be a valid HTTP(S) URL") from exc
    if url.scheme not in {"http", "https"} or not url.host or url.userinfo:
        raise ConfigError("local.url must be an HTTP(S) URL without credentials")
    for key in DEFAULTS.keys() - {"token_env", "client_name"}:
        value = settings[key]
        if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
            raise ConfigError(f"local.{key} must be a finite positive number")
    if type(settings["park_after_misses"]) is not int:
        raise ConfigError("local.park_after_misses must be a positive integer")
    if not os.environ.get(settings["token_env"]):
        raise ConfigError(f"Environment variable {settings['token_env']!r} is missing or empty")
    return settings


def make_client(settings: dict) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings["url"].rstrip("/"),
        headers={"Authorization": f"Bearer {os.environ[settings['token_env']]}"},
        timeout=settings["timeout_seconds"],
    )


async def check_response(response: httpx.Response) -> None:
    if response.is_success:
        return
    await response.aread()
    code = response.status_code
    raise BackendError(f"Local voice request failed ({code})", retryable=code == 429 or code >= 500)


@contextmanager
def backend_errors() -> Iterator[None]:
    try:
        yield
    except httpx.TransportError as exc:
        raise BackendError(
            f"Local voice connection failed: {str(exc) or type(exc).__name__}", retryable=True
        ) from exc
