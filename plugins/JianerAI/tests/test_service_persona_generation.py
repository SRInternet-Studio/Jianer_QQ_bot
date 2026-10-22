from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from plugins.JianerAI.persona import PersonaProfile
from plugins.JianerAI.presets import PresetStore
from plugins.JianerAI.service import JianerAIService


def _store(tmp_path: Path) -> PresetStore:
    (tmp_path / "current.json").write_text("{}", encoding="utf-8")
    return PresetStore(tmp_path / "current.json", tmp_path)


def _service(store: PresetStore) -> tuple[JianerAIService, list[str]]:
    service = object.__new__(JianerAIService)
    sent: list[str] = []
    service.options = SimpleNamespace(reminder="~")
    service.presets = store
    service.memory = SimpleNamespace(ensure_persona_partition=lambda _key: None)
    service._is_admin = lambda _event: True
    service._send_confused = lambda *_args: None
    service._log_exception = lambda _message: None
    service._send_text = lambda _event, _actions, text, reply=False: _send(
        sent,
        text,
    )
    return service, sent


async def _send(sent: list[str], text: str) -> None:
    sent.append(text)


def _profile(source: str) -> PersonaProfile:
    return PersonaProfile.from_mapping(
        {
            "identity": "身份",
            "personality": "性格",
            "behavior_style": "行为",
            "reply_style": "表达",
            "group_chat_prompt": "群聊要求",
            "private_chat_prompt": "私聊要求",
            "extra_settings": [{"label": "外观", "content": "外观"}],
        },
        source_text=source,
    )


def test_add_persona_saves_raw_before_structured_json(tmp_path: Path):
    store = _store(tmp_path)
    service, sent = _service(store)

    async def generate(_event, _actions, preset):
        assert (tmp_path / preset.path).read_text(encoding="utf-8") == "原始人设"
        return _profile(preset.template)

    service._generate_persona_profile = generate

    async def scenario():
        return await service.add_persona(
            SimpleNamespace(user_id="1"),
            SimpleNamespace(),
            "新角色 简介 : 原始人设",
        )

    assert asyncio.run(scenario()) is True
    preset = next(iter(store.list_presets()))
    assert preset.template == "原始人设"
    assert preset.persona_status == "structured"
    assert (tmp_path / "personas" / f"{preset.key}.json").is_file()
    assert any("结构化人格已生成" in item for item in sent)


def test_add_persona_keeps_raw_when_generation_fails(tmp_path: Path):
    store = _store(tmp_path)
    service, sent = _service(store)

    async def generate(_event, _actions, _preset):
        return None

    service._generate_persona_profile = generate

    async def scenario():
        return await service.add_persona(
            SimpleNamespace(user_id="1"),
            SimpleNamespace(),
            "失败角色 简介 : 必须保留的原始人设",
        )

    assert asyncio.run(scenario()) is True
    preset = next(iter(store.list_presets()))
    assert preset.template == "必须保留的原始人设"
    assert preset.persona_status == "legacy"
    assert not (tmp_path / "personas" / f"{preset.key}.json").exists()
    assert any("原始人设已保留" in item for item in sent)


def test_add_persona_metadata_does_not_contain_persona(tmp_path: Path):
    store = _store(tmp_path)
    service, _ = _service(store)

    async def generate(_event, _actions, preset):
        return _profile(preset.template)

    service._generate_persona_profile = generate

    async def scenario():
        return await service.add_persona(
            SimpleNamespace(user_id="1"),
            SimpleNamespace(),
            "角色 简介 : 人设正文",
        )

    asyncio.run(scenario())
    raw = json.loads((tmp_path / "current.json").read_text(encoding="utf-8"))
    preset_key = next(iter(raw))
    assert "persona" not in raw[preset_key]


def test_service_renders_structured_persona_in_group_mode(tmp_path: Path):
    store = _store(tmp_path)
    service, _ = _service(store)
    preset = store.upsert(
        key="Role",
        name="角色",
        info="简介",
        template="原始人设",
    )
    store.write_persona("Role", _profile(preset.template))
    service.options = SimpleNamespace(
        reminder="~",
        bot_name="简儿",
        bot_name_en="Jianer",
    )
    rendered = service._render_persona(
        "Role",
        SimpleNamespace(group_id="g", sender={"nickname": "用户"}),
        "qq:1",
    )
    assert "【身份与背景】" in rendered
    assert "【群聊要求】" in rendered
    assert "【额外设定】" in rendered
