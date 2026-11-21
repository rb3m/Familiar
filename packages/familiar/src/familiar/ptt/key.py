"""macOS function-key and keypad push-to-talk."""

from typing import Any

from familiar.interfaces import PluginError

from .macos_tap import EventTapPTT

# Apple's Events.h kVK_F1 ... kVK_F20 and kVK_ANSI_Keypad* virtual keycodes.
ALLOWED_KEYS = {
    122: "F1",
    120: "F2",
    99: "F3",
    118: "F4",
    96: "F5",
    97: "F6",
    98: "F7",
    100: "F8",
    101: "F9",
    109: "F10",
    103: "F11",
    111: "F12",
    105: "F13",
    107: "F14",
    113: "F15",
    106: "F16",
    64: "F17",
    79: "F18",
    80: "F19",
    90: "F20",
    82: "Keypad 0",
    83: "Keypad 1",
    84: "Keypad 2",
    85: "Keypad 3",
    86: "Keypad 4",
    87: "Keypad 5",
    88: "Keypad 6",
    89: "Keypad 7",
    91: "Keypad 8",
    92: "Keypad 9",
    65: "Keypad Decimal",
    76: "Keypad Enter",
    69: "Keypad Plus",
    78: "Keypad Minus",
    67: "Keypad Multiply",
    75: "Keypad Divide",
    81: "Keypad Equals",
    71: "Keypad Clear",
}
KEY_ERROR = (
    "Only function keys F1-F20 and keypad keys can be push-to-talk; "
    "remap a spare key, e.g. a keyboard knob, to F13"
)


class KeyPTT(EventTapPTT):
    name = "key"

    def __init__(self, settings: dict) -> None:
        self.key = settings.get("key")
        if type(self.key) is not int or self.key not in ALLOWED_KEYS:
            raise PluginError(KEY_ERROR)
        self.consume = settings.get("consume", True)
        if not isinstance(self.consume, bool):
            raise PluginError("key PTT consume must be a boolean")
        super().__init__(settings)

    def event_types(self) -> tuple[int, ...]:
        q = self._quartz
        return q.kCGEventKeyDown, q.kCGEventKeyUp

    def classify(self, kind: int, event: Any) -> tuple[bool | None, bool]:
        q = self._quartz
        if q.CGEventGetIntegerValueField(event, q.kCGKeyboardEventKeycode) != self.key:
            return None, False
        if kind == q.kCGEventKeyUp:
            return False, self.consume
        if self._held or q.CGEventGetIntegerValueField(event, q.kCGKeyboardEventAutorepeat):
            return None, self.consume
        return True, self.consume
