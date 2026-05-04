"""协议判定与群消息文本标准化。"""
import re


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
