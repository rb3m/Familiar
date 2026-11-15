"""Fact-check a landmark pack: each fact is judged against its Wikipedia article's text by an
LLM (Gemini, via Familiar's own config) as supported, claim (true only as a stated claim), or
unsupported, with a corrected wording. Writes a JSON report; does not modify the pack.

Usage: uv run python scripts/factcheck_landmarks.py PACK.yaml REPORT.json [--delay 4]
"""

import argparse
import asyncio
import json
import sys
import urllib.parse
from pathlib import Path

import httpx
import yaml

from familiar import plugins
from familiar.config import load_config
from familiar.interfaces import Message

API = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "FamiliarLandmarkCheck/0.1 (personal project)"}
PROMPT = """You are a strict fact-checker. Judge each numbered FACT only against the ARTICLE text.
Verdicts: "supported" (the article states it as fact), "claim" (the article only reports it as a
claim, slogan or boast), "unsupported" (absent or contradicted). For non-supported facts give a
corrected wording that the article does support (e.g. "Morton claims that ..."), or "" to drop it.
Reply with JSON only: [{"n": 1, "verdict": "...", "fix": "..."}]

ARTICLE:
{article}

FACTS:
{facts}"""


def title_from(url: str) -> str:
    return urllib.parse.unquote(url.split("/wiki/", 1)[1]).split("#")[0].replace("_", " ")


def article_text(client: httpx.Client, title: str) -> str:
    params = {
        "action": "query",
        "format": "json",
        "prop": "extracts",
        "explaintext": 1,
        "redirects": 1,
        "titles": title,
    }
    pages = client.get(API, params=params).json()["query"]["pages"]
    return next(iter(pages.values())).get("extract", "")[:30000]


async def judge(llm, entry: dict, article: str) -> list[dict]:
    facts = "\n".join(f"{n}. {fact}" for n, fact in enumerate(entry["facts"], 1))
    prompt = PROMPT.replace("{article}", article).replace("{facts}", facts)
    reply = "".join([c.text async for c in llm.stream([Message("user", prompt)], "", [])])
    cleaned = reply.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    return json.loads(cleaned)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pack")
    parser.add_argument("report")
    parser.add_argument("--delay", type=float, default=4.0)
    args = parser.parse_args()
    entries = yaml.safe_load(Path(args.pack).read_text())
    config = load_config()
    llm = plugins.load("llm", config.llm.backend, {**config.llm.settings, "temperature": 0})
    report = []
    with httpx.Client(headers=HEADERS, timeout=30) as client:
        for entry in entries:
            article = article_text(client, title_from(entry["source"]))
            try:
                verdicts = await judge(llm, entry, article)
            except (json.JSONDecodeError, Exception) as exc:  # keep going; report the failure
                verdicts = [{"n": 0, "verdict": "error", "fix": str(exc)[:200]}]
            report.append({"id": entry["id"], "facts": entry["facts"], "verdicts": verdicts})
            bad = [v for v in verdicts if v.get("verdict") != "supported"]
            print(
                f"{entry['id']}: {len(entry['facts']) - len(bad)}/{len(entry['facts'])} ok",
                flush=True,
            )
            await asyncio.sleep(args.delay)
    Path(args.report).write_text(json.dumps(report, indent=2))
    await llm.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
