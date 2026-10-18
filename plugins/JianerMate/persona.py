"""MaiBot-style persona model, LLM transcription, and preset import.

The persona layout mirrors MaiBot's ``[personality]`` section: a short
identity, a behaviour style, a reply style, plus optional alternate reply
styles that are injected into a single reply with a configurable probability.
Group and private chats get separate chat prompts, matching MaiBot's
``group_chat_prompt`` / ``private_chat_prompts``.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from plugins.JianerMate.providers import OpenAICompatibleClient, ProviderError


DEFAULT_BOT_NAME = "简儿"
DEFAULT_PERSONALITY = (
    "你是常年混在 QQ 群里的普通群友，性格轻松、嘴甜、有点机灵，不端着，"
    "也不刻意卖萌。"
)
DEFAULT_BEHAVIOR_STYLE = (
    "只在有话可说时开口，优先接住群里正在聊的话题；不刷屏、不抢话、"
    "不复读别人刚说过的话；被 @ 时认真回应，不敷衍。"
)
DEFAULT_REPLY_STYLE = (
    "说话要短，像真人打字：一次最多两三条，每条尽量不超过二十个字；"
    "用日常口语，不用 Markdown、不列清单、不写长段落、不复述问题。"
)
DEFAULT_GROUP_CHAT_PROMPT = (
    "你正在 QQ 群里和群友聊天，下面是群里最近聊的内容，里面包含文字和表情包。"
    "回复尽量简短，一次只接一个话题，别把话题扯散；不用刻意回复别人的表情包，"
    "理解它表达的意思就好；控制发言频率，不用回复每个人的消息，"
    "优先接你感兴趣或者被点名的内容。"
)
DEFAULT_PRIVATE_CHAT_PROMPT = (
    "你正在私聊里聊天，回复尽量简短、自然；注意把握聊天内容，"
    "看对方的发言频率决定接话节奏，不要一句话问一堆问题。"
)

MAX_NAME_CHARS = 40
MAX_ALIAS_CHARS = 40
MAX_PERSONA_FIELD_CHARS = 600
MAX_STYLE_CHARS = 400
MAX_MULTIPLE_STYLES = 8
MAX_ALIASES = 8


class PersonaError(RuntimeError):
    """Raised when a persona payload cannot be used."""


@dataclass(frozen=True, slots=True)
class Persona:
    """One persona shared by every conversation."""

    name: str = DEFAULT_BOT_NAME
    aliases: tuple[str, ...] = ()
    personality: str = DEFAULT_PERSONALITY
    behavior_style: str = DEFAULT_BEHAVIOR_STYLE
    reply_style: str = DEFAULT_REPLY_STYLE
    multiple_reply_styles: tuple[str, ...] = ()
    multiple_probability: float = 0.0
    group_chat_prompt: str = DEFAULT_GROUP_CHAT_PROMPT
    private_chat_prompt: str = DEFAULT_PRIVATE_CHAT_PROMPT

    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #
    @classmethod
    def from_value(
        cls,
        value: Any,
        *,
        fallback_name: str = DEFAULT_BOT_NAME,
    ) -> "Persona":
        name = _clean(str(fallback_name or DEFAULT_BOT_NAME), MAX_NAME_CHARS) or (
            DEFAULT_BOT_NAME
        )
        if isinstance(value, Mapping):
            return cls(
                name=_clean(value.get("name"), MAX_NAME_CHARS) or name,
                aliases=_clean_aliases(
                    value.get("aliases", value.get("alias_names"))
                ),
                personality=_clean(
                    value.get("personality"), MAX_PERSONA_FIELD_CHARS
                )
                or DEFAULT_PERSONALITY,
                behavior_style=_clean(
                    value.get("behavior_style"), MAX_PERSONA_FIELD_CHARS
                )
                or DEFAULT_BEHAVIOR_STYLE,
                reply_style=_clean(
                    value.get("reply_style"), MAX_PERSONA_FIELD_CHARS
                )
                or DEFAULT_REPLY_STYLE,
                multiple_reply_styles=_clean_styles(
                    value.get("multiple_reply_styles")
                ),
                multiple_probability=_clamp_probability(
                    value.get("multiple_probability"), 0.0
                ),
                group_chat_prompt=_clean(
                    value.get("group_chat_prompt"), MAX_PERSONA_FIELD_CHARS
                )
                or DEFAULT_GROUP_CHAT_PROMPT,
                private_chat_prompt=_clean(
                    value.get("private_chat_prompt"), MAX_PERSONA_FIELD_CHARS
                )
                or DEFAULT_PRIVATE_CHAT_PROMPT,
            )
        if isinstance(value, str) and value.strip():
            # Legacy shape: one free-form template is treated as personality.
            return cls(
                name=name,
                aliases=(),
                personality=_clean(value, MAX_PERSONA_FIELD_CHARS),
                behavior_style=DEFAULT_BEHAVIOR_STYLE,
                reply_style=DEFAULT_REPLY_STYLE,
            )
        return cls(name=name)

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        base: "Persona | None" = None,
        fallback_name: str = DEFAULT_BOT_NAME,
    ) -> "Persona":
        """Merge a model-produced payload over ``base`` (or the defaults)."""

        if not isinstance(payload, Mapping):
            raise PersonaError("人格数据必须是 JSON 对象")
        current = base or cls.from_value(None, fallback_name=fallback_name)
        values: dict[str, Any] = {
            "name": current.name,
            "aliases": current.aliases,
            "personality": current.personality,
            "behavior_style": current.behavior_style,
            "reply_style": current.reply_style,
            "multiple_reply_styles": current.multiple_reply_styles,
            "multiple_probability": current.multiple_probability,
            "group_chat_prompt": current.group_chat_prompt,
            "private_chat_prompt": current.private_chat_prompt,
        }
        for key in (
            "name",
            "personality",
            "behavior_style",
            "reply_style",
            "group_chat_prompt",
            "private_chat_prompt",
        ):
            if key in payload and payload[key] is not None:
                limit = MAX_NAME_CHARS if key == "name" else MAX_PERSONA_FIELD_CHARS
                cleaned = _clean(payload[key], limit)
                if cleaned:
                    values[key] = cleaned
        if payload.get("multiple_reply_styles") is not None:
            values["multiple_reply_styles"] = _clean_styles(
                payload.get("multiple_reply_styles")
            )
        if payload.get("aliases") is not None:
            values["aliases"] = _clean_aliases(payload.get("aliases"))
        if payload.get("multiple_probability") is not None:
            values["multiple_probability"] = _clamp_probability(
                payload.get("multiple_probability"),
                current.multiple_probability,
            )
        return cls(**values)

    # ------------------------------------------------------------------ #
    # serialisation and rendering
    # ------------------------------------------------------------------ #
    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "aliases": list(self.aliases),
            "personality": self.personality,
            "behavior_style": self.behavior_style,
            "reply_style": self.reply_style,
            "multiple_reply_styles": list(self.multiple_reply_styles),
            "multiple_probability": self.multiple_probability,
            "group_chat_prompt": self.group_chat_prompt,
            "private_chat_prompt": self.private_chat_prompt,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), ensure_ascii=False, separators=(",", ":"))

    def render(self, mode: str = "group") -> str:
        chat_prompt = (
            self.private_chat_prompt if mode == "private" else self.group_chat_prompt
        )
        return "\n".join(
            line
            for line in (
                f"你的名字是 {self.name}。",
                self.personality,
                self.behavior_style,
                self.reply_style,
                chat_prompt,
            )
            if str(line or "").strip()
        ).strip()

    # ------------------------------------------------------------------ #
    # behaviour helpers
    # ------------------------------------------------------------------ #
    def pick_multiple_style(self, rng: random.Random) -> tuple[str | None, str]:
        """Return a one-off alternate style plus a short label for logs."""

        if self.multiple_probability <= 0 or not self.multiple_reply_styles:
            return None, ""
        if rng.random() >= self.multiple_probability:
            return None, ""
        index = rng.randrange(len(self.multiple_reply_styles))
        return self.multiple_reply_styles[index], f"备用风格#{index + 1}"

    def mentions_name(self, text: str) -> bool:
        """Whether ``text`` calls the bot by its name or one of its aliases."""

        haystack = str(text or "").casefold()
        if not haystack:
            return False
        candidates = (self.name, *self.aliases)
        for candidate in candidates:
            needle = str(candidate or "").strip().casefold()
            if len(needle) >= 2 and needle in haystack:
                return True
        return False

    def with_multiple_probability(self, value: Any) -> "Persona":
        return Persona(
            name=self.name,
            aliases=self.aliases,
            personality=self.personality,
            behavior_style=self.behavior_style,
            reply_style=self.reply_style,
            multiple_reply_styles=self.multiple_reply_styles,
            multiple_probability=_clamp_probability(
                value, self.multiple_probability
            ),
            group_chat_prompt=self.group_chat_prompt,
            private_chat_prompt=self.private_chat_prompt,
        )

    def summary_lines(self) -> list[str]:
        lines = [
            f"名字：{self.name}",
            f"别名：{'、'.join(self.aliases) if self.aliases else '（无）'}",
            f"性格：{_shorten(self.personality)}",
            f"行动：{_shorten(self.behavior_style)}",
            f"说话：{_shorten(self.reply_style)}",
            f"备用风格：{len(self.multiple_reply_styles)} 条"
            f"｜切换概率：{self.multiple_probability:.2f}",
            f"群聊提示：{_shorten(self.group_chat_prompt, 60)}",
            f"私聊提示：{_shorten(self.private_chat_prompt, 60)}",
        ]
        if self.multiple_reply_styles:
            lines.append("备用风格列表：")
            for index, style in enumerate(self.multiple_reply_styles, start=1):
                lines.append(f"{index}. {_shorten(style, 60)}")
        return lines


# ---------------------------------------------------------------------- #
# LLM transcription
# ---------------------------------------------------------------------- #
PERSONA_CONVERSION_RULES = (
    "你是一个人格设定整理器。用户会给你一份旧的机器人角色设定，"
    "可能来自旧插件、角色卡或一段随手写的描述。请把它改写成结构化的群聊人格设定。\n"
    "只输出一个 JSON 对象，不要输出解释，字段如下：\n"
    '{"name": "名字", "aliases": ["别人可能用来称呼她的名字"], '
    '"personality": "她是谁、什么性格（1-2 句）", '
    '"behavior_style": "行动准则：什么时候插话、什么时候安静", '
    '"reply_style": "说话习惯：长短、语气、口头禅", '
    '"multiple_reply_styles": ["备用说话风格，最多 5 条，没有就留空数组"], '
    '"multiple_probability": 0.0, '
    '"group_chat_prompt": "群聊时的补充要求", '
    '"private_chat_prompt": "私聊时的补充要求"}\n'
    "要求：保留原文里的事实、称呼、口癖和禁忌，不要凭空添加身份或经历；"
    "原文没有提到的字段就用合理默认值；文字用简体中文，每段控制在一段以内；"
    "multiple_probability 建议 0，除非原文明确要求随机切换风格。"
)


class PersonaConverter:
    """Ask a model to turn an existing setting into a Persona payload."""

    def __init__(
        self,
        client: OpenAICompatibleClient,
        *,
        logger: Any | None = None,
    ) -> None:
        self.client = client
        self._logger = logger

    async def convert(
        self,
        *,
        model_key: str,
        source_text: str,
        source_name: str = "",
        base: Persona | None = None,
    ) -> Persona:
        body = str(source_text or "").strip()
        if not body:
            raise PersonaError("原始设定是空的")
        user_prompt = (
            f"原始设定名称：{source_name.strip() or '（未命名）'}\n"
            f"原始设定内容：\n{body[:6000]}"
        )
        payload = await self.client.complete_json(
            model_key,
            system_prompt=PERSONA_CONVERSION_RULES,
            user_text=user_prompt,
            max_tokens=900,
            temperature=0.2,
        )
        if not isinstance(payload, Mapping) or not any(
            str(payload.get(key) or "").strip()
            for key in ("personality", "behavior_style", "reply_style")
        ):
            raise PersonaError("模型没有给出可用的人格字段")
        return Persona.from_payload(payload, base=base)


# ---------------------------------------------------------------------- #
# JianerAI preset import (read-only, no code dependency)
# ---------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class PresetEntry:
    key: str
    name: str
    info: str
    template: str
    path: str

    @property
    def label(self) -> str:
        return f"{self.name}（{self.key}）" if self.name else self.key


def load_jianer_ai_presets(
    root: str | Path,
) -> tuple[dict[str, PresetEntry], dict[str, str]]:
    """Read ``prerequisites/current.json`` and its templates if present."""

    base = Path(root)
    metadata_path = base / "prerequisites" / "current.json"
    entries: dict[str, PresetEntry] = {}
    errors: dict[str, str] = {}
    if not metadata_path.is_file():
        return entries, errors
    try:
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors[str(metadata_path)] = f"预设文件读取失败: {exc.__class__.__name__}"
        return entries, errors
    if not isinstance(raw, Mapping):
        errors[str(metadata_path)] = "预设文件必须是 JSON 对象"
        return entries, errors
    for key, value in raw.items():
        preset_key = str(key or "").strip()
        if not preset_key or not isinstance(value, Mapping):
            continue
        relative = str(value.get("path") or f"{preset_key}.txt")
        template_path = Path(relative)
        if not template_path.is_absolute():
            template_path = base / "prerequisites" / template_path
        try:
            template = template_path.read_text(encoding="utf-8")
        except OSError as exc:
            errors[preset_key] = f"模板读取失败: {exc.__class__.__name__}"
            continue
        if not template.strip():
            errors[preset_key] = "模板是空的"
            continue
        entries[preset_key] = PresetEntry(
            key=preset_key,
            name=str(value.get("name") or preset_key),
            info=str(value.get("info") or ""),
            template=template,
            path=str(template_path),
        )
    return entries, errors


def resolve_preset(
    catalog: Mapping[str, PresetEntry],
    query: str = "",
) -> PresetEntry | None:
    """Match a preset by key or display name; fall back to the default."""

    if not catalog:
        return None
    wanted = str(query or "").strip()
    if not wanted:
        return default_preset(catalog)
    if wanted in catalog:
        return catalog[wanted]
    lowered = wanted.casefold()
    for key, entry in catalog.items():
        if key.casefold() == lowered or entry.name.casefold() == lowered:
            return entry
    for key, entry in catalog.items():
        if lowered in key.casefold() or lowered in entry.name.casefold():
            return entry
    return None


def default_preset(catalog: Mapping[str, PresetEntry]) -> PresetEntry | None:
    if not catalog:
        return None
    for preferred in ("Normal", "XingYu", "default"):
        if preferred in catalog:
            return catalog[preferred]
    return catalog[sorted(catalog)[0]]


# ---------------------------------------------------------------------- #
# helpers
# ---------------------------------------------------------------------- #
def _clean(value: Any, limit: int) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return text[:limit].strip()


def _clean_styles(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items: Sequence[Any] = [
            item for item in value.replace("，", ",").split(",")
        ]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items = list(value)
    else:
        return ()
    cleaned: list[str] = []
    for item in items:
        text = _clean(item, MAX_STYLE_CHARS)
        if text and text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= MAX_MULTIPLE_STYLES:
            break
    return tuple(cleaned)


def _clean_aliases(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items: Sequence[Any] = [
            item for item in value.replace("，", ",").split(",")
        ]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items = list(value)
    else:
        return ()
    cleaned: list[str] = []
    for item in items:
        text = _clean(item, MAX_ALIAS_CHARS)
        if text and text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= MAX_ALIASES:
            break
    return tuple(cleaned)


def _clamp_probability(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    return max(0.0, min(1.0, parsed))


def _shorten(text: str, limit: int = 80) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)] + "…"
