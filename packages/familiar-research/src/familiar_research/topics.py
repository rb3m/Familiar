"""Load private topic definitions; reject invalid files without logging their content."""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from familiar_research.paths import topics_dir

_LOG = logging.getLogger(__name__)
_ID = re.compile(r"[a-z0-9-]+\Z")
_DOMAIN = re.compile(r"[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?\Z")


@dataclass(frozen=True)
class Area:
    id: str
    question: str
    remarks: bool
    official_domains: tuple[str, ...]


@dataclass(frozen=True)
class Topic:
    id: str
    title: str
    timezone: ZoneInfo
    active_until: datetime
    check_times: tuple[time, ...]
    lookback_days: int
    context: str
    relevance: tuple[str, ...]
    areas: tuple[Area, ...]


def timestamp(value) -> datetime:
    stamp = datetime.fromisoformat(str(value))
    if stamp.utcoffset() is None:
        raise ValueError("Timezone required")
    return stamp


def text(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Nonempty text required")
    return value


def identifier(value) -> str:
    if not _ID.fullmatch(text(value)):
        raise ValueError("Invalid id")
    return value


def strings(value) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("List required")
    return tuple(text(entry) for entry in value)


def area_from(value: dict) -> Area:
    domains = tuple(domain.lower().rstrip(".") for domain in strings(value["official_domains"]))
    if not all(_DOMAIN.fullmatch(domain) for domain in domains):
        raise ValueError("Invalid domain")
    if type(value["remarks"]) is not bool:
        raise ValueError("Boolean required")
    return Area(identifier(value["id"]), text(value["question"]), value["remarks"], domains)


def topic_from(value: dict) -> Topic:
    areas = tuple(area_from(area) for area in value["areas"])
    times = strings(value["check_times"])
    if not times or not all(re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", t) for t in times):
        raise ValueError("Invalid check times")
    if not areas or len({area.id for area in areas}) != len(areas):
        raise ValueError("Duplicate or missing areas")
    days = value["lookback_days"]
    if type(days) is not int or days < 0:
        raise ValueError("Invalid lookback")
    return Topic(
        identifier(value["id"]),
        text(value["title"]),
        ZoneInfo(text(value["timezone"])),
        timestamp(value["active_until"]),
        tuple(sorted({time.fromisoformat(t) for t in times})),
        days,
        text(value["context"]),
        strings(value["relevance"]),
        areas,
    )


def load_topics(directory: Path | None = None) -> list[Topic]:
    loaded: dict[str, Topic] = {}
    duplicates: set[str] = set()
    for path in sorted((directory or topics_dir()).glob("*.yaml")):
        try:
            topic = topic_from(yaml.safe_load(path.read_text()))
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            yaml.YAMLError,
            ZoneInfoNotFoundError,
        ):
            _LOG.warning("Skipping invalid topic file")
            continue
        if topic.id in loaded:
            duplicates.add(topic.id)
            _LOG.warning("Skipping duplicate topic id")
        loaded[topic.id] = topic
    return [topic for key, topic in loaded.items() if key not in duplicates]
