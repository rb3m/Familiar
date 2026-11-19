"""One active playback backend, with cached source availability."""

import asyncio

from .player import Health


class SourcedPlayer:
    def __init__(self, backends):
        if not backends:
            raise ValueError("At least one music backend is required")
        self.backends = dict(backends)
        self.active = next(iter(backends))
        self._health = {}
        self._volume = None

    @property
    def backend(self):
        return self.backends[self.active]

    @property
    def capabilities(self):
        return self.backend.capabilities

    def available(self, prefix):
        health = self._health.get(prefix, Health("ok"))
        return prefix in self.backends and health.status == "ok"

    async def _check(self, prefix, backend):
        try:
            self._health[prefix] = await backend.health()
        except Exception as exc:
            self._health[prefix] = Health("unavailable", str(exc))

    async def health(self):
        await asyncio.gather(*(self._check(p, b) for p, b in self.backends.items()))
        if any(self.available(p) for p in self.backends):
            return Health("ok")
        return Health("unavailable", "; ".join(h.hint for h in self._health.values()))

    async def play_now(self, uri):
        prefix = uri.partition(":")[0]
        if prefix not in self.backends:
            raise ValueError(f"Unsupported music URI: {uri}")
        target = self.backends[prefix]
        if prefix != self.active:
            await self._prepare_switch(target)
        if self._volume is not None:
            await target.set_volume(self._volume)
        await target.play_now(uri)
        self.active = prefix

    async def _prepare_switch(self, target):
        await self._pause_old()
        if "clear" in target.capabilities:
            await target.clear_upcoming()

    async def _pause_old(self):
        try:
            await self.backend.pause()
        except Exception:
            await self._check(self.active, self.backend)
            if self.available(self.active):
                raise

    async def enqueue(self, uri):
        if uri.partition(":")[0] != self.active:
            raise ValueError("Queued music must match the active source")
        await self.backend.enqueue(uri)

    async def now_playing(self):
        if not self.available(self.active):
            return None
        return await self.backend.now_playing()

    async def skip(self):
        await self.backend.skip()

    async def pause(self):
        await self.backend.pause()

    async def resume(self):
        await self.backend.resume()

    async def get_volume(self):
        return await self.backend.get_volume()

    async def set_volume(self, level):
        await self.backend.set_volume(level)
        self._volume = level

    async def queue_ids(self):
        return await self.backend.queue_ids()

    async def clear_upcoming(self):
        await self.backend.clear_upcoming()

    async def like(self):
        await self.backend.like()

    async def dislike(self):
        await self.backend.dislike()

    async def aclose(self):
        results = await asyncio.gather(
            *(backend.aclose() for backend in self.backends.values()), return_exceptions=True
        )
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise errors[0]
