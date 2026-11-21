"""Own one voice subprocess and its replayable output stream."""

import asyncio
import os
import signal
import sys
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from familiar.config import AppConfig
from familiar.interfaces import ConfigError
from familiar.session_settings import SessionSettings
from familiar.session_settings import default_path as default_session_path

# Allow the time-limited trip summary plus audio/backend cleanup before escalation.
SESSION_STOP_TIMEOUT_S = 20


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


class SessionConflictError(Exception):
    """A voice session is already running."""


class SessionManager:
    def __init__(
        self,
        config: AppConfig,
        config_path: Path | None = None,
        session_path: Path | None = None,
    ) -> None:
        self.config = config
        self.config_path = config_path.resolve() if config_path else None
        self.session_path = (
            Path(session_path).expanduser() if session_path else default_session_path()
        )
        self.lines: deque[dict] = deque(maxlen=200)
        self.subscribers: set[asyncio.Queue] = set()
        self.process: asyncio.subprocess.Process | None = None
        self._monitor: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._state: dict[str, Any] = dict(
            running=False,
            game=None,
            persona=None,
            language=None,
            crew=None,
            carrier=None,
            banter=None,
            started_at=None,
            exit_code=None,
        )

    @property
    def state(self) -> dict:
        return self._state.copy()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(queue)
        return queue

    def broadcast(self, event: str, data: dict) -> None:
        for queue in self.subscribers:
            queue.put_nowait((event, data))

    def command(self, persona: str, language: str, game: str | None) -> list[str]:
        command = [
            sys.executable,
            "-u",
            "-m",
            "familiar",
            "voice",
            "--persona",
            persona,
            "--language",
            language,
        ]
        if game:
            command.extend(["--game", game])
        if self.config_path:
            command.extend(["--config", str(self.config_path)])
        return command

    def session_command(self) -> list[str]:
        """The session file is the single source: no persona/game/language flags."""
        command = [
            sys.executable,
            "-u",
            "-m",
            "familiar",
            "voice",
            "--session",
            str(self.session_path),
        ]
        if self.config_path:
            command.extend(["--config", str(self.config_path)])
        return command

    async def start(self, persona: str, language: str, game: str | None = None) -> dict:
        """Legacy launcher body: a crew of one, written nowhere."""
        fields = dict(
            game=game,
            persona=persona,
            language=language,
            crew=[persona],
            carrier=persona,
            banter=None,
        )
        return await self._spawn(self.command(persona, language, game), fields)

    async def start_session(self, settings: SessionSettings) -> dict:
        fields = dict(
            game=settings.game,
            persona=settings.carrier,
            language=settings.language,
            crew=list(settings.crew),
            carrier=settings.carrier,
            banter=settings.banter,
        )
        return await self._spawn(self.session_command(), fields)

    async def _spawn(self, command: list[str], fields: dict) -> dict:
        async with self._lock:
            if self._state["running"]:
                raise SessionConflictError("A session is already running")
            if self.config.ptt.backend == "terminal":
                raise ConfigError("Assign a push-to-talk button in the Push-to-talk panel first")
            self.process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.config_path.parent if self.config_path else Path.cwd(),
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self.lines.clear()
            self._state.update(
                running=True,
                **fields,
                started_at=timestamp(),
                exit_code=None,
            )
            self.broadcast("session", self.state)
            self._monitor = asyncio.create_task(self._watch(self.process))
            return self.state

    async def _read(self, reader: asyncio.StreamReader, stream: str) -> None:
        while raw := await reader.readline():
            line = dict(
                stream=stream,
                text=raw.decode("utf-8", errors="replace").rstrip("\r\n"),
                ts=timestamp(),
            )
            self.lines.append(line)
            self.broadcast("line", line)

    async def _watch(self, process: asyncio.subprocess.Process) -> None:
        await asyncio.gather(
            self._read(process.stdout, "stdout"), self._read(process.stderr, "stderr")
        )
        code = await process.wait()
        self._state.update(running=False, exit_code=code)
        self.broadcast("session", self.state)

    async def stop(self) -> dict:
        async with self._lock:
            process = self.process
            if process is None or process.returncode is not None:
                if self._monitor:
                    await self._monitor
                return self.state
            for action, timeout in (
                (lambda: process.send_signal(signal.SIGINT), SESSION_STOP_TIMEOUT_S),
                (process.terminate, 2),
                (process.kill, None),
            ):
                try:
                    action()
                except ProcessLookupError:
                    break
                try:
                    await asyncio.wait_for(asyncio.shield(process.wait()), timeout)
                    break
                except TimeoutError:
                    continue
            if self._monitor:
                await self._monitor
            return self.state
