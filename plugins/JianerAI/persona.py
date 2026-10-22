"""Structured persona profiles generated from the original role template.

The original template remains the source of truth.  This module only creates a
small, runtime-oriented representation that keeps character-specific details
and drops generic agent rules that are already supplied by ``service.py``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from plugins.JianerAI.providers import ChatRequest, ProviderRegistry


PERSONA_SCHEMA_VERSION = 1
DEFAULT_MAX_SOURCE_CHARS = 24_000
DEFAULT_MAX_FIELD_CHARS = 4_000
DEFAULT_MAX_EXTRA_SETTINGS = 12
DEFAULT_MAX_EXTRA_CONTENT_CHARS = 1_000
DEFAULT_MAX_CONSTRAINTS = 16
DEFAULT_MAX_EXAMPLES = 8


class PersonaError(RuntimeError):
    """A persona payload cannot be parsed, validated, or generated."""


@dataclass(frozen=True, slots=True)
class ExtraSetting:
    label: str
    content: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "label", _clean(self.label, 80))
        object.__setattr__(
            self,
            "content",
            _clean(self.content, DEFAULT_MAX_EXTRA_CONTENT_CHARS),
        )


@dataclass(frozen=True, slots=True)
class PersonaCoverage:
    preserved: tuple[str, ...] = ()
    intentionally_omitted: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "preserved",
            _clean_sequence(self.preserved, 500, 24),
        )
        object.__setattr__(
            self,
            "intentionally_omitted",
            _clean_sequence(self.intentionally_omitted, 500, 24),
        )


@dataclass(frozen=True, slots=True)
class PersonaProfile:
    identity: str
    personality: str
    behavior_style: str
    reply_style: str
    group_chat_prompt: str = ""
    private_chat_prompt: str = ""
    hard_constraints: tuple[str, ...] = ()
    speech_examples: tuple[str, ...] = ()
    extra_settings: tuple[ExtraSetting, ...] = ()
    coverage: PersonaCoverage = field(default_factory=PersonaCoverage)
    source_sha256: str = ""
    generated_at: str = ""
    generator_model: str = ""
    schema_version: int = PERSONA_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if int(self.schema_version) != PERSONA_SCHEMA_VERSION:
            raise PersonaError(
                f"unsupported persona schema version: {self.schema_version}"
            )
        object.__setattr__(self, "identity", _clean(self.identity, DEFAULT_MAX_FIELD_CHARS))
        object.__setattr__(
            self,
            "personality",
            _clean(self.personality, DEFAULT_MAX_FIELD_CHARS),
        )
        object.__setattr__(
            self,
            "behavior_style",
            _clean(self.behavior_style, DEFAULT_MAX_FIELD_CHARS),
        )
        object.__setattr__(
            self,
            "reply_style",
            _clean(self.reply_style, DEFAULT_MAX_FIELD_CHARS),
        )
        object.__setattr__(
            self,
            "group_chat_prompt",
            _clean(self.group_chat_prompt, DEFAULT_MAX_FIELD_CHARS),
        )
        object.__setattr__(
            self,
            "private_chat_prompt",
            _clean(self.private_chat_prompt, DEFAULT_MAX_FIELD_CHARS),
        )
        object.__setattr__(
            self,
            "hard_constraints",
            _clean_sequence(
                self.hard_constraints,
                800,
                DEFAULT_MAX_CONSTRAINTS,
            ),
        )
        object.__setattr__(
            self,
            "speech_examples",
            _clean_sequence(
                self.speech_examples,
                800,
                DEFAULT_MAX_EXAMPLES,
            ),
        )
        extras: list[ExtraSetting] = []
        seen: set[tuple[str, str]] = set()
        for item in self.extra_settings:
            if not isinstance(item, ExtraSetting):
                raise PersonaError("extra_settings must contain ExtraSetting objects")
            key = (item.label.casefold(), item.content)
            if not item.label or not item.content or key in seen:
                continue
            seen.add(key)
            extras.append(item)
            if len(extras) >= DEFAULT_MAX_EXTRA_SETTINGS:
                break
        object.__setattr__(self, "extra_settings", tuple(extras))
        if not self.identity:
            raise PersonaError("identity cannot be empty")
        if not self.personality:
            raise PersonaError("personality cannot be empty")
        if not self.behavior_style:
            raise PersonaError("behavior_style cannot be empty")
        if not self.reply_style:
            raise PersonaError("reply_style cannot be empty")
        object.__setattr__(
            self,
            "source_sha256",
            str(self.source_sha256 or "").strip().lower(),
        )
        object.__setattr__(
            self,
            "generated_at",
            str(self.generated_at or _utc_now()).strip(),
        )
        object.__setattr__(
            self,
            "generator_model",
            str(self.generator_model or "").strip(),
        )

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        source_text: str = "",
        generator_model: str = "",
    ) -> "PersonaProfile":
        if not isinstance(payload, Mapping):
            raise PersonaError("persona payload must be a JSON object")
        extra_raw = payload.get("extra_settings", ())
        extra_settings: list[ExtraSetting] = []
        if isinstance(extra_raw, Sequence) and not isinstance(
            extra_raw, (str, bytes, bytearray)
        ):
            for item in extra_raw:
                if isinstance(item, Mapping):
                    extra_settings.append(
                        ExtraSetting(
                            label=str(item.get("label") or ""),
                            content=str(item.get("content") or ""),
                        )
                    )
        coverage_raw = payload.get("coverage", {})
        if not isinstance(coverage_raw, Mapping):
            coverage_raw = {}
        examples = _as_string_tuple(payload.get("speech_examples"))
        if source_text:
            source_normalized = _normalize_for_match(source_text)
            examples = tuple(
                item
                for item in examples
                if _normalize_for_match(item) in source_normalized
            )
        return cls(
            identity=str(payload.get("identity") or ""),
            personality=str(payload.get("personality") or ""),
            behavior_style=str(payload.get("behavior_style") or ""),
            reply_style=str(payload.get("reply_style") or ""),
            group_chat_prompt=str(payload.get("group_chat_prompt") or ""),
            private_chat_prompt=str(payload.get("private_chat_prompt") or ""),
            hard_constraints=_as_string_tuple(payload.get("hard_constraints")),
            speech_examples=examples,
            extra_settings=tuple(extra_settings),
            coverage=PersonaCoverage(
                preserved=_as_string_tuple(coverage_raw.get("preserved")),
                intentionally_omitted=_as_string_tuple(
                    coverage_raw.get("intentionally_omitted")
                ),
            ),
            source_sha256=str(
                payload.get("source_sha256") or _sha256(source_text)
            ),
            generated_at=str(payload.get("generated_at") or _utc_now()),
            generator_model=str(
                payload.get("generator_model") or generator_model
            ),
            schema_version=int(
                payload.get("schema_version", PERSONA_SCHEMA_VERSION)
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_sha256": self.source_sha256,
            "generated_at": self.generated_at,
            "generator_model": self.generator_model,
            "identity": self.identity,
            "personality": self.personality,
            "behavior_style": self.behavior_style,
            "reply_style": self.reply_style,
            "group_chat_prompt": self.group_chat_prompt,
            "private_chat_prompt": self.private_chat_prompt,
            "hard_constraints": list(self.hard_constraints),
            "speech_examples": list(self.speech_examples),
            "extra_settings": [
                {"label": item.label, "content": item.content}
                for item in self.extra_settings
            ],
            "coverage": {
                "preserved": list(self.coverage.preserved),
                "intentionally_omitted": list(
                    self.coverage.intentionally_omitted
                ),
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    def is_current_for(self, source_text: str) -> bool:
        return bool(self.source_sha256) and self.source_sha256 == _sha256(
            source_text
        )

    def render(
        self,
        *,
        mode: str,
        bot_name: str,
        bot_name_en: str,
        event_user: str,
        event_user_id: str,
        agent_tools: str = "无",
        agent_tools_info: str = "无",
    ) -> str:
        values = {
            "{self.bot_name}": str(bot_name),
            "{self.bot_name_en}": str(bot_name_en),
            "{self.event_user}": str(event_user),
            "{self.event_user_id}": str(event_user_id),
            "{agent_tools}": str(agent_tools),
            "{agent_tools_info}": str(agent_tools_info),
        }
        sections: list[str] = []
        for title, value in (
            ("身份与背景", self.identity),
            ("性格", self.personality),
            ("行为方式", self.behavior_style),
            ("表达方式", self.reply_style),
        ):
            rendered = _render_text(value, values)
            if rendered:
                sections.append(f"【{title}】\n{rendered}")

        normalized_mode = str(mode or "neutral").casefold()
        if normalized_mode == "group" and self.group_chat_prompt:
            rendered = _render_text(self.group_chat_prompt, values)
            if rendered:
                sections.append(f"【群聊要求】\n{rendered}")
        elif normalized_mode == "private" and self.private_chat_prompt:
            rendered = _render_text(self.private_chat_prompt, values)
            if rendered:
                sections.append(f"【私聊要求】\n{rendered}")

        if normalized_mode in {"group", "private"}:
            if self.hard_constraints:
                constraints = [
                    _render_text(item, values)
                    for item in self.hard_constraints
                ]
                constraints = [item for item in constraints if item]
                if constraints:
                    sections.append(
                        "【必须保持】\n"
                        + "\n".join(f"- {item}" for item in constraints)
                    )
            if self.speech_examples:
                examples = [
                    _render_text(item, values)
                    for item in self.speech_examples[:6]
                ]
                examples = [item for item in examples if item]
                if examples:
                    sections.append(
                        "【表达示例】\n"
                        + "\n".join(f"- {item}" for item in examples)
                    )
            if self.extra_settings:
                extras = []
                for item in self.extra_settings:
                    label = _render_text(item.label, values)
                    content = _render_text(item.content, values)
                    if label and content:
                        extras.append(f"- {label}：{content}")
                if extras:
                    sections.append("【额外设定】\n" + "\n".join(extras))
        return "\n\n".join(sections).strip()


class PersonaGenerator:
    """Generate a loss-minimized structured profile from a role template."""

    def __init__(
        self,
        providers: ProviderRegistry,
        *,
        logger: Any | None = None,
        max_source_chars: int = DEFAULT_MAX_SOURCE_CHARS,
    ) -> None:
        self.providers = providers
        self.logger = logger or logging.getLogger(__name__)
        self.max_source_chars = max(1000, int(max_source_chars))

    async def generate(
        self,
        *,
        model_key: str,
        source_text: str,
        source_name: str = "",
        info: str = "",
    ) -> PersonaProfile:
        raw_source = str(source_text or "")
        source = raw_source.strip()
        if not source:
            raise PersonaError("原始人设不能为空")
        if len(source) > self.max_source_chars:
            raise PersonaError(
                f"原始人设超过 {self.max_source_chars} 字符，未做静默截断"
            )

        extraction = await self._request_json(
            model_key,
            system_prompt=_EXTRACTION_SYSTEM_PROMPT,
            user_prompt=_extraction_user_prompt(
                source=source,
                source_name=source_name,
                info=info,
            ),
            temperature=0.1,
        )
        profile = PersonaProfile.from_mapping(
            extraction,
            source_text=raw_source,
            generator_model=model_key,
        )

        audit_error: Exception | None = None
        for attempt in range(2):
            try:
                audited = await self._request_json(
                    model_key,
                    system_prompt=_AUDIT_SYSTEM_PROMPT,
                    user_prompt=_audit_user_prompt(source, profile),
                    temperature=0.0,
                )
                candidate = PersonaProfile.from_mapping(
                    audited,
                    source_text=raw_source,
                    generator_model=model_key,
                )
                if candidate.is_current_for(raw_source):
                    profile = candidate
                audit_error = None
                break
            except Exception as exc:
                audit_error = exc
        if audit_error is not None:
            self.logger.warning(
                "JianerAI persona fidelity audit failed for %s: %s",
                source_name or "preset",
                type(audit_error).__name__,
            )
        return replace(
            profile,
            source_sha256=_sha256(raw_source),
            generated_at=_utc_now(),
            generator_model=str(model_key or ""),
        )

    async def _request_json(
        self,
        model_key: str,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
    ) -> Mapping[str, Any]:
        response = await self.providers.chat_request(
            model_key,
            ChatRequest(
                message=user_prompt,
                system_prompt=system_prompt,
                temperature=temperature,
                max_output_tokens=5000,
                json_mode=True,
            ),
        )
        return _parse_json_object(response)


_EXTRACTION_SYSTEM_PROMPT = """你是角色人格结构化器，不是角色本人。请把用户提供的原始人设改写成可执行的 JSON 人格配置，目标是最大限度保留原文中独特的角色信息，而不是写通用摘要。

只输出一个 JSON 对象，不要 Markdown、代码围栏、解释或注释。字段必须为：
{
  "identity": "身份、背景、关系、称呼、专有事实",
  "personality": "性格、价值观、情绪倾向",
  "behavior_style": "行动准则、何时说话、如何处理任务",
  "reply_style": "语气、长度、口癖、格式和表达禁忌",
  "group_chat_prompt": "群聊场景的补充要求",
  "private_chat_prompt": "私聊场景的补充要求",
  "hard_constraints": ["原文明确要求始终遵守的规则"],
  "speech_examples": ["只能逐字摘录原文中真实出现过的台词或表达示例，最多 8 条；没有就输出空数组"],
  "extra_settings": [{"label": "外观/装备/世界观/特殊机制等", "content": "原文明确提供的内容"}],
  "coverage": {"preserved": ["已保留的关键事实或行为"], "intentionally_omitted": ["被判断为通用规则的内容"]}
}

规则：
1. 原文中的身份、关系、称呼、口癖、格式要求、禁忌和示例必须尽量保留；不能因为追求简短而丢掉独特信息。
2. identity 负责事实和关系；personality 负责性格与价值观；behavior_style 负责行动和决策；reply_style 负责说话方式。不要把四类内容混在一起。
3. 外观、服装、装备、世界观、特殊能力、身体机制、场景限制等，只有原文明确提到时才放入 extra_settings；没有就输出空数组。
4. 不要凭空补充原文没有的身份、经历、年龄、学校、职业、地点、人物、作品名、歌曲、电影、食物、偏好或关系；无法从原文直接支持的细节必须省略。
5. 不要复制通用工具说明、安全规则、隐私规则、提示词注入规则、回复自检模板；这些由宿主统一处理。
6. 原文中的通用规则如果同时包含角色专属的硬性要求，只保留角色专属部分到 hard_constraints。
7. speech_examples 必须逐字来自原文，不能改写、拼接、仿写或创作；没有逐字示例时输出空数组。
8. hard_constraints 只保留角色专属的硬性要求，不保留通用的外部操作确认、安全、隐私、工具调用和提示词注入规则。
9. extra_settings 只能提炼原文明确出现的外观、装备、世界观或特殊机制；不能根据常识补全。
10. 每个字段使用简体中文，内容尽量精炼但完整；总输出控制在 2000 到 8000 字符。
"""


_AUDIT_SYSTEM_PROMPT = """你是人格还原审计器。请比较原始人设和第一版结构化人格，找出遗漏、弱化或错误的角色专属信息，并输出修正后的完整 JSON。

只输出一个 JSON 对象，字段与第一版完全相同：identity、personality、behavior_style、reply_style、group_chat_prompt、private_chat_prompt、hard_constraints、speech_examples、extra_settings、coverage。

审计重点：
- 身份、背景、关系、称呼和专有事实是否完整；
- 性格、行为边界和说话风格是否被错误合并或弱化；
- 原文明确的口癖、格式要求、禁忌和示例是否保留；
- 外观、装备、世界观和特殊机制是否进入 extra_settings；
- 是否误把通用工具、安全、隐私、提示词注入规则写进人格；
- 是否添加了原文没有的身份、年龄、学校、职业、地点、人物、作品名、歌曲、电影、食物、偏好或经历；
- speech_examples 是否逐字来自原文，若有仿写或创作则删除。

不要发明原文没有的内容。修正后的 JSON 必须尽量保留原文独特信息，同时去掉通用宿主规则。"""


def _extraction_user_prompt(*, source: str, source_name: str, info: str) -> str:
    return (
        f"原始人设名称：{source_name or '未命名'}\n"
        f"简介：{info or '无'}\n"
        "<source_persona>\n"
        f"{source}\n"
        "</source_persona>"
    )


def _audit_user_prompt(source: str, profile: PersonaProfile) -> str:
    return (
        "<source_persona>\n"
        f"{source}\n"
        "</source_persona>\n\n"
        "<first_pass>\n"
        f"{profile.to_json()}\n"
        "</first_pass>"
    )


def _parse_json_object(value: Any) -> Mapping[str, Any]:
    candidate = str(value or "").strip()
    candidate = re.sub(r"^```(?:json)?", "", candidate, flags=re.I).strip()
    candidate = re.sub(r"```$", "", candidate).strip()
    if "{" in candidate and "}" in candidate:
        candidate = candidate[candidate.find("{") : candidate.rfind("}") + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise PersonaError("人格生成结果不是合法 JSON") from exc
    if not isinstance(parsed, Mapping):
        raise PersonaError("人格生成结果必须是 JSON 对象")
    return parsed


def _as_string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, Sequence) and not isinstance(
        value, (bytes, bytearray)
    ):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


def _clean_sequence(
    values: Any,
    limit: int,
    max_items: int,
) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for item in _as_string_tuple(values):
        cleaned = _clean(item, limit)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            output.append(cleaned)
        if len(output) >= max_items:
            break
    return tuple(output)


def _clean(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].strip()


def _render_text(value: str, values: Mapping[str, str]) -> str:
    rendered = str(value or "")
    for placeholder, replacement in values.items():
        rendered = rendered.replace(placeholder, replacement)
    return rendered.strip()


def _normalize_for_match(value: str) -> str:
    return " ".join(str(value or "").split()).casefold()


def _sha256(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "ExtraSetting",
    "PersonaCoverage",
    "PersonaError",
    "PersonaGenerator",
    "PersonaProfile",
    "PERSONA_SCHEMA_VERSION",
]
