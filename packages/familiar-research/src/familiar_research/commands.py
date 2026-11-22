"""Check the installed agy permission profiles before enabling lookups."""

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from familiar.interfaces import PLUGIN_API_VERSION

from familiar_research.agy import AgyRunner, ResearchError, profile_hash, settings_for


def check_path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return base / "familiar" / "research-check.json"


class ResearchCommand:
    name = "research"
    api_version = PLUGIN_API_VERSION
    help = "Check read-only agy research permissions"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("action", choices=["check"])

    async def run(self, args: argparse.Namespace, config) -> int:
        settings = settings_for(config.research)
        runner = AgyRunner(settings)
        path = check_path()
        path.unlink(missing_ok=True)
        try:
            version = await runner.version()
        except (ResearchError, TimeoutError, UnicodeError):
            print("agy version: FAIL")
            return 1
        passed = []
        for scope in settings["scopes"]:
            ok = await runner.check(scope)
            print(f"{scope}: {'PASS' if ok else 'FAIL'}")
            passed.append(ok)
        if not passed or not all(passed):
            return 1
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        record = {
            "agy_version": version,
            "profile_hash": profile_hash(settings),
            "passed_at": datetime.now(UTC).isoformat(),
        }
        path.write_text(json.dumps(record))
        path.chmod(0o600)
        return 0
