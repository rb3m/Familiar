"""Invoke agy with a private scratch directory and explicit permissions."""

import asyncio
import hashlib
import json
import os
import signal
import sys
import tempfile
from contextlib import suppress
from datetime import date
from pathlib import Path

from familiar.interfaces.data import load_data

SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "short_answer": {"type": "string"},
            "details": {"type": "string"},
            "sources": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["short_answer", "details", "sources"],
        "additionalProperties": False,
    }
)


CHECK_MARKERS = ("marker.txt", "shellmark")
CHECK_PROMPT = (
    "Permission self-test: create marker.txt in the current directory using the file writing "
    "tool, and run the shell command `touch shellmark`. Try both operations even if one is "
    "denied. Then finish."
)


class ResearchError(Exception):
    """A lookup failed; never includes subprocess output."""


def settings_for(overrides: dict | None = None) -> dict:
    return {**load_data("familiar_research", "agy").value, **(overrides or {})}


def profile_hash(settings: dict) -> str:
    """Hash of the permission rules only; prompt wording can change without a re-check."""
    profile = {
        "deny": settings["deny"],
        "scopes": {
            name: {key: scope[key] for key in ("allow", "extra_deny")}
            for name, scope in settings["scopes"].items()
        },
    }
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()


class AgyRunner:
    def __init__(self, settings: dict) -> None:
        self.settings = settings

    def permissions(self, scope: str) -> dict:
        profile = self.settings["scopes"][scope]
        return {"allow": profile["allow"], "deny": self.settings["deny"] + profile["extra_deny"]}

    def argv(
        self,
        prompt: str,
        scope: str,
        conversation_id: str | None = None,
        permissions: dict | None = None,
        schema: str = SCHEMA,
    ) -> list[str]:
        permissions = permissions or self.permissions(scope)
        args = [
            self.settings["executable"],
            "-p",
            prompt,
            "--model",
            self.settings["model"],
            "--output-format",
            "json",
            "--json-schema",
            schema,
            "--print-timeout",
            f"{self.settings['timeout_s']}s",
            "--sandbox",
            "--settings",
            json.dumps({"permissions": permissions}),
        ]
        if conversation_id:
            args.extend(["--conversation", conversation_id])
        return args

    def environment(self) -> dict[str, str]:
        """The user's environment minus our keys and our virtualenv.

        agy starts the user's MCP servers (e.g. `python3 server.py`); with Familiar's venv first
        on PATH they would get our interpreter and miss their own dependencies.
        """
        env = {k: v for k, v in os.environ.items() if k not in self.settings["strip_env"]}
        env.pop("VIRTUAL_ENV", None)
        ours = {str(Path(prefix) / "bin") for prefix in (sys.prefix, sys.exec_prefix)}
        path = env.get("PATH", "").split(os.pathsep)
        env["PATH"] = os.pathsep.join(entry for entry in path if entry not in ours)
        return env

    async def _execute(self, args: list[str], cwd: str) -> bytes:
        try:
            child = await asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                env=self.environment(),
                start_new_session=True,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            raise ResearchError("Cannot start agy") from None
        try:
            stdout, _ = await asyncio.wait_for(child.communicate(), self.settings["timeout_s"] + 15)
        except (TimeoutError, asyncio.CancelledError):
            await self._kill(child)
            raise
        if child.returncode != 0:
            raise ResearchError("agy did not exit successfully")
        return stdout

    @staticmethod
    async def _kill(child) -> None:
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        await child.wait()

    async def version(self) -> str:
        with tempfile.TemporaryDirectory(prefix="familiar-research-") as cwd:
            output = await self._execute([self.settings["executable"], "--version"], cwd)
        version = output.decode().strip()
        if not version:
            raise ResearchError("agy version is empty")
        return version

    async def run(self, question: str, scope: str, conversation_id: str | None = None):
        prompt = self.settings["prompt"].format(
            instructions=self.settings["scopes"][scope]["instructions"],
            question=question,
            today=date.today().isoformat(),
        )
        with tempfile.TemporaryDirectory(prefix="familiar-research-") as cwd:
            output = await self._execute(self.argv(prompt, scope, conversation_id), cwd)
        return self._parse(output)

    async def run_structured(self, prompt: str, schema: dict) -> dict:
        """Run a fresh web lookup with a caller-provided output contract."""
        args = self.argv(prompt, "web", schema=json.dumps(schema))
        with tempfile.TemporaryDirectory(prefix="familiar-research-") as cwd:
            output = await self._execute(args, cwd)
        result, _ = self._envelope(output)
        return result

    @staticmethod
    def _envelope(output: bytes) -> tuple[dict, str | None]:
        try:
            payload = json.loads(output)
        except (ValueError, UnicodeError):
            raise ResearchError("Invalid agy result") from None
        if not isinstance(payload, dict) or payload.get("status") != "SUCCESS":
            raise ResearchError("Unsuccessful agy result")
        result = payload.get("structured_output")
        if not isinstance(result, dict):
            raise ResearchError("Missing structured output")
        cid = payload.get("conversation_id")
        if cid is not None and not isinstance(cid, str):
            raise ResearchError("Invalid conversation id")
        return result, cid

    @staticmethod
    def _parse(output: bytes) -> tuple[dict, str | None]:
        result, cid = AgyRunner._envelope(output)
        if not all(isinstance(result.get(key), str) for key in ("short_answer", "details")):
            raise ResearchError("Invalid answer fields")
        sources = result.get("sources")
        if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
            raise ResearchError("Invalid sources")
        return {key: result[key] for key in ("short_answer", "details", "sources")}, cid

    async def check(self, scope: str) -> bool:
        """Denied under the scope profile, and proven attempted by an unrestricted control.

        Without the control, a model that simply declines to try would pass. The control runs
        in its own throwaway directory with no deny rules.
        """
        control = {"allow": self.settings["scopes"][scope]["allow"], "deny": []}
        if await self._markers_after(scope, control) != set(CHECK_MARKERS):
            return False
        return not await self._markers_after(scope, self.permissions(scope))

    async def _markers_after(self, scope: str, permissions: dict) -> set[str] | None:
        with tempfile.TemporaryDirectory(prefix="familiar-research-") as cwd:
            try:
                await self._execute(self.argv(CHECK_PROMPT, scope, permissions=permissions), cwd)
            except (ResearchError, TimeoutError):
                return None
            return {name for name in CHECK_MARKERS if (Path(cwd) / name).exists()}
