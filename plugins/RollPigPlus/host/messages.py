"""JianerCore-native message helpers used by the RollPigPlus modules.

``Message`` extends ``jianer.common.Message`` with the ``+``/``extract_plain_text``
ergonomics the ported modules use, and ``MessageSegment`` builds the reply,
text, image and At segments they need.  Everything ends up as plain JianerCore
segments, so ``actions.send`` serialises it directly.
"""

from __future__ import annotations

import base64
from typing import Any, Iterable, Mapping

from jianer import common, segments


def _image_segment(file: Any, summary: str = "[Image]") -> Any:
    if isinstance(file, (bytes, bytearray, memoryview)):
        encoded = base64.b64encode(bytes(file)).decode("ascii")
        return segments.Image(file=f"base64://{encoded}", summary=summary)
    text = str(file)
    return segments.Image.build(text) or segments.Image(file=text, summary=summary)


def _segment_from_payload(payload: Mapping[str, Any]) -> Any | None:
    """Convert an OneBot-style segment dict into a JianerCore segment."""

    segment_type = str(payload.get("type") or "")
    data = payload.get("data")
    data = dict(data) if isinstance(data, Mapping) else {}
    if segment_type == "text":
        return segments.Text(str(data.get("text", "")))
    if segment_type == "image":
        file = data.get("file") or data.get("url")
        return None if file is None else _image_segment(file)
    if segment_type == "reply":
        message_id = data.get("id") or data.get("message_id")
        return segments.Reply(str(message_id)) if message_id is not None else None
    if segment_type == "at":
        user_id = data.get("qq") or data.get("user_id")
        return segments.At(str(user_id)) if user_id is not None else None
    if segment_type == "node":
        content = data.get("content")
        if content is None:
            return None
        user_id = data.get("user_id") or data.get("uin") or data.get("qq") or ""
        name = data.get("nickname") or data.get("name") or data.get("nick_name") or ""
        return segments.Node(str(user_id), str(name), Message(content))
    return None


class Message(common.Message):
    """``jianer.common.Message`` with list-like ``+`` handling."""

    def __init__(self, *items: Any) -> None:
        super().__init__()
        for item in items:
            self.append(item)

    def append(self, item: Any) -> None:
        if item is None:
            return
        if isinstance(item, common.Message):
            self.contents.extend(item.contents)
        elif isinstance(item, Mapping):
            segment = _segment_from_payload(item)
            if segment is not None:
                self.contents.append(segment)
        elif isinstance(item, segments.SegmentBase):
            self.contents.append(item)
        elif isinstance(item, str):
            self.contents.append(segments.Text(item))
        elif isinstance(item, Iterable):
            for entry in item:
                self.append(entry)
        else:
            self.contents.append(segments.Text(str(item)))

    def __add__(self, other: Any) -> "Message":
        result = Message(*self.contents)
        result.append(other)
        return result

    def __radd__(self, other: Any) -> "Message":
        result = Message(other)
        result.contents.extend(self.contents)
        return result

    def __iadd__(self, other: Any) -> "Message":
        self.append(other)
        return self

    def extract_plain_text(self) -> str:
        return "".join(
            str(getattr(item, "text", ""))
            for item in self.contents
            if isinstance(item, segments.Text)
        )


class MessageSegment:
    """Factories mirroring the segment helpers the ported modules call."""

    @staticmethod
    def text(value: Any) -> Message:
        return Message(segments.Text(str(value)))

    @staticmethod
    def image(file: Any, summary: str = "[Image]") -> Message:
        return Message(_image_segment(file, summary))

    @staticmethod
    def reply(message_id: Any) -> Message:
        if message_id is None:
            return Message()
        return Message(segments.Reply(str(message_id)))

    @staticmethod
    def at(user_id: Any) -> Message:
        return Message(segments.At(str(user_id)))


def reply_text(message_id: Any, text: str) -> Message:
    return MessageSegment.reply(message_id) + text


__all__ = ["Message", "MessageSegment", "reply_text"]
