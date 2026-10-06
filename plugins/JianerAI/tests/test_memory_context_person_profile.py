from __future__ import annotations

import asyncio
from types import SimpleNamespace

from plugins.JianerAI.memory_context import MemoryContextProvider
from plugins.JianerAI.memorix_adapter import JianerMemoryAdapter


class _Memory:
    revision = "test-revision"

    def __init__(self):
        self.profile_lookups = []
        self.episode_lookups = []

    def query_memories(self, **kwargs):
        return []

    def get_person_profile_context(self, canonical_user_id):
        self.profile_lookups.append(canonical_user_id)
        return "【人物画像-内部参考】\n喜欢讨论图像生成。"

    def query_conversation_episodes(self, **kwargs):
        self.episode_lookups.append(kwargs)
        return []


def test_profile_follows_canonical_speaker_across_chats_but_episodes_do_not():
    memory = _Memory()
    provider = MemoryContextProvider(memory)
    snapshots = []

    for conversation_id in ("group-a", "group-b"):
        snapshots.append(
            provider.build(
                conversation_scope={
                    "protocol": "onebot",
                    "self_id": "bot-1",
                    "conversation_kind": "group",
                    "conversation_id": conversation_id,
                },
                current_speaker="qq:123",
                query="图像生成",
                preset="default",
            )
        )

    assert memory.profile_lookups == ["qq:123", "qq:123"]
    assert all("喜欢讨论图像生成" in item.person_profile for item in snapshots)
    assert "跨会话人物画像" in snapshots[0].as_prompt()
    assert snapshots[0].to_subagent_snapshot()["person_profile"] == snapshots[0].person_profile
    assert [item["conversation_id"] for item in memory.episode_lookups] == [
        "group-a",
        "group-b",
    ]


def test_adapter_reads_cached_profile_and_applies_manual_override(tmp_path):
    calls = []

    class MetadataStore:
        def get_latest_person_profile_snapshot(self, person_id):
            calls.append(("snapshot", person_id))
            return {"profile_text": "自动画像", "profile_version": 2}

    class ProfileService:
        metadata_store = MetadataStore()

        def _cfg(self, key, default=None):
            return default

        def _apply_manual_override(self, person_id, payload):
            calls.append(("override", person_id))
            return {**payload, "profile_text": "人工校正画像"}

        @staticmethod
        def format_persona_profile_block(profile):
            return f"【人物画像-内部参考】\n{profile['profile_text']}"

    kernel = SimpleNamespace(person_profile_service=ProfileService())
    adapter = JianerMemoryAdapter(data_dir=tmp_path, kernel=kernel)
    adapter._call_kernel = lambda operation: asyncio.run(operation(kernel))

    result = adapter.get_person_profile_context("qq:123")

    assert result == "【人物画像-内部参考】\n人工校正画像"
    assert calls == [("snapshot", "qq:123"), ("override", "qq:123")]


def test_profile_lookup_is_skipped_when_person_context_is_disabled():
    memory = _Memory()
    snapshot = MemoryContextProvider(memory).build(
        conversation_scope={"protocol": "onebot", "self_id": "bot-1"},
        current_speaker="qq:123",
        query="hello",
        preset="default",
        include_person=False,
    )

    assert memory.profile_lookups == []
    assert not snapshot.person_profile


def test_profile_survives_memory_context_budget_trimming():
    memory = _Memory()
    memory.get_person_profile_context = lambda _canonical_user_id: "画像" * 2000
    snapshot = MemoryContextProvider(memory, max_characters=1000).build(
        conversation_scope={"protocol": "onebot", "self_id": "bot-1"},
        current_speaker="qq:123",
        query="hello",
        preset="default",
    )

    assert snapshot.person_profile
    assert "画像" in snapshot.as_prompt()
