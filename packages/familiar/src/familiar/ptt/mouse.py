"""macOS mouse push-to-talk through an active session event tap."""

from typing import Any

from familiar.interfaces import PluginError

from .macos_tap import EventTapPTT, native_modules


def mouse_modules() -> tuple[Any, Any]:
    """Check native APIs and Accessibility permission."""
    return native_modules("mouse")


class MousePTT(EventTapPTT):
    name = "mouse"

    def __init__(self, settings: dict) -> None:
        self.button = settings.get("button")
        if type(self.button) is not int or self.button < 2:
            raise PluginError("mouse PTT requires a button integer >= 2")
        self.consume = settings.get("consume", True)
        if not isinstance(self.consume, bool):
            raise PluginError("mouse PTT consume must be a boolean")
        super().__init__(settings)

    def event_types(self) -> tuple[int, ...]:
        q = self._quartz
        return q.kCGEventOtherMouseDown, q.kCGEventOtherMouseUp, q.kCGEventOtherMouseDragged

    def classify(self, kind: int, event: Any) -> tuple[bool | None, bool]:
        q = self._quartz
        if q.CGEventGetIntegerValueField(event, q.kCGMouseEventButtonNumber) != self.button:
            return None, False
        pressed = {q.kCGEventOtherMouseDown: True, q.kCGEventOtherMouseUp: False}.get(kind)
        return pressed, self.consume
