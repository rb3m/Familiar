"""Silent scheduled checks, with pure URL, confirmation and schedule rules."""

import asyncio
import json
import logging
import random
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from hashlib import sha1
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from familiar_research.agy import ResearchError
from familiar_research.paths import read_json, state_path, write_json
from familiar_research.topics import Area, Topic, load_topics, timestamp


def object_schema(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_STRING = {"type": "string"}
_STRINGS = {"type": "array", "items": _STRING}
_ITEM = object_schema(
    {
        "headline": _STRING,
        "summary": _STRING,
        "severity": {"type": "string", "enum": ["info", "watch", "action"]},
        "published": _STRING,
        "source_url": _STRING,
    }
)
_AREA = object_schema(
    {
        "id": _STRING,
        "searched": {"type": "boolean"},
        "sources_consulted": _STRINGS,
        "status": _STRING,
        "items": {"type": "array", "items": _ITEM},
    }
)
TOPIC_SCHEMA = object_schema({"areas": {"type": "array", "items": _AREA}})


def validate_result(value, schema: dict = TOPIC_SCHEMA) -> None:
    """Validate the small schema locally as well as requesting it from agy."""
    kind = schema["type"]
    types = {"object": dict, "array": list, "string": str, "boolean": bool}
    if type(value) is not types[kind]:
        raise ResearchError("Invalid topic result")
    if "enum" in schema and value not in schema["enum"]:
        raise ResearchError("Invalid topic severity")
    if kind == "array":
        validate_array(value, schema["items"])
        return
    if kind != "object":
        return
    if set(value) != set(schema["properties"]):
        raise ResearchError("Invalid topic fields")
    for key, child in schema["properties"].items():
        validate_result(value[key], child)


def validate_array(value: list, schema: dict) -> None:
    for entry in value:
        validate_result(entry, schema)


def bracket_host(host: str) -> str:
    if ":" in host:
        return f"[{host}]"
    return host


def canonical_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return ""
        if parts.username or parts.password:
            return ""
        host = parts.hostname.lower().rstrip(".")
        host = bracket_host(host)
        if parts.port is not None:
            host += f":{parts.port}"
        query = urlencode(
            [
                (k, v)
                for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if not k.lower().startswith("utm_")
            ]
        )
        return urlunsplit((parts.scheme.lower(), host, parts.path.rstrip("/"), query, ""))
    except (ValueError, TypeError):
        return ""


def since_date(topic: Topic, area: Area, state: dict, now: datetime) -> date:
    success = state.get("areas", {}).get(area.id, {}).get("last_success")
    if success:
        return timestamp(success).astimezone(topic.timezone).date()
    return now.astimezone(topic.timezone).date() - timedelta(days=topic.lookback_days)


def confirmed_severity(item: dict, area: Area, topic: Topic, url: str) -> str:
    if item["severity"] != "action":
        return item["severity"]
    domain = urlsplit(url).hostname or ""
    official = any(domain == d or domain.endswith("." + d) for d in area.official_domains)
    relevant = any(
        t.casefold() in (item["headline"] + " " + item["summary"]).casefold()
        for t in topic.relevance
    )
    if area.remarks and official and relevant:
        return "action"
    return "watch"


def new_item(item: dict, since: date, seen: list[str]) -> tuple[str, date] | None:
    url = canonical_url(item["source_url"])
    # Hash canonical URLs so identifiers in their paths never persist unmasked.
    if not url or sha1(url.encode()).hexdigest() in seen:
        return None
    try:
        published = datetime.fromisoformat(item["published"]).date()
    except ValueError:
        return None
    if published < since:
        return None
    return url, published


def process_area(topic, area, state, result, now, masker, seen, items) -> dict:
    previous = state.get("areas", {}).get(area.id, {})
    coverage = {
        **previous,
        "checked_at": now.isoformat(),
        "searched": result["searched"],
        "sources_consulted": [masker.mask(s) for s in result["sources_consulted"]],
        "status": masker.mask(result["status"]),
    }
    since = since_date(topic, area, state, now)
    if result["searched"]:
        coverage["last_success"] = now.isoformat()
    for item in result["items"]:
        fresh = new_item(item, since, seen)
        if fresh is None:
            continue
        url, published = fresh
        severity = confirmed_severity(item, area, topic, url)
        if severity == "action" and any(i["confirmed"] == "action" for i in items):
            severity = "watch"
        seen.append(sha1(url.encode()).hexdigest())
        items.append(
            {
                "first_seen": now.isoformat(),
                "area": area.id,
                "headline": masker.mask(item["headline"]),
                "summary": masker.mask(item["summary"]),
                "proposed": item["severity"],
                "confirmed": severity,
                "published": published.isoformat(),
                "url": masker.mask(url),
                "url_id": sha1(url.encode()).hexdigest()[:12],
                "domain": masker.mask(urlsplit(url).hostname),
            }
        )
    return coverage


def process_result(topic: Topic, state: dict, result: dict, now: datetime, masker) -> dict:
    validate_result(result)
    found = {entry["id"]: entry for entry in result["areas"]}
    seen = list(state.get("seen", []))
    items: list[dict] = []
    coverage = {
        area.id: process_area(
            topic,
            area,
            state,
            found.get(
                area.id,
                {
                    "searched": False,
                    "sources_consulted": [],
                    "status": "not searched",
                    "items": [],
                },
            ),
            now,
            masker,
            seen,
            items,
        )
        for area in topic.areas
    }
    return {
        **state,
        "last_check": now.isoformat(),
        "areas": coverage,
        "seen": seen[-500:],
        "items": (state.get("items", []) + items)[-50:],
    }


def next_slot(topic: Topic, now: datetime, jitter_s: int) -> datetime:
    local = now.astimezone(topic.timezone)
    slots = [
        datetime.combine(local.date(), t, topic.timezone).astimezone(UTC) for t in topic.check_times
    ]
    future = [slot for slot in slots if slot > now]
    if future:
        return min(future) + timedelta(seconds=jitter_s)
    tomorrow = local.date() + timedelta(days=1)
    return datetime.combine(tomorrow, topic.check_times[0], topic.timezone).astimezone(
        UTC
    ) + timedelta(seconds=jitter_s)


def initial_schedule(state: dict, now: datetime, jitter_s: int) -> dict:
    due = state.get("next_due")
    if not due or timestamp(due) <= now:
        due = (now + timedelta(seconds=90)).isoformat()
    return {**state, "jitter_s": state.get("jitter_s", jitter_s), "next_due": due}


def topic_prompt(topic: Topic, state: dict, now: datetime, template: str) -> str:
    areas = [
        {"id": a.id, "question": a.question, "since": since_date(topic, a, state, now).isoformat()}
        for a in topic.areas
    ]
    return template.format(
        today=now.astimezone(topic.timezone).date().isoformat(),
        context=topic.context,
        areas=json.dumps(areas),
    )


class TopicWatch:
    def __init__(self, service):
        self.service = service
        self.check_task: asyncio.Task | None = None
        self.initialized: set[str] = set()

    async def interrupt(self):
        if self.check_task is None:
            return
        self.check_task.cancel()
        with suppress(asyncio.CancelledError):
            await self.check_task

    async def run(self):
        while not self.service._closed:
            await self.poll(datetime.now(UTC))
            await asyncio.sleep(60)

    async def poll(self, now: datetime):
        for topic in load_topics():
            try:
                await self.poll_topic(topic, now)
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                logging.getLogger(__name__).warning("Cannot read or save topic state")

    async def poll_topic(self, topic: Topic, now: datetime):
        if now >= topic.active_until:
            return
        path = state_path(topic.id)
        state = read_json(path)
        jitter_s = state.get("jitter_s")
        if jitter_s is None:
            jitter_s = random.randint(0, 900)
        if topic.id not in self.initialized:
            state = initial_schedule(state, now, jitter_s)
            write_json(path, state)
            self.initialized.add(topic.id)
        if timestamp(state["next_due"]) > now:
            return
        if self.service._request_lock.locked():
            return
        job = self.service._job
        if job is not None and not job.done():
            return
        if self.check_task is not None and not self.check_task.done():
            return
        self.check_task = asyncio.create_task(self._check_safely(topic, state))

    async def _check_safely(self, topic: Topic, state: dict):
        try:
            await self.check(topic, state)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            logging.getLogger(__name__).warning("Cannot read or save topic state")

    async def check(self, topic: Topic, state: dict):
        now = datetime.now(UTC)
        if now >= topic.active_until:
            return
        try:
            prompt = topic_prompt(topic, state, now, self.service.settings["topic_prompt"])
            result = await self.service.runner.run_structured(prompt, TOPIC_SCHEMA)
            state = process_result(topic, state, result, now, self.service.masker)
        except (ResearchError, TimeoutError):
            missing = {"areas": []}
            state = process_result(topic, state, missing, now, self.service.masker)
            state["areas"] = {
                key: {**value, "status": "check failed"} for key, value in state["areas"].items()
            }
        state["next_due"] = next_slot(topic, datetime.now(UTC), state["jitter_s"]).isoformat()
        write_json(state_path(topic.id), state)
