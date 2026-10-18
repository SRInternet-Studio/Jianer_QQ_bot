import asyncio
import json
import random

import pytest

from plugins.JianerMate.persona import (
    DEFAULT_GROUP_CHAT_PROMPT,
    DEFAULT_PERSONALITY,
    DEFAULT_PRIVATE_CHAT_PROMPT,
    Persona,
    PersonaConverter,
    PersonaError,
    default_preset,
    load_jianer_ai_presets,
    resolve_preset,
)
from plugins.JianerMate.providers import ResponseParseError


def test_persona_from_value_extended_fields():
    persona = Persona.from_value(
        {
            "name": "阿简",
            "personality": "话不多",
            "behavior_style": "只在被点名时说话",
            "reply_style": "最多两个字",
            "multiple_reply_styles": ["用1-2个字回复", "带翻译腔"],
            "multiple_probability": 0.3,
            "group_chat_prompt": "群里要短",
            "private_chat_prompt": "私聊可以多说",
        }
    )
    assert persona.name == "阿简"
    assert persona.multiple_reply_styles == ("用1-2个字回复", "带翻译腔")
    assert persona.multiple_probability == 0.3
    assert persona.group_chat_prompt == "群里要短"
    assert persona.private_chat_prompt == "私聊可以多说"


def test_persona_defaults_and_legacy_string():
    default = Persona.from_value(None)
    assert default.personality == DEFAULT_PERSONALITY
    assert default.group_chat_prompt == DEFAULT_GROUP_CHAT_PROMPT
    assert default.private_chat_prompt == DEFAULT_PRIVATE_CHAT_PROMPT
    assert default.multiple_reply_styles == ()

    legacy = Persona.from_value("你是一个毒舌群友", fallback_name="小简")
    assert legacy.name == "小简"
    assert legacy.personality == "你是一个毒舌群友"


def test_persona_aliases_and_name_mentions():
    persona = Persona.from_value(
        {
            "name": "简儿",
            "alias_names": ["小简", "Jianer"],
        }
    )
    assert persona.aliases == ("小简", "Jianer")
    assert persona.mentions_name("小简在吗") is True
    assert persona.mentions_name("jianer 看一下") is True
    assert persona.mentions_name("今天天气不错") is False
    assert persona.mentions_name("") is False

    payload = persona.to_payload()
    assert payload["aliases"] == ["小简", "Jianer"]
    restored = Persona.from_payload(payload, base=None)
    assert restored.aliases == persona.aliases
    assert any("别名" in line for line in persona.summary_lines())

    merged = Persona.from_payload({"personality": "新性格"}, base=persona)
    assert merged.aliases == persona.aliases
    cleared = Persona.from_payload({"aliases": []}, base=persona)
    assert cleared.aliases == ()


def test_persona_from_payload_merges_over_base_and_sanitises():
    base = Persona.from_value(
        {
            "name": "简儿",
            "multiple_reply_styles": ["保持原风格"],
            "multiple_probability": 0.1,
        }
    )
    persona = Persona.from_payload(
        {
            "personality": "  新的性格  ",
            "reply_style": "",
            "multiple_reply_styles": ["a", "a", "b", "c", "d", "e", "f", "g", "h"],
            "multiple_probability": 4.2,
        },
        base=base,
    )
    assert persona.personality == "新的性格"
    assert persona.reply_style == base.reply_style
    assert persona.name == "简儿"
    assert persona.multiple_reply_styles[:2] == ("a", "b")
    assert len(persona.multiple_reply_styles) <= 8
    assert persona.multiple_probability == 1.0


def test_persona_render_modes_and_round_trip():
    persona = Persona.from_value(
        {
            "name": "阿简",
            "group_chat_prompt": "群里少说话",
            "private_chat_prompt": "私聊多聊会儿",
        }
    )
    group_prompt = persona.render("group")
    private_prompt = persona.render("private")
    assert "你的名字是 阿简" in group_prompt
    assert "群里少说话" in group_prompt
    assert "私聊多聊会儿" in private_prompt
    assert "群里少说话" not in private_prompt

    restored = Persona.from_payload(json.loads(persona.to_json()), base=None)
    assert restored == persona


def test_pick_multiple_style_probability_and_labels():
    persona = Persona.from_value(
        {
            "multiple_reply_styles": ["用1-2个字回复", "带翻译腔"],
            "multiple_probability": 1.0,
        }
    )
    style, label = persona.pick_multiple_style(random.Random(3))
    assert style in persona.multiple_reply_styles
    assert label.startswith("备用风格#")

    never = Persona.from_value({"multiple_probability": 0.0})
    assert never.pick_multiple_style(random.Random(1)) == (None, "")

    empty = Persona.from_value(
        {"multiple_reply_styles": [], "multiple_probability": 1.0}
    )
    assert empty.pick_multiple_style(random.Random(1)) == (None, "")

    adjusted = persona.with_multiple_probability(0.25)
    assert adjusted.multiple_probability == 0.25
    assert adjusted.multiple_reply_styles == persona.multiple_reply_styles


class _StubClient:
    def __init__(self, payload, error=None, errors=()):
        self.payload = payload
        self.error = error
        self.errors = list(errors)
        self.calls = []

    async def complete_json(self, model_key, **kwargs):
        self.calls.append({"model_key": model_key, **kwargs})
        if self.errors:
            raise self.errors.pop(0)
        if self.error is not None:
            raise self.error
        return self.payload


def test_persona_converter_builds_persona_from_model_json():
    client = _StubClient(
        {
            "name": "机娘",
            "personality": "你是一台机娘",
            "behavior_style": "只在被叫到时说话",
            "reply_style": "说话带点机械感",
            "multiple_reply_styles": ["用1-2个字回复"],
            "multiple_probability": 0.2,
        }
    )
    converter = PersonaConverter(client)

    async def scenario():
        return await converter.convert(
            model_key="planner",
            source_text="我是一个机娘，说话简短，喜欢说 desu",
            source_name="机娘",
            base=None,
        )

    persona = asyncio.run(scenario())
    assert persona.name == "机娘"
    assert persona.reply_style == "说话带点机械感"
    assert persona.multiple_reply_styles == ("用1-2个字回复",)
    assert persona.multiple_probability == 0.2
    call = client.calls[0]
    assert call["model_key"] == "planner"
    assert "我是一个机娘" in call["user_text"]
    assert "机娘" in call["user_text"]


def test_persona_converter_rejects_unusable_payload():
    converter = PersonaConverter(_StubClient({"name": "只有名字"}))

    async def scenario():
        await converter.convert(model_key="planner", source_text="原始设定")

    with pytest.raises(PersonaError):
        asyncio.run(scenario())


def test_persona_converter_retries_once_when_json_is_unparsable():
    client = _StubClient(
        {
            "personality": "你是一个机娘",
            "behavior_style": "只在被叫到时说话",
            "reply_style": "说话带点机械感",
        },
        errors=(ResponseParseError("模型返回的 JSON 无法解析"),),
    )
    converter = PersonaConverter(client)

    async def scenario():
        return await converter.convert(
            model_key="gemini", source_text="我是一个机娘"
        )

    persona = asyncio.run(scenario())
    assert persona.personality == "你是一个机娘"
    assert len(client.calls) == 2
    assert "上一次的输出没法被程序解析" not in client.calls[0]["system_prompt"]
    assert "上一次的输出没法被程序解析" in client.calls[1]["system_prompt"]
    assert client.calls[1]["temperature"] == 0.0


def test_persona_converter_reports_model_when_json_keeps_failing():
    client = _StubClient(
        {},
        errors=(
            ResponseParseError("模型返回的 JSON 无法解析"),
            ResponseParseError("模型返回的 JSON 无法解析"),
        ),
    )
    converter = PersonaConverter(client)

    async def scenario():
        await converter.convert(model_key="gemini", source_text="我是一个机娘")

    with pytest.raises(PersonaError) as info:
        asyncio.run(scenario())
    assert "gemini" in str(info.value)
    assert "JSON" in str(info.value)
    assert len(client.calls) == 2


def test_load_jianer_ai_presets_and_resolution(tmp_path):
    preset_dir = tmp_path / "prerequisites"
    preset_dir.mkdir()
    (preset_dir / "current.json").write_text(
        json.dumps(
            {
                "XingYu": {
                    "name": "机娘",
                    "info": "是机娘desu~！",
                    "path": "机娘.txt",
                },
                "Normal": {
                    "name": "做我女朋友",
                    "info": "老公～你来啦！",
                    "path": "Normal.txt",
                },
                "Broken": {"name": "缺失模板", "path": "missing.txt"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (preset_dir / "机娘.txt").write_text("你是机娘，说话简短。", encoding="utf-8")
    (preset_dir / "Normal.txt").write_text("你是我女朋友。", encoding="utf-8")

    catalog, errors = load_jianer_ai_presets(tmp_path)
    assert set(catalog) == {"XingYu", "Normal"}
    assert errors["Broken"] == "模板读取失败: FileNotFoundError"
    assert resolve_preset(catalog, "机娘").key == "XingYu"
    assert resolve_preset(catalog, "normal").key == "Normal"
    assert resolve_preset(catalog, "机").key == "XingYu"
    assert resolve_preset(catalog, "").key == "Normal"
    assert resolve_preset(catalog, "不存在") is None
    assert default_preset(catalog).key == "Normal"
    assert default_preset({}) is None
