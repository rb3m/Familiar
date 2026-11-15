"""Shared async HTTP setup and error handling for ElevenLabs."""

import os
from collections.abc import Iterator
from contextlib import contextmanager

import httpx

from familiar.interfaces import BackendError, ConfigError


def make_client(settings: dict) -> httpx.AsyncClient:
    variable = settings.get("api_key_env", "ELEVENLABS_API_KEY")
    if not isinstance(variable, str) or not variable:
        raise ConfigError("ElevenLabs api_key_env must name an environment variable")
    key = os.environ.get(variable)
    if not key:
        raise ConfigError(f"Environment variable {variable!r} is missing or empty")
    return httpx.AsyncClient(
        base_url="https://api.elevenlabs.io",
        headers={"xi-api-key": key},
        timeout=float(settings.get("timeout_seconds", 30)),
    )


async def check_response(response: httpx.Response) -> None:
    if response.is_success:
        return
    body = (await response.aread()).decode("utf-8", errors="replace")
    code = response.status_code
    if code in {401, 402} and "quota" in body.lower():
        raise BackendError("ElevenLabs quota exhausted", retryable=False)
    raise BackendError(
        f"ElevenLabs request failed ({code}): {body}",
        retryable=code == 429 or code >= 500,
    )


@contextmanager
def backend_errors() -> Iterator[None]:
    try:
        yield
    except httpx.TransportError as exc:
        raise BackendError(f"ElevenLabs connection failed: {exc}", retryable=True) from exc


class ElevenLabsClient:
    def __init__(self, settings: dict) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = make_client(self.settings)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
