"""Short-message shaping and human-like reply post-processing."""

from __future__ import annotations

import random
import re
from typing import Any, Iterable, Sequence

from plugins.JianerMate.config import StyleOptions


_MARKDOWN_PATTERNS = (
    (re.compile(r"```[\s\S]*?```"), " "),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),
    (re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)"), r"\1"),
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE), ""),
    (re.compile(r"^\s*[-*+]\s+", re.MULTILINE), ""),
    (re.compile(r"^\s*\d+[.、]\s+", re.MULTILINE), ""),
)
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;…~～])")
_URL_PATTERN = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_MENTION_PATTERN = re.compile(r"@\S+")
_ASCII_RUN = re.compile(r"[A-Za-z0-9_]+")
_BRACKET_PATTERN = re.compile(r"[（(【\[][^）)】\]]*[）)】\]]")
_TYPO_MAP = {
    "你": "泥",
    "我": "窝",
    "的": "滴",
    "了": "惹",
    "好": "豪",
    "呢": "捏",
    "吗": "嘛",
    "是": "似",
    "在": "再",
    "会": "汇",
    "就": "旧",
    "很": "狠",
    "说": "梭",
}


def sanitize_messages(
    raw: Sequence[Any],
    options: StyleOptions,
    *,
    rng: random.Random | None = None,
) -> list[str]:
    """Flatten model output into short, plain-text chat messages."""

    del rng  # reserved for future shaping knobs
    produced: list[str] = []
    for item in raw or ():
        text = _clean(str(item or ""))
        if not text:
            continue
        for line in _split_lines(text):
            for chunk in _split_long(line, options.max_chars):
                cleaned = _clean(chunk)
                if cleaned:
                    produced.append(cleaned)
        if len(produced) >= options.max_parts:
            break
    return produced[: options.max_parts]


def inject_typo(
    text: str,
    *,
    probability: float,
    rng: random.Random,
) -> tuple[str, bool]:
    """Introduce at most one homophone-style typo outside protected spans."""

    if probability <= 0 or rng.random() >= probability:
        return text, False
    protected = _protected_mask(text)
    positions = [
        index
        for index, char in enumerate(text)
        if char in _TYPO_MAP and not protected[index]
    ]
    if not positions:
        return text, False
    index = rng.choice(positions)
    return f"{text[:index]}{_TYPO_MAP[text[index]]}{text[index + 1:]}", True


def decorate(
    text: str,
    options: StyleOptions,
    *,
    rng: random.Random,
) -> str:
    """Occasionally append a kaomoji or prepend a catchphrase."""

    if options.kaomoji_probability <= 0 or rng.random() >= options.kaomoji_probability:
        return text
    if options.kaomoji and rng.random() < 0.5:
        return f"{text} {rng.choice(options.kaomoji)}"
    if options.catchphrases:
        return f"{rng.choice(options.catchphrases)}，{text}"
    return text


def sample_delay_ms(options: StyleOptions, *, rng: random.Random) -> int:
    low = max(0, int(options.delay_min_ms))
    high = max(low, int(options.delay_max_ms))
    if high == low:
        return low
    return rng.randint(low, high)


def _clean(text: str) -> str:
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    for pattern, replacement in _MARKDOWN_PATTERNS:
        value = pattern.sub(replacement, value)
    value = re.sub(r"[ \t\u3000]+", " ", value)
    value = re.sub(r"\n+", " ", value)
    return value.strip().strip("`").strip()


def _split_lines(text: str) -> Iterable[str]:
    for line in text.split("\n"):
        line = line.strip()
        if line:
            yield line


def _split_long(text: str, max_chars: int) -> list[str]:
    soft = max(4, int(max_chars))
    hard = soft * 3
    if len(text) <= soft:
        return [text]
    pieces = [piece for piece in _SENTENCE_SPLIT.split(text) if piece.strip()]
    if len(pieces) <= 1:
        return [_truncate(text, hard)]
    output: list[str] = []
    buffer = ""
    for piece in pieces:
        if not buffer:
            buffer = piece
        elif len(buffer) + len(piece) <= soft:
            buffer += piece
        else:
            output.append(buffer)
            buffer = piece
    if buffer:
        output.append(buffer)
    return [_truncate(item.strip(), hard) for item in output if item.strip()]


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[: max(1, limit - 1)].rstrip()}…"


def _protected_mask(text: str) -> list[bool]:
    mask = [False] * len(text)
    for pattern in (_URL_PATTERN, _MENTION_PATTERN, _ASCII_RUN, _BRACKET_PATTERN):
        for match in pattern.finditer(text):
            for index in range(match.start(), match.end()):
                mask[index] = True
    return mask
