"""Planner stage: decide whether the bot should speak up on its own."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Sequence

from plugins.JianerMate.config import RuntimeOptions
from plugins.JianerMate.persona import Persona
from plugins.JianerMate.providers import OpenAICompatibleClient
from plugins.JianerMate.store import MessageRecord


BEHAVIOR_RULES = (
    "你正在扮演一个真实群友，而不是客服或助手。"
    "判断标准：只有当你确实有想说的内容、能接住正在聊的话题、"
    "或者气氛适合接话时才开口；如果话题已经结束、别人只是在互相道别、"
    "刚有人被 @ 但没有你、或者上一句就是你自己说的，就保持沉默。"
    "宁可少说话，也不要为了发言而发言。"
)

PLANNER_SCHEMA = (
    "只输出一个 JSON 对象，不要输出其它文字，字段如下：\n"
    '{"speak": true 或 false, "reason": "一句话说明理由", '
    '"topic": "你想接的话题", "target_message_id": "要接的消息 id 或空字符串", '
    '"want_meme": true 或 false}\n'
    "want_meme 表示这次发言是否适合配一张表情包。"
)


@dataclass(frozen=True, slots=True)
class PlannerDecision:
    speak: bool
    reason: str = ""
    topic: str = ""
    target_message_id: str = ""
    want_meme: bool = False
    style: str = ""
    raw: str = ""

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        raw: str = "",
        valid_message_ids: Sequence[str] = (),
    ) -> "PlannerDecision":
        speak = _as_bool(payload.get("speak"), False)
        target = str(payload.get("target_message_id") or "").strip()
        if target and valid_message_ids and target not in set(valid_message_ids):
            target = ""
        return cls(
            speak=speak,
            reason=str(payload.get("reason") or "")[:200],
            topic=str(payload.get("topic") or "")[:200],
            target_message_id=target,
            want_meme=_as_bool(payload.get("want_meme"), False),
            raw=raw[:2000],
        )

    @classmethod
    def silence(cls, reason: str, *, raw: str = "") -> "PlannerDecision":
        return cls(speak=False, reason=reason, raw=raw)

    def as_json(self) -> str:
        return json.dumps(
            {
                "speak": self.speak,
                "reason": self.reason,
                "topic": self.topic,
                "target_message_id": self.target_message_id,
                "want_meme": self.want_meme,
                "style": self.style,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


class Planner:
    """Single text-model call that gates proactive speech."""

    def __init__(
        self,
        client: OpenAICompatibleClient,
        *,
        options: RuntimeOptions,
        logger: Any | None = None,
    ) -> None:
        self.client = client
        self.options = options
        self._logger = logger

    async def decide(
        self,
        *,
        model_key: str,
        conversation_label: str,
        recent: Sequence[MessageRecord],
        talk_value: float,
        custom_prompt: str = "",
        persona: Persona | None = None,
        style_override: str = "",
        now: float | None = None,
    ) -> PlannerDecision:
        system_prompt, user_prompt = self.build_prompt(
            conversation_label=conversation_label,
            recent=recent,
            talk_value=talk_value,
            custom_prompt=custom_prompt,
            persona=persona,
            style_override=style_override,
            now=now,
        )
        payload = await self.client.complete_json(
            model_key,
            system_prompt=system_prompt,
            user_text=user_prompt,
            max_tokens=400,
            temperature=0.4,
        )
        valid_ids = [
            record.platform_message_id
            for record in recent
            if record.platform_message_id
        ]
        return PlannerDecision.from_payload(
            payload,
            raw=json.dumps(payload, ensure_ascii=False),
            valid_message_ids=valid_ids,
        )

    def build_prompt(
        self,
        *,
        conversation_label: str,
        recent: Sequence[MessageRecord],
        talk_value: float,
        custom_prompt: str = "",
        persona: Persona | None = None,
        style_override: str = "",
        now: float | None = None,
    ) -> tuple[str, str]:
        timestamp = time.localtime(now if now is not None else time.time())
        active_persona = persona or self.options.persona
        system_prompt = "\n\n".join(
            part
            for part in (
                active_persona.render("group"),
                BEHAVIOR_RULES,
                style_instruction(style_override),
                PLANNER_SCHEMA,
            )
            if part
        )
        lines = [
            f"当前群/会话：{conversation_label}",
            "当前时间："
            f"{timestamp.tm_year}-{timestamp.tm_mon:02d}-{timestamp.tm_mday:02d} "
            f"{timestamp.tm_hour:02d}:{timestamp.tm_min:02d}",
            f"发言倾向 talk_value（0 到 1，越大越想说话）：{talk_value:.2f}",
        ]
        if custom_prompt.strip():
            lines.append(f"本群补充要求：{custom_prompt.strip()[:400]}")
        lines.append("最近的聊天记录（从旧到新）：")
        lines.append(format_transcript(recent) or "（暂无）")
        lines.append("")
        lines.append("请决定这次要不要主动说话，并按 JSON 格式回答。")
        return system_prompt, "\n".join(lines)


def style_instruction(style_override: str) -> str:
    style = str(style_override or "").strip()
    if not style:
        return ""
    return f"本次回复临时改用这种说话风格（只影响这一次）：{style}"


def format_transcript(messages: Sequence[MessageRecord]) -> str:
    lines: list[str] = []
    for record in messages:
        lines.append(format_message(record))
    return "\n".join(line for line in lines if line)


def format_message(record: MessageRecord) -> str:
    content = str(record.content or "").strip()
    if not content:
        return ""
    if record.direction == "outgoing":
        speaker = "自己"
    else:
        speaker = record.sender_name or record.sender_id or "群友"
    message_id = record.platform_message_id or f"local-{record.id}"
    return f"[{message_id}] {speaker}: {content}"


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().casefold()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default
