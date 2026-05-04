"""黑名单读取与 emoji 判定工具。"""
import emoji


def load_blacklist():
    try:
        with open("blacklist.sr", "r", encoding="utf-8") as f:
            blacklist115 = set(line.strip() for line in f)
        return blacklist115
    except FileNotFoundError:
        return set()


def has_emoji(s: str) -> bool:
    # emoji +1：恰好包含一个 emoji 且字符串长度为 1
    return emoji.emoji_count(s) == 1 and len(s) == 1
