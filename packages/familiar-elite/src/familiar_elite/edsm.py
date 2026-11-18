"""On-demand, anonymous EDSM reads with serial pacing and persistent cache."""

import asyncio
import hashlib
import json
import math
import os
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from importlib.metadata import version
from pathlib import Path

import httpx
from familiar.interfaces import ConfigError, load_data


def positive(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ConfigError(f"Elite {name} must be positive and finite")
    return float(value)


@dataclass(frozen=True)
class Reply:
    data: list | dict | None
    offline: bool = False


class EdsmClient:
    def __init__(
        self, settings, *, transport=None, clock=time.time, sleeper=asyncio.sleep, cache_dir=None
    ):
        self.enabled = settings.get("edsm_enabled", True)
        if type(self.enabled) is not bool:
            raise ConfigError("Elite edsm_enabled must be a boolean")
        self.interval = positive(settings.get("edsm_min_interval_s", 1.0), "edsm_min_interval_s")
        self.clock, self.sleep = clock, sleeper
        root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        self.cache_dir = Path(cache_dir) if cache_dir is not None else root / "familiar/elite/edsm"
        self.transport = transport
        self._client = None
        self._lock = asyncio.Lock()
        self._last_request = None
        self._retry_at = 0.0

    def _cache_path(self, endpoint, params):
        key = json.dumps([endpoint, sorted(params.items())], separators=(",", ":"))
        return self.cache_dir / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def _read(self, path):
        try:
            record = json.loads(path.read_text())
            if not isinstance(record.get("data"), (list, dict)):
                return None
            if not isinstance(record.get("time"), (int, float)):
                return None
            return record
        except (OSError, ValueError, AttributeError):
            return None

    def _write(self, path, data):
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"time": self.clock(), "data": data}, stream)
            temporary.replace(path)
        except OSError:
            pass
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)

    async def fetch(self, kind, params, deadline):
        """Return stale cached data on failure; every failure is marked offline."""
        data = load_data("familiar_elite", "edsm").value
        endpoint = data["endpoints"][kind]
        params = {**endpoint.get("flags", {}), **params}
        path = self._cache_path(endpoint["path"], params)
        async with self._lock:
            cached = self._read(path)
            if cached and self.clock() - cached["time"] < endpoint["ttl_s"]:
                return Reply(cached["data"], offline=not self.enabled)
            if not self.enabled:
                return Reply(cached["data"] if cached else None, True)
            try:
                result = await self._request(data["base_url"], endpoint["path"], params, deadline)
                self._write(path, result)
                return Reply(result)
            except (httpx.HTTPError, ValueError, TimeoutError):
                return Reply(cached["data"] if cached else None, True)

    async def _paced_get(self, base_url, endpoint, params, deadline):
        delay = 0.0
        if self._last_request is not None:
            delay = max(0.0, self._last_request + self.interval - self.clock())
        delay = max(delay, self._retry_at - self.clock())
        if self.clock() + delay >= deadline:
            raise TimeoutError("EDSM action budget exhausted")
        await self.sleep(delay)
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": f"Familiar/{version('familiar-elite')}"},
                transport=self.transport,
            )
        self._last_request = self.clock()
        remaining = max(0.001, deadline - self.clock())
        async with asyncio.timeout(remaining):
            response = await self._client.get(
                base_url.rstrip("/") + "/" + endpoint, params=params, timeout=remaining
            )
        if response.status_code == 429 or response.status_code >= 500:
            self._retry_at = self.clock() + self._retry_delay(response)
        return response

    def _retry_delay(self, response):
        value = response.headers.get("Retry-After", str(self.interval))
        try:
            return max(0.0, float(value))
        except ValueError:
            return max(0.0, parsedate_to_datetime(value).timestamp() - self.clock())

    async def _backoff(self, deadline):
        delay = max(0.0, self._retry_at - self.clock())
        if not math.isfinite(delay) or self.clock() + delay >= deadline:
            raise TimeoutError("EDSM retry exceeds action budget")
        await self.sleep(delay)

    async def _request(self, base_url, endpoint, params, deadline):
        response = await self._paced_get(base_url, endpoint, params, deadline)
        if response.status_code == 429 or response.status_code >= 500:
            await self._backoff(deadline)
            response = await self._paced_get(base_url, endpoint, params, deadline)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, (list, dict)):
            raise ValueError("Invalid EDSM payload")
        return result

    async def aclose(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None
