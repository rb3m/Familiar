"""Session-only action gate: bounded execution and explicit later-turn consent."""

import asyncio
import json
import re
import time
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from uuid import uuid4

from familiar.interfaces import ACTION_STATUSES, ActionSpec, Actuator, ToolSpec
from familiar.tools import ToolRegistry

AFFIRMATIVES = {
    "en": [
        "yes",
        "yeah",
        "yep",
        "yup",
        "sure",
        "confirm",
        "confirmed",
        "affirmative",
        "do it",
        "go ahead",
        "go for it",
        "ok",
        "okay",
    ],
    "es": ["sí", "si", "dale", "hazlo", "confirmo", "claro", "adelante", "de acuerdo", "vale"],
    "ja": ["はい", "うん", "お願い", "どうぞ", "やって", "了解", "オッケー"],
}
NEGATIVES = {
    "en": ["no", "nope", "don't", "do not", "cancel", "stop", "abort", "wait", "belay"],
    "es": ["no", "cancela", "cancelar", "espera", "alto", "detente"],
    "ja": ["いいえ", "いや", "だめ", "ダメ", "やめ", "待って", "キャンセル"],
}


# Risky tools are still called at once: the result asks the player, so the model must not.
RISK_NOTES = {
    (False, False): "Runs immediately",
    (True, False): "Call it right away; the result asks the player to confirm",
    (False, True): "Call it right away; for risky arguments the result asks the player to confirm",
    (True, True): "Call it right away; for risky arguments the result asks the player to confirm",
}


@dataclass(frozen=True)
class _Pending:
    id: str
    name: str
    arguments: dict
    turn: int
    timestamp: float
    context: str | None


class ActionKeeper:
    def __init__(self, actuator: Actuator, settings: dict, language: str) -> None:
        self.actuator = actuator
        self.language = language
        self.confirm_ttl_s = settings.get("confirm_ttl_s", 30)
        self.action_timeout_s = settings.get("action_timeout_s", 5)
        self.affirmatives = self._words(AFFIRMATIVES, settings.get("affirmatives", {}))
        self.negatives = self._words(NEGATIVES, settings.get("negatives", {}))
        self.pending: _Pending | None = None
        self.turn = 0
        self.utterance = ""
        self.history: deque[dict] = deque(maxlen=32)
        self._results: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _words(defaults: dict, extras: dict) -> dict:
        return {
            lang: defaults.get(lang, []) + extras.get(lang, [])
            for lang in defaults.keys() | extras.keys()
        }

    def _matches(self, words: dict, ignore: str = "") -> bool:
        text = self.utterance.casefold()
        for lang, entries in words.items():
            for word in entries:
                word = word.casefold()
                if not word or word == ignore.casefold():
                    continue
                if lang == "ja" and word in text:
                    return True
                if lang != "ja" and re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text):
                    return True
        return False

    def _record(self, decision: str, **details) -> dict:
        self.history.append({"turn": self.turn, "decision": decision, **deepcopy(details)})
        return details

    def begin_turn(self, user_text: str) -> None:
        self.turn += 1
        self.utterance = user_text
        self._results = {}
        if self.pending and self._matches(self.negatives, self.pending.name):
            pending, self.pending = self.pending, None
            self._record("negative_cancelled", confirmation_id=pending.id, name=pending.name)

    def _context(self) -> str | None:
        method = getattr(self.actuator, "action_context", None)
        return method() if method is not None else None

    def register(self, registry: ToolRegistry) -> None:
        for name, spec in self.actuator.actions().items():
            risk = RISK_NOTES[(spec.risky, spec.risky_when is not None)]
            registry.register(
                ToolSpec("act_" + name, f"{spec.description}. {risk}.", spec.parameters),
                partial(self.request, name, spec),
            )
        registry.register(
            ToolSpec(
                "confirm_action",
                "Confirm a pending action after the player says yes in a later message.",
                {
                    "type": "object",
                    "properties": {"confirmation_id": {"type": "string"}},
                    "required": ["confirmation_id"],
                    "additionalProperties": False,
                },
            ),
            self.confirm_action,
        )
        registry.register(
            ToolSpec(
                "cancel_action",
                "Cancel the pending action.",
                {"type": "object", "properties": {}, "additionalProperties": False},
            ),
            self.cancel_action,
        )

    async def request(self, name: str, spec: ActionSpec, /, **arguments) -> dict:
        async with self._lock:
            key = json.dumps([name, arguments], sort_keys=True, allow_nan=False)
            if key in self._results:
                result = deepcopy(self._results[key])
                self._record("deduplicated", name=name, arguments=arguments, result=result)
                return result
            frozen = deepcopy(arguments)
            risky = spec.risky_when(frozen) if spec.risky_when is not None else spec.risky
            if risky:
                pending = _Pending(
                    uuid4().hex[:12],
                    name,
                    deepcopy(arguments),
                    self.turn,
                    time.monotonic(),
                    self._context(),
                )
                if self.pending is not None:
                    self._record(
                        "replaced", name=self.pending.name, confirmation_id=self.pending.id
                    )
                self.pending = pending
                summary = " ".join(spec.description.split())
                args = (
                    json.dumps(arguments, ensure_ascii=False, sort_keys=True) if arguments else ""
                )
                result = self._record(
                    "pending",
                    status="needs_confirmation",
                    confirmation_id=pending.id,
                    ask=f"{summary} {args}".strip(),
                )
            else:
                result = await self._perform(name, frozen)
            self._results[key] = deepcopy(result)
            return result

    async def _perform(self, name: str, arguments: dict) -> dict:
        # Cache an uncertain outcome before awaiting so cancellation cannot permit a retry.
        key = json.dumps([name, arguments], sort_keys=True, allow_nan=False)
        self._results[key] = {
            "status": "unknown",
            "detail": "execution interrupted; outcome unknown, not retried",
        }
        try:
            result = await asyncio.wait_for(
                self.actuator.perform(name, deepcopy(arguments)), self.action_timeout_s
            )
            if (
                not isinstance(result, dict)
                or result.get("status") not in ACTION_STATUSES
                or not isinstance(result.get("detail"), str)
            ):
                raise ValueError("Action result must contain a valid status and detail")
        except asyncio.CancelledError:
            self._record("interrupted", name=name, arguments=arguments, **self._results[key])
            raise
        except TimeoutError:
            result = {"status": "unknown", "detail": "timed out; outcome unknown, not retried"}
        except Exception as exc:
            result = {"status": "error", "detail": str(exc) or type(exc).__name__}
        self._results[key] = deepcopy(result)
        self._record("performed", name=name, arguments=arguments, result=result)
        return result

    async def confirm_action(self, confirmation_id: str) -> dict:
        async with self._lock:
            pending = self.pending
            reason = None
            if pending is None or pending.id != confirmation_id:
                reason = "No pending action with that confirmation ID"
            elif self.turn <= pending.turn:
                reason = "Confirmation requires a later player turn"
            elif time.monotonic() - pending.timestamp > self.confirm_ttl_s:
                reason = "Confirmation expired"
            elif self._matches(self.negatives, pending.name) or not self._matches(
                self.affirmatives
            ):
                reason = "The player must say an affirmative with no negative words"
            elif self._context() != pending.context:
                reason = "Action context changed"
            if reason is not None:
                return self._record("refused", status="error", detail=reason)
            self.pending = None
            return await self._perform(pending.name, pending.arguments)

    async def cancel_action(self) -> dict:
        self.pending = None
        return self._record("cancelled", status="done", detail="Pending action cancelled")
