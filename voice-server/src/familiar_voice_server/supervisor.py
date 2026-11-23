"""Small always-on lease supervisor. Deliberately independent of MLX and numpy."""

import argparse
import asyncio
import contextlib
import hmac
import logging
import os
import socket
import sys
import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from .config import Config, ConfigError, load_config

LOG = logging.getLogger(__name__)
AUDIO_TYPE = "audio/L16; rate=24000; channels=1"
Spawn = Callable[[Config, int], Awaitable[asyncio.subprocess.Process]]
# A failed worker is retried on lease renewals no more often than this (avoids crash loops).
RETRY_FAILED_S = 30.0


async def spawn_worker(config: Config, port: int) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "familiar_voice_server.worker",
        "--config",
        str(config.path),
        "--port",
        str(port),
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Supervisor:
    def __init__(self, config: Config, token: str, spawn: Spawn = spawn_worker):
        self.config = config
        self.token = token
        self.spawn = spawn
        self.state = "idle"
        self.busy = False
        self.leases: dict[str, float] = {}
        self.idle_since: float | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.port = 0
        self.session: ClientSession | None = None
        self.lifecycle: asyncio.Task | None = None
        self.start_task: asyncio.Task | None = None
        self.closing = False
        self.failed_at: float | None = None
        self.retry_failed_s = RETRY_FAILED_S

    def expire(self) -> None:
        now = time.monotonic()
        expired = [key for key, expiry in self.leases.items() if expiry <= now]
        last_expiry = max((self.leases[key] for key in expired), default=now)
        for key in expired:
            del self.leases[key]
        if not self.leases and self.idle_since is None:
            self.idle_since = last_expiry

    def ensure_worker(self) -> None:
        if self.state not in {"idle", "failed"} or not self.leases or self.closing:
            return
        self.state = "loading"
        self.start_task = asyncio.create_task(self.start_worker())

    async def start_worker(self) -> None:
        try:
            self.port = free_port()
            self.process = await self.spawn(self.config, self.port)
        except Exception:
            LOG.exception("Cannot start voice worker")
            self.mark_failed()

    def mark_failed(self) -> None:
        self.state = "failed"
        self.failed_at = time.monotonic()

    def retry_failed(self) -> None:
        """Renewals restart a crashed worker, at most every retry_failed_s."""
        if self.state != "failed" or self.failed_at is None:
            return
        if time.monotonic() - self.failed_at < self.retry_failed_s:
            return
        self.ensure_worker()

    async def stop_worker(self) -> None:
        process = self.process
        if process is None:
            return
        self.state = "stopping"
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), self.config.worker.stop_timeout_s)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        self.process = None
        self.state = "idle"
        self.ensure_worker()

    async def check_worker(self) -> None:
        process = self.process
        if process is None:
            return
        if process.returncode is not None:
            await process.wait()
            self.process = None
            self.mark_failed()
            LOG.error("Voice worker exited with status %s", process.returncode)
            return
        if self.state == "loading":
            try:
                async with self.session.get(
                    f"http://127.0.0.1:{self.port}/ready",
                    timeout=ClientTimeout(total=0.2),
                ) as response:
                    if response.status == 200:
                        self.state = "ready"
            except (ClientError, TimeoutError):
                pass
        if self.leases or self.busy or self.idle_since is None:
            return
        if time.monotonic() - self.idle_since >= self.config.lease.grace_s:
            await self.stop_worker()

    async def sweep(self) -> None:
        interval = min(0.1, self.config.lease.ttl_s / 4, max(self.config.lease.grace_s / 4, 0.005))
        while True:
            self.expire()
            await self.check_worker()
            await asyncio.sleep(interval)

    async def context(self, app: web.Application):
        async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=2)) as session:
            self.session = session
            self.lifecycle = asyncio.create_task(self.sweep())
            try:
                yield
            finally:
                self.closing = True
                self.lifecycle.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.lifecycle
                if self.start_task is not None:
                    await self.start_task
                await self.stop_worker()

    def error(self, message: str, status: int) -> web.Response:
        return web.json_response({"error": message, "state": self.state}, status=status)

    async def create_lease(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except ValueError:
            return self.error("lease body must be JSON", 400)
        client = body.get("client") if isinstance(body, dict) else None
        if not isinstance(client, str) or not client.strip():
            return self.error("client must be a non-empty string", 400)
        self.expire()
        lease_id = str(uuid4())
        self.leases[lease_id] = time.monotonic() + self.config.lease.ttl_s
        self.idle_since = None
        self.ensure_worker()
        return web.json_response(
            {"lease_id": lease_id, "ttl_s": self.config.lease.ttl_s, "state": self.state},
            status=201,
        )

    async def renew_lease(self, request: web.Request) -> web.Response:
        self.expire()
        lease_id = request.match_info["id"]
        if lease_id not in self.leases:
            return self.error("unknown or expired lease", 404)
        self.leases[lease_id] = time.monotonic() + self.config.lease.ttl_s
        self.retry_failed()
        return web.json_response({"ttl_s": self.config.lease.ttl_s, "state": self.state})

    async def delete_lease(self, request: web.Request) -> web.Response:
        self.expire()
        self.leases.pop(request.match_info["id"], None)
        if not self.leases and self.idle_since is None:
            self.idle_since = time.monotonic()
        return web.Response(status=204)

    async def status(self, request: web.Request) -> web.Response:
        self.expire()
        return web.json_response(
            {
                "state": self.state,
                "leases": len(self.leases),
                "model": self.config.model.id,
                "voices": list(self.config.voices),
                "busy": self.busy,
            }
        )

    async def speech(self, request: web.Request) -> web.StreamResponse:
        try:
            body = self.config.speech(await request.json())
        except ValueError as exc:
            return self.error(str(exc), 400)
        if self.state != "ready" or self.busy:
            return self.error("busy" if self.busy else "worker is not ready", 503)
        self.busy = True
        output = None
        disconnected = False
        try:
            url = f"http://127.0.0.1:{self.port}/speech"
            async with self.session.post(url, json=body) as upstream:
                if upstream.status != 200:
                    status = 503 if upstream.status == 503 else 500
                    return self.error("worker could not synthesize speech", status)
                async for chunk in upstream.content.iter_any():
                    if disconnected:
                        continue  # Drain the worker so its real in-flight lifetime stays protected.
                    try:
                        if output is None:
                            output = web.StreamResponse(headers={"Content-Type": AUDIO_TYPE})
                            await output.prepare(request)
                        await output.write(chunk)
                    except (ConnectionError, RuntimeError):
                        disconnected = True
                if output is None:
                    return self.error("worker returned no audio", 500)
                if not disconnected:
                    await output.write_eof()
                return output
        except (ClientError, TimeoutError, ConnectionError):
            LOG.exception("Voice worker stream failed")
            if output is None:
                return self.error("synthesis failed", 500)
            if request.transport is not None:
                request.transport.close()
            return output
        finally:
            self.busy = False


def create_app(config: Config, token: str, spawn: Spawn = spawn_worker) -> web.Application:
    if not token.strip():
        raise ValueError("voice server token must not be empty")
    supervisor = Supervisor(config, token, spawn)

    @web.middleware
    async def auth(request: web.Request, handler):
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
            return supervisor.error("unauthorized", 401)
        return await handler(request)

    app = web.Application(middlewares=[auth])
    app[SUPERVISOR] = supervisor
    app.cleanup_ctx.append(supervisor.context)
    app.add_routes(
        [
            web.post("/v1/leases", supervisor.create_lease),
            web.put("/v1/leases/{id}", supervisor.renew_lease),
            web.delete("/v1/leases/{id}", supervisor.delete_lease),
            web.get("/v1/status", supervisor.status),
            web.post("/v1/speech", supervisor.speech),
        ]
    )
    return app


SUPERVISOR = web.AppKey("supervisor", Supervisor)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="~/.config/familiar-voice/config.yaml")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        parser.error(str(exc))
    token = os.environ.get(config.token_env, "")
    if not token.strip():
        parser.error(f"required token environment variable {config.token_env} is missing or empty")
    logging.basicConfig(level=logging.INFO)
    web.run_app(create_app(config, token), host=config.listen.host, port=config.listen.port)


if __name__ == "__main__":
    main()
