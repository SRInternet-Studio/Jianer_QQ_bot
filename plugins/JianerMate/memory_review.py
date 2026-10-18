"""Automatic long-term memory review after a successful reply.

Mirrors JianerAI's post-reply reviewer: a small model reads the recent
exchange, proposes at most a few memory actions, and JianerMate applies them
through the memorix-backed scoped memory API. Jobs are keyed by exchange and
persisted, so a crash or an offline model does not lose work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from plugins.JianerMate.providers import OpenAICompatibleClient


_LOGGER = logging.getLogger("jianer_mate.memory_review")

REVIEW_RULES = (
    "你是一个长期记忆整理器。阅读最近的一段群聊或私聊，判断其中是否有值得"
    "跨会话保留的信息，并输出记忆动作。要求：\n"
    "1. 只输出 JSON：{\"memories\": [ ... ]}，没有值得记的就输出 {\"memories\": []}。\n"
    "2. 每条记忆字段：action（create 或 update）、scope（person 或 group）、"
    "memory_id（update 时必填）、canonical_fact（中性事实摘要，用于去重）、"
    "memory_text（用当前人设的第一人称语气写成的一句完整记忆）、"
    "importance（0-1）、confidence（0-1）。\n"
    "3. 只记录稳定的偏好、身份、关系、约定与长期事件；不要记录一时情绪、"
    "闲聊寒暄、玩笑或可从上下文推断的临时信息。\n"
    "4. person 只针对该发言人本人；group 只针对当前群整体。最多输出 3 条。\n"
    "5. 已经在“已知记忆”里的内容不要重复创建，确有补充时用 update + memory_id。"
)


@dataclass(frozen=True, slots=True)
class MemoryAction:
    action: str
    scope: str
    canonical_fact: str
    memory_text: str
    memory_id: str = ""
    importance: float = 0.7
    confidence: float = 0.9

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "MemoryAction | None":
        if not isinstance(payload, Mapping):
            return None
        action = str(payload.get("action") or "create").strip().casefold()
        if action not in {"create", "update"}:
            return None
        scope = str(payload.get("scope") or "person").strip().casefold()
        if scope not in {"person", "group"}:
            return None
        text = str(
            payload.get("memory_text") or payload.get("content") or ""
        ).strip()[:1000]
        fact = str(payload.get("canonical_fact") or text).strip()[:1000]
        if not text or not fact:
            return None
        memory_id = str(payload.get("memory_id") or "").strip()
        if action == "update" and not memory_id:
            return None
        return cls(
            action=action,
            scope=scope,
            canonical_fact=fact,
            memory_text=text,
            memory_id=memory_id,
            importance=_clamp(payload.get("importance"), 0.7),
            confidence=_clamp(payload.get("confidence"), 0.9),
        )


class MemoryReviewer:
    """Ask a small model which memories an exchange deserves."""

    def __init__(
        self,
        client: OpenAICompatibleClient,
        *,
        logger: Any | None = None,
    ) -> None:
        self.client = client
        self._logger = logger or _LOGGER

    async def review(
        self,
        *,
        model_key: str,
        transcript: str,
        persona_text: str = "",
        known_memories: Sequence[Mapping[str, Any]] = (),
        max_actions: int = 3,
    ) -> tuple[MemoryAction, ...]:
        body = str(transcript or "").strip()
        if not body:
            return ()
        known_lines = [
            f"- id={item.get('id')} scope={item.get('scope')} "
            f"fact={item.get('canonical_fact') or item.get('content')}"
            for item in known_memories
        ]
        user_prompt = "\n".join(
            part
            for part in (
                f"当前人设：{persona_text.strip()[:400]}" if persona_text else "",
                "已知记忆：\n" + "\n".join(known_lines) if known_lines else "已知记忆：（空）",
                "最近的对话：",
                body[:4000],
                f"请最多输出 {max(1, int(max_actions))} 条记忆动作。",
            )
            if part
        )
        payload = await self.client.complete_json(
            model_key,
            system_prompt=REVIEW_RULES,
            user_text=user_prompt,
            max_tokens=700,
            temperature=0.2,
        )
        raw_items = payload.get("memories")
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
            return ()
        actions: list[MemoryAction] = []
        for item in list(raw_items)[: max(1, int(max_actions))]:
            action = MemoryAction.from_payload(item)
            if action is not None:
                actions.append(action)
        return tuple(actions)


def _clamp(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    return max(0.0, min(1.0, parsed))
