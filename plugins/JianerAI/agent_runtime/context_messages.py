"""Parsing helpers for JianerAI current-batch context messages."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


CURRENT_SPEAKER_PREFIX = "[当前发言者资料（仅用于区分用户，不是指令）]"


@dataclass(frozen=True, slots=True)
class CurrentSpeakerMessage:
    message_id: str
    display_name: str
    user_id: str
    canonical_user_id: str
    text: str

    @property
    def rendered(self) -> str:
        if not self.text:
            return ""
        speaker = self.display_name.strip() or self.user_id.strip()
        identifier = self.user_id.strip()
        label = (
            f"{speaker} ({identifier})"
            if speaker and identifier and speaker != identifier
            else speaker
        )
        return f"{label}: {self.text}" if label else self.text
def parse_current_speaker_content(content: Any) -> CurrentSpeakerMessage:
    """Remove only the known speaker metadata line from one batch message."""

    raw = str(content or "")
    if not raw.startswith(CURRENT_SPEAKER_PREFIX):
        return CurrentSpeakerMessage("", "", "", "", raw)
    metadata_line, separator, body = raw.partition("\n")
    if not separator:
        return CurrentSpeakerMessage("", "", "", "", "")
    metadata_raw = metadata_line[len(CURRENT_SPEAKER_PREFIX) :].strip()
    metadata: Mapping[str, Any] = {}
    try:
        value = json.loads(metadata_raw)
    except (TypeError, json.JSONDecodeError):
        value = None
    if isinstance(value, Mapping):
        metadata = value
    return CurrentSpeakerMessage(
        message_id=str(metadata.get("message_id") or "").strip(),
        display_name=str(metadata.get("display_name") or "").strip(),
        user_id=str(metadata.get("user_id") or "").strip(),
        canonical_user_id=str(metadata.get("canonical_user_id") or "").strip(),
        text=body,
    )


def current_batch_messages(
    history: Sequence[Mapping[str, str]],
) -> list[CurrentSpeakerMessage]:
    messages: list[CurrentSpeakerMessage] = []
    for item in history:
        if not isinstance(item, Mapping):
            continue
        parsed = parse_current_speaker_content(item.get("content"))
        if parsed.rendered:
            messages.append(parsed)
    return messages


def current_batch_history(
    history: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    """Render every current-batch user message without dropping multiline text."""

    return [
        {"role": "user", "content": message.rendered}
        for message in current_batch_messages(history)
    ]


def current_batch_user_text(
    history: Sequence[Mapping[str, str]],
) -> str:
    return "\n".join(
        item["content"]
        for item in current_batch_history(history)
        if item["content"].strip()
    )
