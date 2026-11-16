"""Offline pack operations and injectable Wikipedia/research curation steps."""

import asyncio
import json
import math
import re
import urllib.parse
from collections.abc import Awaitable, Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

import httpx
import yaml
from familiar.interfaces import ConfigError, Message

from .knowledge import Landmark, _landmark
from .places import STATE_NAMES

Entry = dict[str, Any]
Runner = Callable[[Sequence[str], Path], Awaitable[None]]
API = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "FamiliarLandmarkCheck/0.1 (personal project)"}
DEFAULT_COMMAND = ("agy", "-p", "PROMPT", "--dangerously-skip-permissions")
DEFAULT_PACK = Path(__file__).parent / "knowledge/ats/landmarks.yaml"


def owned_states(game_dir: Path) -> set[str]:
    """Detect installed ATS state archives, including the base-game California."""
    if not game_dir.is_dir():
        raise ConfigError(f"Game directory does not exist: {game_dir}")
    legacy = {"NEVADA": "NV", "ARIZONA": "AZ"}
    codes = {p.stem[4:].upper() for p in game_dir.glob("dlc_*.scs") if p.is_file()}
    return {"CA"} | {legacy.get(code, code) for code in codes} & STATE_NAMES.keys()


def pack_states(landmarks: Iterable[Landmark | Entry]) -> set[str]:
    return {p["state"] if isinstance(p, dict) else p.state for p in landmarks}


def read_pack(path: Path) -> list[Entry]:
    """Validate untrusted research output before using or merging it."""
    try:
        entries = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read pack {path}: {exc}") from exc
    if not isinstance(entries, list):
        raise ConfigError(f"{path}: pack must be a YAML list")
    seen: set[str] = set()
    for entry in entries:
        landmark = _landmark(entry, path)
        if landmark.id in seen:
            raise ConfigError(f"{path}: duplicate id {landmark.id}")
        seen.add(landmark.id)
    return entries


def write_pack(path: Path, entries: list[Entry], *, header: str = "") -> None:
    path.write_text(
        header + yaml.safe_dump(entries, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371 * math.asin(math.sqrt(h))


def title_from(url: str) -> str | None:
    marker = "wikipedia.org/wiki/"
    if marker not in url:
        return None
    return urllib.parse.unquote(url.split(marker, 1)[1]).split("#")[0].replace("_", " ")


def wiki_coordinates(
    client: httpx.Client, titles: list[str]
) -> dict[str, tuple[float, float] | str | None]:
    """Resolve redirects; map each requested title to (lat, lon), None (no coords) or 'missing'."""
    params = {
        "action": "query",
        "format": "json",
        "prop": "coordinates",
        "redirects": 1,
        "colimit": "max",  # default is 10 pages per request
        "titles": "|".join(titles),
    }
    response = client.get(API, params=params, headers=HEADERS)
    response.raise_for_status()
    data = response.json()["query"]
    normalized = {r["from"]: r["to"] for r in data.get("normalized", [])}
    redirects = {r["from"]: r["to"] for r in data.get("redirects", [])}
    pages = {p["title"]: p for p in data["pages"].values()}
    result = {}
    for title in titles:
        final = normalized.get(title, title)
        final = redirects.get(final, final)
        result[title] = _page_coordinates(pages.get(final, {"missing": True}))
    return result


def _page_coordinates(page: dict) -> tuple[float, float] | str | None:
    if "missing" in page:
        return "missing"
    coords = page.get("coordinates")
    return (coords[0]["lat"], coords[0]["lon"]) if coords else None


def check(entry: Entry, found: tuple[float, float] | str | None, max_km: float) -> str:
    if found == "missing":
        return "NO-ARTICLE"
    if found is None:
        return "NO-COORDS"
    distance = haversine_km((entry["lat"], entry["lon"]), found)
    return "OK" if distance <= max_km else f"OFF {distance:.0f} km"


def verify_coordinates(
    entries: list[Entry], client: httpx.Client, max_km: float = 5
) -> dict[str, str]:
    titled = [(entry, title_from(entry.get("source", ""))) for entry in entries]
    titles = sorted({title for _, title in titled if title})
    found: dict[str, tuple[float, float] | str | None] = {}
    for start in range(0, len(titles), 40):
        found.update(wiki_coordinates(client, titles[start : start + 40]))
    return {
        entry["id"]: check(entry, found.get(title), max_km) if title else "NOT-WIKIPEDIA"
        for entry, title in titled
    }


PROMPT = """You are a strict fact-checker. Judge each numbered FACT only against the ARTICLE text.
Verdicts: "supported" (the article states it as fact), "claim" (the article only reports it as a
claim, slogan or boast), "unsupported" (absent or contradicted). For non-supported facts give a
corrected wording that the article does support (e.g. "Morton claims that ..."), or "" to drop it.
Reply with JSON only: [{"n": 1, "verdict": "...", "fix": "..."}]

ARTICLE:
{article}

FACTS:
{facts}"""


def _fetch_extract(client: httpx.Client, title: str) -> str:
    params = {
        "action": "query",
        "format": "json",
        "prop": "extracts",
        "explaintext": 1,
        "redirects": 1,
        "titles": title,
    }
    response = client.get(API, params=params, headers=HEADERS)
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise ValueError(str(data["error"]))
    pages = data["query"]["pages"]
    return next(iter(pages.values())).get("extract", "")[:30000]


async def judge(llm: Any, entry: Entry, article: str) -> list[dict]:
    facts = "\n".join(f"{n}. {fact}" for n, fact in enumerate(entry["facts"], 1))
    prompt = PROMPT.replace("{article}", article).replace("{facts}", facts)
    reply = "".join([c.text async for c in llm.stream([Message("user", prompt)], "", [])])
    cleaned = reply.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    verdicts = json.loads(cleaned)
    if not isinstance(verdicts, list) or any(not isinstance(v, dict) for v in verdicts):
        raise ValueError("Expected a list of verdict mappings")
    for verdict in verdicts:
        if (
            type(verdict.get("n")) is not int
            or not 1 <= verdict["n"] <= len(entry["facts"])
            or verdict.get("verdict") not in {"supported", "claim", "unsupported"}
            or not isinstance(verdict.get("fix", ""), str)
        ):
            raise ValueError("Invalid fact verdict")
    return verdicts


async def article_text(
    client: httpx.Client,
    title: str,
    attempts: int = 4,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str:
    """Back off on HTTP failures, API errors and non-JSON responses."""
    for attempt in range(attempts):
        try:
            return _fetch_extract(client, title)
        except (httpx.HTTPError, ValueError, KeyError):
            if attempt + 1 < attempts:
                await sleep(5 * 2**attempt)
    return ""


async def factcheck(
    entries: list[Entry],
    llm: Any,
    client: httpx.Client,
    delay_s: float,
    report_path: Path,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> list[Entry]:
    """Resume a report and persist every result, including per-landmark failures."""
    report = json.loads(report_path.read_text()) if report_path.is_file() else []
    done = {item["id"] for item in report}
    report_path.parent.mkdir(parents=True, exist_ok=True)
    for entry in (entry for entry in entries if entry["id"] not in done):
        try:
            title = title_from(entry["source"])
            article = await article_text(client, title, sleep=sleep) if title else ""
            if not article:
                raise ValueError("Article unavailable")
            verdicts = await judge(llm, entry, article)
        except Exception as exc:
            verdicts = [{"n": 0, "verdict": "error", "fix": str(exc)[:200]}]
        report.append({"id": entry["id"], "facts": entry["facts"], "verdicts": verdicts})
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        await sleep(delay_s)
    return report


# Checker phrasing that should not end up in the companion's facts.
_META = re.compile(
    r"^(the article (states|says|notes|mentions|reports|describes)( that)?"
    r"|according to the article,?)\s+",
    re.I,
)


# "..., but does not mention X": the part before is the supported fact.
_UNSUPPORTED_TAIL = re.compile(
    r",?\s+(but|though|although)\s+(it\s+|the article\s+)?(does not|doesn't)\b.*$", re.I
)


def clean(fix: str) -> str:
    """Strip checker commentary; drop the fix when commentary remains."""
    text = _UNSUPPORTED_TAIL.sub("", _META.sub("", fix.strip())).rstrip(" ,;")
    if not text or re.search(r"\barticle\b", text, re.I):
        return ""
    text = text if text.endswith((".", "!", "?", '"')) else text + "."
    return text[:1].upper() + text[1:]


def corrected_facts(facts: list[str], verdicts: list[dict]) -> list[str] | None:
    """None when the check errored; otherwise the surviving, corrected facts."""
    by_number = {v.get("n"): v for v in verdicts}
    if any(v.get("verdict") == "error" for v in verdicts):
        return None
    result = []
    for number, fact in enumerate(facts, 1):
        verdict = by_number.get(number, {"verdict": "unsupported", "fix": ""})
        replacement = (
            fact if verdict.get("verdict") == "supported" else clean(verdict.get("fix", ""))
        )
        if replacement:
            result.append(replacement)
    return result


def apply_report(entries: list[Entry], report: list[Entry]) -> tuple[list[Entry], list[str]]:
    checks = {item["id"]: item for item in report}
    kept, left_out = [], []
    for entry in entries:
        item = checks.get(entry["id"])
        facts = corrected_facts(entry["facts"], item["verdicts"]) if item else None
        if not facts:
            left_out.append(entry["id"])
            continue
        kept.append({**entry, "facts": facts})
    return kept, left_out


def merge(pack_path: Path, new_entries: list[Entry]) -> tuple[int, int]:
    """Append unique entries while retaining the exact leading comment block."""
    text = pack_path.read_text(encoding="utf-8") if pack_path.exists() else ""
    lines = text.splitlines(keepends=True)
    boundary = next(
        (n for n, line in enumerate(lines) if line.strip() and not line.startswith("#")),
        len(lines),
    )
    header = "".join(lines[:boundary])
    entries = yaml.safe_load(text) or []
    ids = {entry["id"] for entry in entries}
    names = {(entry["name"], entry["state"]) for entry in entries}
    added = 0
    for entry in new_entries:
        key = (entry["name"], entry["state"])
        if entry["id"] in ids or key in names:
            continue
        entries.append(entry)
        ids.add(entry["id"])
        names.add(key)
        added += 1
    write_pack(pack_path, entries, header=header)
    return added, len(new_entries) - added


async def _run_research(command: Sequence[str], workdir: Path) -> None:
    process = await asyncio.create_subprocess_exec(*command, cwd=workdir)
    try:
        await asyncio.wait_for(process.wait(), timeout=30 * 60)
    except (TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise ConfigError(f"Research command exited with status {process.returncode}")


async def research(
    states: Sequence[str],
    per_state: int,
    workdir: Path,
    command: Sequence[str] = DEFAULT_COMMAND,
    *,
    runner: Runner = _run_research,
) -> list[Entry]:
    if per_state < 1:
        raise ConfigError("per-state must be positive")
    if not states or set(states) - STATE_NAMES.keys():
        raise ConfigError("Research requires valid USPS state codes")
    template = (Path(__file__).parent / "knowledge/research_prompt.md").read_text()
    prompt = template.format(states=" ".join(states), per_state=per_state)
    workdir.mkdir(parents=True, exist_ok=True)
    # A failed/no-output run must never consume stale research from a previous run.
    output = workdir / "landmarks.yaml"
    if output.exists():
        raise ConfigError(f"Research output already exists: {output}; choose a new workdir")
    await runner([prompt if arg == "PROMPT" else arg for arg in command], workdir)
    entries = read_pack(output)
    if pack_states(entries) - set(states):
        raise ConfigError("Research returned landmarks outside the requested states")
    return entries
