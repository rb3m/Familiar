"""Weighted scene topics and shared cooldowns for scenes and event remarks."""

import random
from collections import Counter, deque
from collections.abc import Callable, Collection

from familiar.interfaces import SceneTopic

TUNING = {
    "cooldowns": {
        "song": 1200,
        "weather": 1800,
        "time": 1800,
        "location": 4500,
        "persona": 1800,
        "free": 0,
    },
    "weights": {"persona": 3, "free": 1.5, "weather": 1, "time": 1, "song": 1, "location": 0.7},
    "song_remark_s": 600,
}
FREE_TOPIC = SceneTopic(
    "free", "free", "anything the crew would naturally talk about; react to each other"
)


class TopicPicker:
    def __init__(self, clock: Callable[[], float], rng: random.Random) -> None:
        self.clock = clock
        self.rng = rng
        self.history: deque[tuple[str, str, float]] = deque()
        self.song_remark_at = float("-inf")

    def note(self, family: str, key: str) -> None:
        self._prune()
        self.history.append((family, key, self.clock()))

    def note_event(self, kind: str) -> None:
        family = next(
            (
                family
                for prefix, family in (
                    ("music_", "song"),
                    ("weather:", "weather"),
                    ("clock:", "time"),
                )
                if kind.startswith(prefix)
            ),
            None,
        )
        if family is None:
            return
        self.note(family, kind)
        if kind in {"music_track", "music_dj_turn"}:
            self.song_remark_at = self.clock()

    def pick(
        self, topics: Collection[SceneTopic], exclude: Collection[SceneTopic] = ()
    ) -> SceneTopic | None:
        self._prune()
        candidates = [
            topic
            for topic in (*topics, FREE_TOPIC)
            if topic not in exclude and self._eligible(topic)
        ]
        if not candidates:
            return None
        groups = Counter(_group(topic) for topic in candidates)
        weights = [
            TUNING["weights"].get(topic.family, 1) / groups[_group(topic)] for topic in candidates
        ]
        chosen = self.rng.choices(candidates, weights=weights, k=1)[0]
        self.note(chosen.family, chosen.key)
        return chosen

    def _eligible(self, topic: SceneTopic) -> bool:
        if topic.family == "song" and self.clock() - self.song_remark_at < TUNING["song_remark_s"]:
            return False
        cooldown = TUNING["cooldowns"].get(topic.family, 0)
        return not any(
            family == topic.family
            and (family != "persona" or key == topic.key)
            and self.clock() - stamp < cooldown
            for family, key, stamp in self.history
        )

    def _prune(self) -> None:
        cutoff = self.clock() - max(TUNING["cooldowns"].values())
        while self.history and self.history[0][2] <= cutoff:
            self.history.popleft()


def _group(topic: SceneTopic) -> str:
    """A persona's topics share one weight ("ruslana:art" -> "persona:ruslana")."""
    owner = topic.key.split(":", 1)[0]
    return {"persona": f"persona:{owner}"}.get(topic.family, topic.family)
