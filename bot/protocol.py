"""协议判定与群消息文本标准化。"""
import re
from typing import Any


_FEISHU_LEADING_MENTION_RE = re.compile(r"^(?:@\S+\s*)+")
_FEISHU_PLACEHOLDER_MENTION_RE = re.compile(r"^(?:@_user_\d+\s*)+")


def is_qq_protocol(config) -> bool:
    return str(config.protocol).lower() in {"onebot", "milky"}


def is_feishu_protocol(config) -> bool:
    return str(config.protocol).lower() == "feishu"


def normalize_group_message_text(config, text: str) -> str:
    msg = str(text or "").strip()
    if str(config.protocol).lower() == "feishu":
        msg = _FEISHU_LEADING_MENTION_RE.sub("", msg).strip()
        msg = msg.replace("\u200b", "").replace("\ufeff", "").strip()
    return msg


def restore_feishu_mention_flag(config, event: Any, text: str) -> bool:
    """Restore mentions represented as Lark ``@_user_N`` text placeholders."""

    if not is_feishu_protocol(config):
        return False
    mentioned = bool(getattr(event, "is_mentioned", False))
    if not mentioned and _FEISHU_PLACEHOLDER_MENTION_RE.match(
        str(text or "").strip()
    ):
        event.is_mentioned = True
        mentioned = True
    return mentioned


def plugin_message_text(
    config,
    message: Any,
    fallback: str,
    *,
    text_segment_type: type,
) -> str:
    """Project QQ messages to plain text without consuming their At segments."""

    if not is_qq_protocol(config):
        return str(fallback or "").strip()
    return "".join(
        str(getattr(segment, "text", "") or "")
        for segment in (message or ())
        if isinstance(segment, text_segment_type)
    ).strip()
