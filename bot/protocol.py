"""协议判定与群消息文本标准化。"""
import re
from typing import Any


def is_qq_protocol(config) -> bool:
    return str(config.protocol).lower() in {"onebot", "milky"}


def is_feishu_protocol(config) -> bool:
    return str(config.protocol).lower() == "feishu"


def normalize_group_message_text(config, text: str) -> str:
    msg = str(text or "").strip()
    if str(config.protocol).lower() == "feishu":
        msg = re.sub(r"^(?:@\S+\s*)+", "", msg).strip()
        msg = msg.replace("\u200b", "").replace("\ufeff", "").strip()
    return msg


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
