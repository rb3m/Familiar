"""Shared Gemini client construction and speech error translation."""

import os
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
from google import genai
from google.genai import errors, types
from google.genai._interactions import APIConnectionError
from google.genai._interactions import APIError as InteractionAPIError

from familiar.backends.models import model_defaults
from familiar.interfaces import BackendError, ConfigError


def make_client(settings: dict, api_version: str | None = "v1beta") -> genai.Client:
    variable = settings.get("api_key_env", "GEMINI_API_KEY")
    if not isinstance(variable, str) or not variable:
        raise ConfigError("Gemini api_key_env must name an environment variable")
    key = os.environ.get(variable)
    if not key:
        raise ConfigError(f"Environment variable {variable!r} is missing or empty")
    options = types.HttpOptions(
        api_version=api_version,
        timeout=int(float(settings.get("timeout_seconds", 30)) * 1000),
    )
    return genai.Client(api_key=key, http_options=options)


@contextmanager
def backend_errors() -> Iterator[None]:
    try:
        yield
    except errors.APIError as exc:
        code = exc.code or 0
        raise BackendError(
            f"Gemini speech failed ({code}): {exc.message}",
            retryable=code == 429 or code >= 500,
        ) from exc
    except InteractionAPIError as exc:
        code = getattr(exc, "status_code", 0)
        raise BackendError(
            f"Gemini speech failed ({code}): {exc}",
            retryable=isinstance(exc, APIConnectionError) or code == 429 or code >= 500,
        ) from exc
    except (TimeoutError, httpx.TransportError) as exc:
        raise BackendError(f"Gemini speech connection failed: {exc}", retryable=True) from exc


class GeminiSpeechClient:
    """Lazy v1beta client lifecycle shared by clip and streaming speech."""

    model_kind: str

    def __init__(self, settings: dict) -> None:
        self.settings = settings
        self.model = settings.get("model", model_defaults("gemini")[self.model_kind])
        self._client: genai.Client | None = None

    def _get_client(self) -> genai.Client:
        if self._client is None:
            self._client = make_client(self.settings)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aio.aclose()
            self._client = None
