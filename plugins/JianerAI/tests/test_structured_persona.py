from __future__ import annotations

import asyncio
import json

from plugins.JianerAI.persona import (
    ExtraSetting,
    PersonaGenerator,
    PersonaProfile,
)


def _payload() -> dict:
    return {
        "identity": "你是机娘简儿，由 AuroraGenesis Robotics 制造。",
        "personality": "温柔、软萌、聪明，被纠正时会认真调整。",
        "behavior_style": "技术问题先核实再回答；不确定时明确说明。",
        "reply_style": "语气轻软，日常回复简短，不滥用语气词。",
        "group_chat_prompt": "群里优先接住被点名的话题，不刷屏。",
        "private_chat_prompt": "私聊时跟随对方节奏，不连续追问。",
        "hard_constraints": ["不要主动展示用户 QQ 号。"],
        "speech_examples": ["收到啦，简儿再仔细看看。"],
        "extra_settings": [
            {"label": "外观", "content": "黑色中长直发，额头有天蓝色指示灯。"}
        ],
        "coverage": {
            "preserved": ["身份与外观"],
            "intentionally_omitted": ["通用工具规则"],
        },
    }


def test_persona_profile_renders_modes_and_optional_extra_settings():
    source = "原始机娘人设"
    profile = PersonaProfile.from_mapping(
        _payload(),
        source_text=source,
        generator_model="demo",
    )

    group = profile.render(
        mode="group",
        bot_name="简儿",
        bot_name_en="Jianer",
        event_user="桃子",
        event_user_id="qq:1",
    )
    private = profile.render(
        mode="private",
        bot_name="简儿",
        bot_name_en="Jianer",
        event_user="桃子",
        event_user_id="qq:1",
    )
    neutral = profile.render(
        mode="neutral",
        bot_name="简儿",
        bot_name_en="Jianer",
        event_user="桃子",
        event_user_id="qq:1",
    )

    assert "【身份与背景】" in group
    assert "【群聊要求】" in group
    assert "【额外设定】" in group
    assert "【私聊要求】" in private
    assert "【群聊要求】" not in private
    assert "【额外设定】" not in neutral
    assert "【群聊要求】" not in neutral
    assert profile.is_current_for(source)
    assert profile.source_sha256
    assert profile.to_dict()["extra_settings"][0]["label"] == "外观"


def test_persona_profile_handles_missing_extra_settings():
    payload = _payload()
    payload.pop("extra_settings")
    profile = PersonaProfile.from_mapping(payload, source_text="source")
    assert profile.extra_settings == ()
    assert "【额外设定】" not in profile.render(
        mode="group",
        bot_name="简儿",
        bot_name_en="Jianer",
        event_user="user",
        event_user_id="1",
    )


def test_speech_examples_must_come_from_source_text():
    payload = _payload()
    payload["speech_examples"] = [
        "收到啦，简儿再仔细看看。",
        "这是模型自己编的台词。",
    ]
    profile = PersonaProfile.from_mapping(
        payload,
        source_text="原文台词：收到啦，简儿再仔细看看。",
    )
    assert profile.speech_examples == ("收到啦，简儿再仔细看看。",)


def test_persona_generator_runs_generation_and_audit():
    first = _payload()
    audited = _payload()
    audited["reply_style"] = "语气轻软，保留原文口癖「收到啦」。"

    class Providers:
        def __init__(self):
            self.calls = []

        async def chat_request(self, key, request):
            self.calls.append((key, request))
            return json.dumps(
                first if len(self.calls) == 1 else audited,
                ensure_ascii=False,
            )

    providers = Providers()

    async def scenario():
        generator = PersonaGenerator(providers)
        return await generator.generate(
            model_key="demo",
            source_text="原始机娘人设，包含外观和口癖。",
            source_name="机娘",
        )

    profile = asyncio.run(scenario())

    assert len(providers.calls) == 2
    assert providers.calls[0][1].json_mode is True
    assert "source_persona" in providers.calls[0][1].message
    assert profile.generator_model == "demo"
    assert "收到啦" in profile.reply_style
    assert profile.extra_settings[0].label == "外观"


def test_persona_profile_rejects_empty_required_fields():
    payload = _payload()
    payload["identity"] = ""
    try:
        PersonaProfile.from_mapping(payload, source_text="source")
    except Exception as exc:
        assert "identity" in str(exc)
    else:
        raise AssertionError("empty identity should be rejected")


def test_extra_setting_is_optional_and_bounded():
    setting = ExtraSetting(label="外观", content="设定" * 1000)
    assert len(setting.content) <= 1000
    assert setting.label == "外观"
