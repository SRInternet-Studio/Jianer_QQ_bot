"""Runtime trigger prefix helpers for MaimaiDX commands."""

from bot import plugin_state

from typing import Any


class _MessageBodyEvent:
    """Expose stripped command text while forwarding the adapter event."""

    __slots__ = ("_event", "msg_str")

    def __init__(self, event: Any, message_text: str) -> None:
        self._event = event
        self.msg_str = message_text

    def __getattr__(self, name: str) -> Any:
        return getattr(self._event, name)


def _reminder() -> str:
    return str(plugin_state.get_runtime().get("reminder", "") or "")


def reminder() -> str:
    return _reminder()


def command_pattern(command: str, *, value: str | None = None) -> str:
    return f"{_reminder() if value is None else value}{command}"


def is_triggered(event: Any) -> bool:
    prefix = _reminder()
    if not prefix:
        return True
    return str(getattr(event, "msg_str", "") or "").startswith(prefix)


def body_event(event: Any) -> Any:
    prefix = _reminder()
    text = str(getattr(event, "msg_str", "") or "")
    if not prefix or not text.startswith(prefix):
        return event
    return _MessageBodyEvent(event, text[len(prefix) :])


def command_body(text: Any, command: str, *, value: str | None = None) -> str | None:
    raw = str(text or "").strip()
    prefix = _reminder() if value is None else value
    if prefix and not raw.startswith(prefix):
        return None
    body = raw[len(prefix) :]
    return body[len(command) :].strip() if body[: len(command)].lower() == command.lower() else None
