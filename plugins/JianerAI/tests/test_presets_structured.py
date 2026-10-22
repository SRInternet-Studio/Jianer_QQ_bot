from __future__ import annotations

import json
from pathlib import Path

from plugins.JianerAI.persona import PersonaProfile
from plugins.JianerAI.presets import PresetStore


def _profile(source: str) -> PersonaProfile:
    return PersonaProfile.from_mapping(
        {
            "identity": "结构化身份",
            "personality": "结构化性格",
            "behavior_style": "结构化行为",
            "reply_style": "结构化表达",
            "group_chat_prompt": "群聊补充",
            "private_chat_prompt": "私聊补充",
            "extra_settings": [
                {"label": "外观", "content": "结构化外观"}
            ],
        },
        source_text=source,
    )


def _store(tmp_path: Path) -> PresetStore:
    (tmp_path / "role.txt").write_text("原始人设", encoding="utf-8")
    (tmp_path / "current.json").write_text(
        json.dumps(
            {
                "Role": {
                    "name": "测试角色",
                    "info": "简介",
                    "path": "role.txt",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return PresetStore(tmp_path / "current.json", tmp_path)


def test_structured_persona_is_loaded_from_separate_json(tmp_path: Path):
    store = _store(tmp_path)
    preset = store.get("Role")
    assert preset.persona_status == "legacy"

    store.write_persona("Role", _profile(preset.template))
    store.reload()
    preset = store.get("Role")

    assert preset.persona_status == "structured"
    assert (tmp_path / "personas" / "Role.json").is_file()
    metadata = json.loads((tmp_path / "current.json").read_text(encoding="utf-8"))
    assert "persona" not in metadata["Role"]
    rendered = store.render(
        "Role",
        mode="group",
        bot_name="简儿",
        bot_name_en="Jianer",
        event_user="用户",
        event_user_id="1",
    )
    assert "【身份与背景】" in rendered
    assert "【额外设定】" in rendered


def test_source_change_makes_structured_persona_stale_and_falls_back(tmp_path: Path):
    store = _store(tmp_path)
    preset = store.get("Role")
    store.write_persona("Role", _profile(preset.template))
    (tmp_path / "role.txt").write_text("原始人设已修改", encoding="utf-8")
    store.reload()
    preset = store.get("Role")

    assert preset.persona_status == "stale"
    rendered = store.render(
        "Role",
        mode="group",
        bot_name="简儿",
        bot_name_en="Jianer",
        event_user="用户",
        event_user_id="1",
    )
    assert rendered == "原始人设已修改"


def test_write_persona_rejects_a_profile_for_different_source(tmp_path: Path):
    store = _store(tmp_path)
    try:
        store.write_persona("Role", _profile("另一份人设"))
    except Exception as exc:
        assert "source template" in str(exc)
    else:
        raise AssertionError("stale structured persona should be rejected")


def test_invalid_persona_json_falls_back_without_breaking_store(tmp_path: Path):
    store = _store(tmp_path)
    persona_dir = tmp_path / "personas"
    persona_dir.mkdir()
    (persona_dir / "Role.json").write_text("{broken", encoding="utf-8")
    store.reload()
    preset = store.get("Role")

    assert preset.persona_status == "legacy"
    assert store.persona_error("Role")


def test_persona_json_without_source_hash_is_rejected(tmp_path: Path):
    store = _store(tmp_path)
    persona_dir = tmp_path / "personas"
    persona_dir.mkdir()
    payload = _profile("原始人设").to_dict()
    payload.pop("source_sha256")
    (persona_dir / "Role.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    store.reload()

    assert store.get("Role").persona_status == "legacy"
    assert "source_sha256" in store.persona_error("Role")


def test_delete_removes_separate_persona_file(tmp_path: Path):
    store = _store(tmp_path)
    preset = store.get("Role")
    store.write_persona("Role", _profile(preset.template))
    assert store.delete("Role")
    assert not (tmp_path / "personas" / "Role.json").exists()
