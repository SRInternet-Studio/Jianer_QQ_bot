"""JianerAI reply tool backed by the dedicated Replyer model."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from plugins.JianerAI.tools.contracts import (
    ToolContext,
    ToolExecutionError,
    ToolRisk,
    ToolSpec,
)


REPLY_TOOL_NAME = "reply"
REPLY_STYLES = ("简短表达", "正常回复", "长回复")


def reply_tool() -> ToolSpec:
    """Return the single natural-language chat output tool."""

    return ToolSpec(
        name=REPLY_TOOL_NAME,
        description=(
            "生成并发送一条普通聊天回复。reply_reference 只提供事实、上下文和"
            "表达方向，最终正文由独立 Replyer 模型生成。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "reply_reference": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 12000,
                    "description": (
                        "本次回复需要使用的关键事实、回复目标、关系、语气和表达方向；"
                        "不要填写最终可见正文。"
                    ),
                },
                "reply_style": {
                    "type": "string",
                    "enum": list(REPLY_STYLES),
                    "description": "可选的本轮回复篇幅要求。",
                },
                "reply_to_message_id": {
                    "type": "string",
                    "description": "可选的引用目标消息 ID；留空时使用当前触发消息。",
                },
            },
            "required": ["reply_reference"],
            "additionalProperties": False,
        },
        handler=_reply,
        risk=ToolRisk.MUTATING,
        group="chat",
        sub_visible=False,
        timeout_seconds=240.0,
        max_output_chars=8000,
    )


async def _reply(
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    reply_reference = str(arguments.get("reply_reference") or "").strip()
    if not reply_reference:
        raise ToolExecutionError("invalid_reply_reference", "reply_reference 不能为空。")

    sink = getattr(context, "message_sink", None)
    service = getattr(sink, "service", None)
    replyer = getattr(service, "replyer", None)
    if replyer is None or not callable(getattr(replyer, "generate", None)):
        raise ToolExecutionError("replyer_unavailable", "JianerAI Replyer 当前不可用。")
    if sink is None or not callable(getattr(sink, "send", None)):
        raise ToolExecutionError("unsupported_protocol", "当前连接不支持发送消息。")

    reply_to_message_id = str(arguments.get("reply_to_message_id") or "").strip()
    if not reply_to_message_id:
        reply_to_message_id = _event_message_id(context.event)
    reply_style = str(arguments.get("reply_style") or "").strip()
    user_request = _current_user_text(context.history)
    try:
        generated = await replyer.generate(
            context,
            reply_reference=reply_reference,
            reply_style=reply_style,
            reply_to_message_id=reply_to_message_id,
        )
    except ToolExecutionError:
        raise
    except Exception as exc:
        raise ToolExecutionError(
            "replyer_failed",
            f"Replyer 生成失败：{type(exc).__name__}。",
        ) from exc

    text = str(getattr(generated, "text", "") or "").strip()
    if not text:
        raise ToolExecutionError("empty_reply", "Replyer 返回了空回复。")
    try:
        message_ids = await sink.send(
            text,
            reply_to_message_id=reply_to_message_id,
            source="replyer",
            user_request=user_request,
        )
    except Exception as exc:
        raise ToolExecutionError(
            "send_failed",
            f"回复发送失败：{type(exc).__name__}。",
        ) from exc

    suppressed = bool(getattr(sink, "last_send_suppressed", False))
    return {
        "sent": True,
        "suppressed": suppressed,
        "reply_text": "" if suppressed else text,
        "target_message_id": reply_to_message_id,
        "message_ids": [
            str(item)
            for item in (
                message_ids
                if isinstance(message_ids, Sequence)
                and not isinstance(message_ids, (str, bytes, bytearray))
                else ()
            )
            if str(item).strip()
        ],
        "model": str(getattr(generated, "model", "") or ""),
        "cache_prefix_hash": str(
            getattr(generated, "cache_prefix_hash", "") or ""
        ),
        "prompt_tokens": int(getattr(generated, "prompt_tokens", 0) or 0),
        "completion_tokens": int(
            getattr(generated, "completion_tokens", 0) or 0
        ),
        "cached_prompt_tokens": int(
            getattr(generated, "cached_prompt_tokens", 0) or 0
        ),
    }


def _event_message_id(event: Any) -> str:
    for name in ("message_id", "msg_id", "id"):
        value = str(getattr(event, name, "") or "").strip()
        if value:
            return value
    return ""


def _current_user_text(history: Sequence[Mapping[str, str]]) -> str:
    texts: list[str] = []
    for item in history:
        if not isinstance(item, Mapping):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        body = content.rsplit("\n", 1)[-1].strip()
        if body:
            texts.append(body)
    return "\n".join(texts)
