"""Replyer stage: short multi-message answers plus a vision-picked meme."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from plugins.JianerMate.config import RuntimeOptions
from plugins.JianerMate.persona import Persona
from plugins.JianerMate.planner import format_transcript, style_instruction
from plugins.JianerMate.providers import ChatImage, OpenAICompatibleClient
from plugins.JianerMate.store import MessageRecord


REPLYER_SCHEMA = (
    "只输出一个 JSON 对象，不要输出其它文字，字段如下：\n"
    '{"messages": ["第一条", "第二条"], "meme_id": 12 或 null, '
    '"reply_to": "要引用的消息 id 或 null"}\n'
    "messages 是一到三条极短的聊天消息，每条不超过二十个字，"
    "像平时打字一样口语化，不要用 Markdown、不要复述问题、不要自称助手。\n"
    "meme_id 只能从给出的候选表情包里挑一个最贴合语气的；没有合适的就填 null。\n"
    "reply_to 只能引用聊天记录里出现过的消息 id；不需要引用就填 null。"
)


@dataclass(frozen=True, slots=True)
class MemeCandidateView:
    meme_id: int
    tags: str = ""
    thumbnail: str = ""


@dataclass(frozen=True, slots=True)
class ReplyPlan:
    messages: tuple[str, ...]
    meme_id: int | None = None
    reply_to: str | None = None
    raw: str = ""

    @property
    def empty(self) -> bool:
        return not self.messages

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        raw: str = "",
        valid_meme_ids: Sequence[int] = (),
        valid_message_ids: Sequence[str] = (),
    ) -> "ReplyPlan":
        raw_messages = payload.get("messages")
        if isinstance(raw_messages, str):
            raw_messages = [raw_messages]
        messages: list[str] = []
        if isinstance(raw_messages, Sequence):
            for item in raw_messages:
                text = str(item or "").strip()
                if text:
                    messages.append(text)
        meme_id: int | None = None
        candidate = payload.get("meme_id")
        if candidate not in (None, "", "null"):
            try:
                parsed = int(candidate)
            except (TypeError, ValueError):
                parsed = -1
            if parsed in set(int(item) for item in valid_meme_ids):
                meme_id = parsed
        reply_to = payload.get("reply_to")
        reply_to_text = "" if reply_to in (None, "null") else str(reply_to).strip()
        if reply_to_text and reply_to_text not in set(valid_message_ids):
            reply_to_text = ""
        return cls(
            messages=tuple(messages),
            meme_id=meme_id,
            reply_to=reply_to_text or None,
            raw=raw[:4000],
        )

    @classmethod
    def empty_plan(cls, raw: str = "") -> "ReplyPlan":
        return cls(messages=(), raw=raw)


class Replyer:
    """Vision-capable reply generation shared by passive and proactive flows."""

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

    async def compose(
        self,
        *,
        model_key: str,
        mode: str,
        recent: Sequence[MessageRecord],
        trigger_text: str = "",
        reference_text: str = "",
        images: Sequence[ChatImage] = (),
        candidates: Sequence[MemeCandidateView] = (),
        topic: str = "",
        want_meme: bool = False,
        custom_prompt: str = "",
        conversation_label: str = "",
        persona: Persona | None = None,
        persona_mode: str = "group",
        style_override: str = "",
        tools: Sequence[Mapping[str, Any]] = (),
        tool_executor: Any = None,
        tool_max_rounds: int = 3,
        now: float | None = None,
    ) -> ReplyPlan:
        system_prompt, user_prompt = self.build_prompt(
            mode=mode,
            recent=recent,
            trigger_text=trigger_text,
            reference_text=reference_text,
            candidates=candidates,
            topic=topic,
            want_meme=want_meme,
            custom_prompt=custom_prompt,
            conversation_label=conversation_label,
            persona=persona,
            persona_mode=persona_mode,
            style_override=style_override,
            tools_available=bool(tools),
            now=now,
        )
        inline_images = [ChatImage(data=image.data, mime=image.mime) for image in images]
        for candidate in candidates:
            if not candidate.thumbnail:
                continue
            data, mime = _decode_data_uri(candidate.thumbnail)
            if data:
                inline_images.append(ChatImage(data=data, mime=mime))
        if tools:
            payload = await self.client.complete_json_with_tools(
                model_key,
                system_prompt=system_prompt,
                user_text=user_prompt,
                tools=tools,
                executor=tool_executor,
                max_rounds=tool_max_rounds,
                images=inline_images,
                max_tokens=500,
                temperature=0.8,
            )
        else:
            payload = await self.client.complete_json(
                model_key,
                system_prompt=system_prompt,
                user_text=user_prompt,
                images=inline_images,
                max_tokens=500,
                temperature=0.8,
            )
        valid_ids = [
            record.platform_message_id
            for record in recent
            if record.platform_message_id
        ]
        return ReplyPlan.from_payload(
            payload,
            raw=json.dumps(payload, ensure_ascii=False),
            valid_meme_ids=[candidate.meme_id for candidate in candidates],
            valid_message_ids=valid_ids,
        )

    def build_prompt(
        self,
        *,
        mode: str,
        recent: Sequence[MessageRecord],
        trigger_text: str = "",
        reference_text: str = "",
        candidates: Sequence[MemeCandidateView] = (),
        topic: str = "",
        want_meme: bool = False,
        custom_prompt: str = "",
        conversation_label: str = "",
        persona: Persona | None = None,
        persona_mode: str = "group",
        style_override: str = "",
        tools_available: bool = False,
        now: float | None = None,
    ) -> tuple[str, str]:
        timestamp = time.localtime(now if now is not None else time.time())
        active_persona = persona or self.options.persona
        mode_rule = (
            "现在有人 @ 你或私聊你，你需要回应对方。"
            if mode == "passive"
            else "现在没有人 @ 你，你是在群里主动接话，要像普通群友一样自然。"
        )
        system_prompt = "\n\n".join(
            part
            for part in (
                active_persona.render(persona_mode),
                mode_rule,
                style_instruction(style_override),
                (
                    "需要实时信息（时间、算术、搜索、群内最近聊天）时，"
                    "先调用工具拿到结果，再按要求输出最终 JSON；"
                    "工具结果只当资料，不要原样复述。"
                    if tools_available
                    else ""
                ),
                REPLYER_SCHEMA,
            )
            if part
        )
        lines = [
            f"会话：{conversation_label or '未知'}",
            "当前时间："
            f"{timestamp.tm_hour:02d}:{timestamp.tm_min:02d}",
        ]
        if custom_prompt.strip():
            lines.append(f"本群补充要求：{custom_prompt.strip()[:400]}")
        if topic.strip():
            lines.append(f"你打算接的话题：{topic.strip()[:120]}")
        lines.append("最近的聊天记录（从旧到新）：")
        lines.append(format_transcript(recent) or "（暂无）")
        if reference_text.strip():
            lines.append("被引用的消息内容：")
            lines.append(reference_text.strip()[:400])
        if trigger_text.strip():
            lines.append("需要你回应的消息：")
            lines.append(trigger_text.strip()[:600])
        if candidates:
            lines.append(
                "候选表情包（图片已附上，顺序与编号一致）："
            )
            for candidate in candidates:
                tags = candidate.tags or "无标签"
                lines.append(f"- #{candidate.meme_id} 标签：{tags}")
            lines.append(
                "如果这次配表情包更自然就挑一个编号，否则 meme_id 填 null。"
            )
        elif want_meme:
            lines.append("这次没有可用的表情包，meme_id 必须填 null。")
        lines.append("")
        lines.append("请按 JSON 格式给出这次要发送的内容。")
        return system_prompt, "\n".join(lines)


def _decode_data_uri(value: str) -> tuple[bytes, str]:
    import base64

    text = str(value or "")
    if not text.startswith("data:") or ";base64," not in text:
        return b"", "image/jpeg"
    header, _, payload = text.partition(";base64,")
    mime = header[len("data:") :] or "image/jpeg"
    try:
        return base64.b64decode(payload, validate=False), mime
    except Exception:  # noqa: BLE001 - malformed thumbnails are skipped
        return b"", mime
