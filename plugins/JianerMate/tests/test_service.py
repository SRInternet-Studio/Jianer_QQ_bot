import asyncio
import json
import random
import time
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from jianer import common
from jianer import segments
from jianer.adapters import (
    Capability,
    MediaResolution,
    ReferenceResolution,
    ResolutionStatus,
)

from plugins.JianerMate.config import MemeOptions, RuntimeOptions
from plugins.JianerMate.memes import MemeLibrary
from plugins.JianerMate.moderation import ModerationDecision
from plugins.JianerMate.persona import Persona
from plugins.JianerMate.memory_review import MemoryAction
from plugins.JianerMate.planner import PlannerDecision
from plugins.JianerMate.providers import (
    OPENAI_CHAT_COMPLETIONS,
    ModelEntry,
    ProviderError,
)
from plugins.JianerMate.replyer import ReplyPlan
from plugins.JianerMate.service import ConversationRef, JianerMateService
from plugins.JianerMate.store import MateStore


def _png(color="red", size=(120, 120)):
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class _FakeResult:
    def __init__(self, message_id):
        self.data = SimpleNamespace(message_id=message_id)


class _FakeActions:
    protocol = "milky"
    capabilities = frozenset(
        {
            Capability.SEND_REPLY,
            Capability.SEND_IMAGE,
            Capability.RESOLVE_MEDIA,
            Capability.RESOLVE_REFERENCE,
        }
    )

    def __init__(self, media_bytes=None):
        self.sent = []
        self.media_bytes = media_bytes or _png()

    async def send(self, message, group_id=None, user_id=None):
        self.sent.append(
            {"message": message, "group_id": group_id, "user_id": user_id}
        )
        return _FakeResult(f"sent-{len(self.sent)}")

    async def resolve_media(self, request, *, conversation=None, policy=None):
        return MediaResolution(
            status=ResolutionStatus.OK,
            error_code=None,
            mime="image/png",
            size=len(self.media_bytes),
            data=self.media_bytes,
            source="test",
        )

    async def resolve_reference(
        self, message_id, *, conversation=None, timeout_seconds=10.0
    ):
        return ReferenceResolution(
            status=ResolutionStatus.OK,
            error_code=None,
            message_id=str(message_id),
            conversation=conversation,
            sender_id="5",
            sent_at=1,
            segments=(segments.Text("被引用的内容"),),
        )


def _event(
    *,
    group_id="222",
    user_id="5",
    self_id="10001",
    text="你好",
    at=False,
    message_id="m1",
    images=(),
    reply=None,
    role=None,
):
    message = common.Message()
    if reply is not None:
        message.add(segments.Reply(str(reply)))
    if at:
        message.add(segments.At(self_id))
    if text:
        message.add(segments.Text(text))
    for _ in images:
        message.add(segments.Image("file:///tmp/example.png"))
    return SimpleNamespace(
        protocol="milky",
        self_id=self_id,
        user_id=user_id,
        group_id=group_id,
        message=message,
        message_id=message_id,
        sender=SimpleNamespace(card=None, nickname="群友", role=role),
        is_mentioned=at,
    )


class _StubClient:
    def __init__(self, entries=None):
        self._entries = dict(
            entries
            or {
                "test": ModelEntry(
                    key="test",
                    friendly_name="Test",
                    response_type=OPENAI_CHAT_COMPLETIONS,
                    model="test-model",
                    api_key="sk-test",
                    base_url="https://api.example.com/v1",
                )
            }
        )
        self.closed = False

    @property
    def models(self):
        return dict(self._entries)

    def has_model(self, key):
        entry = self._entries.get(str(key or "").strip())
        return bool(entry and entry.supported)

    def reload(self, entries):
        self._entries = dict(entries)

    async def close(self):
        self.closed = True


class _StubPlanner:
    def __init__(self, decision=None):
        self.decision = decision or PlannerDecision(speak=False, reason="silent")
        self.calls = []

    async def decide(self, **kwargs):
        self.calls.append(kwargs)
        return self.decision


class _StubReplyer:
    def __init__(self, plan=None):
        self.plan = plan or ReplyPlan(messages=("在的",))
        self.calls = []

    async def compose(self, **kwargs):
        self.calls.append(kwargs)
        return self.plan


class _StubModerator:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.calls = []

    async def review_many(self, texts, *, context=""):
        self.calls.append({"texts": list(texts), "context": context})
        return ModerationDecision(
            allowed=self.allowed,
            reason="" if self.allowed else "blocked",
        )


class _FlakyReplyer:
    """Fails when media input is attached, then succeeds without it."""

    def __init__(self, plan=None):
        self.plan = plan or ReplyPlan(messages=("只发文字",))
        self.calls = []

    async def compose(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("images") or kwargs.get("candidates"):
            raise ProviderError("model does not accept images")
        return self.plan


class _StubConverter:
    def __init__(self, persona):
        self.persona = persona
        self.calls = []

    async def convert(self, **kwargs):
        self.calls.append(kwargs)
        return self.persona


class _SilentLogger:
    def info(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None

    def exception(self, *args, **kwargs):
        return None

    def log(self, *args, **kwargs):
        return None


class _FailingActions(_FakeActions):
    def __init__(self, media_bytes=None):
        super().__init__(media_bytes)
        self.attempts = 0
        self.images = 0

    async def send(self, message, group_id=None, user_id=None):
        self.attempts += 1
        if any(isinstance(segment, segments.Image) for segment in message):
            self.images += 1
        raise RuntimeError("adapter rejected the message")


def _sent_text(item):
    return "".join(
        str(getattr(segment, "text", ""))
        for segment in item["message"]
        if isinstance(segment, segments.Text)
    )


def _runtime(tmp_path, **overrides):
    others = {
        "jianer_mate_data_dir": str(tmp_path / "data"),
        "jianer_mate_ai_config_dir": str(tmp_path / "aiconfig"),
        "jianer_mate_planner_model": "test",
        "jianer_mate_replyer_model": "test",
        "jianer_mate_debounce_seconds": 0,
        "jianer_mate_reply_delay_ms": [0, 0],
        "jianer_mate_typo_probability": 0,
        "jianer_mate_kaomoji_probability": 0,
        "jianer_mate_talk_value_default": 1.0,
        "jianer_mate_min_interval_seconds": 0,
        "jianer_mate_quiet_hours_default": "关闭",
        "jianer_mate_config_path": str(tmp_path / "mate-config.json"),
        "jianer_mate_memory_backend": "legacy",
    }
    others.update(overrides)
    config = SimpleNamespace(others=others, black_list=[])
    return {
        "config": config,
        "reminder": "~",
        "root_users": ["1"],
        "super_users": [],
        "manage_users": [],
        "admins": ["1"],
    }


async def _noop_sleep(_delay):
    await asyncio.sleep(0)


def _service(
    tmp_path,
    *,
    planner=None,
    replyer=None,
    moderator=None,
    store=None,
    library=None,
    speech=None,
    **overrides,
):
    runtime = _runtime(tmp_path, **overrides)
    options = RuntimeOptions.from_runtime(runtime)
    client = _StubClient()
    return JianerMateService(
        options,
        runtime=runtime,
        store=store,
        memes=library,
        client=client,
        planner=planner or _StubPlanner(),
        replyer=replyer or _StubReplyer(),
        moderator=moderator,
        clock=time.time,
        rng=random.Random(7),
        sleep=_noop_sleep,
        logger=_SilentLogger(),
        speech=speech,
    )


def _activate(service):
    asyncio.run(service.start_after_activation(SimpleNamespace(plugins={})))
    assert service.active is True


def _enable_group(service, key="222"):
    conversation = service.store.ensure_conversation(
        "milky", "10001", "group", key, now=1
    )
    service.store.update_conversation(conversation.ref, enabled=True, now=1)
    return service.store.get_conversation_by_ref(conversation.ref)


def test_passive_group_mention_and_private_reply(tmp_path):
    service = _service(tmp_path)
    _activate(service)

    actions = _FakeActions()
    event = _event(at=True, text="在吗")
    assert asyncio.run(service.handle_fallback(event, actions)) is True
    assert len(actions.sent) == 1
    sent_text = "".join(
        str(getattr(segment, "text", ""))
        for segment in actions.sent[0]["message"]
        if isinstance(segment, segments.Text)
    )
    assert "在的" in sent_text
    assert actions.sent[0]["group_id"] == "222"
    assert service.replyer.calls[0]["mode"] == "passive"

    private_actions = _FakeActions()
    private_event = _event(group_id=None, at=False, text="早上好")
    assert asyncio.run(service.handle_fallback(private_event, private_actions)) is True
    assert len(private_actions.sent) == 1
    assert private_actions.sent[0]["user_id"] == "5"

    ignored = _FakeActions()
    plain = _event(at=False, text="随便聊聊")
    assert asyncio.run(service.handle_fallback(plain, ignored)) is False
    assert ignored.sent == []

    conversation = service.store.get_conversation("milky", "10001", "group", "222")
    assert conversation is not None
    directions = [
        record.direction
        for record in service.store.recent_messages(conversation.ref, limit=10)
    ]
    assert "incoming" not in directions
    assert directions.count("outgoing") >= 1
    asyncio.run(service.shutdown())


def test_proactive_flow_speaks_after_debounce(tmp_path):
    planner = _StubPlanner(
        PlannerDecision(
            speak=True,
            reason="话题可接",
            topic="打招呼",
            want_meme=False,
        )
    )
    replyer = _StubReplyer(ReplyPlan(messages=("早上好", "今天人好少")))
    service = _service(tmp_path, planner=planner, replyer=replyer)
    _activate(service)
    conversation = _enable_group(service)

    async def scenario():
        actions = _FakeActions()
        event = _event(at=False, text="早上好呀")
        await service.observe(event, actions)
        for _ in range(20):
            await asyncio.sleep(0.01)
            if actions.sent:
                break
        return actions

    actions = asyncio.run(scenario())
    assert len(actions.sent) == 2
    decisions = service.store.recent_decisions(conversation.ref, limit=5)
    assert decisions and decisions[0].spoke is True
    assert planner.calls and replyer.calls
    asyncio.run(service.shutdown())


def test_proactive_gate_blocks_quiet_hours_cooldown_and_probability(tmp_path):
    planner = _StubPlanner(PlannerDecision(speak=True, reason="想说"))
    replyer = _StubReplyer(ReplyPlan(messages=("嗨",)))

    quiet_service = _service(
        tmp_path / "quiet",
        planner=planner,
        replyer=replyer,
        jianer_mate_quiet_hours_default="00:00-23:59",
    )
    _activate(quiet_service)
    quiet_conversation = _enable_group(quiet_service)
    passed, reason = quiet_service.evaluate_gate(
        quiet_service.store.get_conversation_by_ref(quiet_conversation.ref),
        quiet_service.store.recent_messages(quiet_conversation.ref, limit=5),
        now=time.time(),
    )
    assert passed is False
    assert reason == "quiet_hours"

    cooldown_service = _service(
        tmp_path / "cooldown", planner=planner, replyer=replyer
    )
    _activate(cooldown_service)
    cooldown_conversation = _enable_group(cooldown_service)
    cooldown_service.store.append_message(
        cooldown_conversation.ref,
        direction="outgoing",
        content="刚说过",
        now=int(time.time()),
    )
    cooldown_service.store.append_message(
        cooldown_conversation.ref,
        direction="incoming",
        content="有人接话了",
        now=int(time.time()),
    )
    cooldown_service.store.update_conversation(
        cooldown_conversation.ref, min_interval_seconds=3600
    )
    passed, reason = cooldown_service.evaluate_gate(
        cooldown_service.store.get_conversation_by_ref(cooldown_conversation.ref),
        cooldown_service.store.recent_messages(cooldown_conversation.ref, limit=5),
        now=time.time(),
    )
    assert passed is False
    assert reason == "cooldown"

    random_service = _service(
        tmp_path / "random",
        planner=planner,
        replyer=replyer,
        jianer_mate_talk_value_default=0.0,
    )
    _activate(random_service)
    random_conversation = _enable_group(random_service)
    random_service.store.append_message(
        random_conversation.ref,
        direction="incoming",
        content="有人说了一句",
        now=int(time.time()),
    )
    passed, reason = random_service.evaluate_gate(
        random_service.store.get_conversation_by_ref(random_conversation.ref),
        random_service.store.recent_messages(random_conversation.ref, limit=5),
        now=time.time(),
    )
    assert passed is False
    assert reason == "talk_value"
    asyncio.run(quiet_service.shutdown())
    asyncio.run(cooldown_service.shutdown())
    asyncio.run(random_service.shutdown())


def test_planner_silence_and_moderation_block(tmp_path):
    silent_service = _service(
        tmp_path / "silent",
        planner=_StubPlanner(PlannerDecision(speak=False, reason="没必要插话")),
    )
    _activate(silent_service)
    conversation = _enable_group(silent_service)

    async def silent_scenario():
        actions = _FakeActions()
        await silent_service.observe(_event(at=False, text="哈哈哈"), actions)
        for _ in range(20):
            await asyncio.sleep(0.01)
        return actions

    actions = asyncio.run(silent_scenario())
    assert actions.sent == []
    decisions = silent_service.store.recent_decisions(conversation.ref, limit=3)
    assert decisions and "silence" not in decisions[0].gate_reason
    assert decisions[0].gate_reason == "没必要插话"

    blocked_service = _service(
        tmp_path / "blocked",
        planner=_StubPlanner(PlannerDecision(speak=True, reason="想说")),
        replyer=_StubReplyer(ReplyPlan(messages=("试试",))),
        moderator=_StubModerator(allowed=False),
    )
    _activate(blocked_service)
    _enable_group(blocked_service)

    async def blocked_scenario():
        actions = _FakeActions()
        await blocked_service.observe(_event(at=False, text="来聊聊"), actions)
        for _ in range(20):
            await asyncio.sleep(0.01)
        return actions

    blocked_actions = asyncio.run(blocked_scenario())
    assert blocked_actions.sent == []
    asyncio.run(silent_service.shutdown())
    asyncio.run(blocked_service.shutdown())


def test_meme_delivery_conflict_detection_and_shutdown(tmp_path):
    store = MateStore(tmp_path / "data" / "mate.db")
    library = MemeLibrary(
        store,
        replace(MemeOptions(), max_entries=8),
        directory=tmp_path / "data" / "memes",
    )
    ingested = library.ingest(_png("blue"), source_group="222")
    assert ingested.status == "added"
    service = _service(
        tmp_path,
        store=store,
        library=library,
        planner=_StubPlanner(PlannerDecision(speak=True, reason="配图更自然")),
        replyer=_StubReplyer(
            ReplyPlan(messages=("好耶",), meme_id=ingested.meme_id)
        ),
    )
    service.client = _StubClient()
    _activate(service)
    _enable_group(service)

    async def scenario():
        actions = _FakeActions()
        await service.observe(_event(at=False, text="今天好累"), actions)
        for _ in range(20):
            await asyncio.sleep(0.01)
            if any(
                isinstance(segment, segments.Image)
                for item in actions.sent
                for segment in item["message"]
            ):
                break
        return actions

    actions = asyncio.run(scenario())
    image_segments = [
        segment
        for item in actions.sent
        for segment in item["message"]
        if isinstance(segment, segments.Image)
    ]
    assert image_segments
    assert image_segments[0].file.startswith("file://")
    assert store.get_meme(ingested.meme_id).use_count == 1
    asyncio.run(service.shutdown())
    assert service.client.closed is True

    conflicted = _service(tmp_path / "conflict")
    asyncio.run(
        conflicted.start_after_activation(
            SimpleNamespace(plugins={"jianerbot-plugin-jianer-ai": object()})
        )
    )
    assert conflicted.active is False
    assert conflicted.conflict is True
    actions = _FakeActions()
    asyncio.run(conflicted.observe(_event(at=True), actions))
    assert actions.sent == []
    assert asyncio.run(conflicted.handle_fallback(_event(at=True), actions)) is False
    asyncio.run(conflicted.shutdown())


def test_replyer_degrades_to_text_only_when_images_fail(tmp_path):
    store = MateStore(tmp_path / "data" / "mate.db")
    library = MemeLibrary(
        store,
        replace(MemeOptions(), max_entries=8),
        directory=tmp_path / "data" / "memes",
    )
    assert library.ingest(_png("green")).status == "added"
    replyer = _FlakyReplyer()
    service = _service(
        tmp_path,
        store=store,
        library=library,
        planner=_StubPlanner(PlannerDecision(speak=True, reason="想说")),
        replyer=replyer,
    )
    _activate(service)
    _enable_group(service)

    async def scenario():
        actions = _FakeActions()
        await service.observe(_event(at=False, text="说说看"), actions)
        for _ in range(20):
            await asyncio.sleep(0.01)
            if actions.sent:
                break
        return actions

    actions = asyncio.run(scenario())
    assert actions.sent
    assert len(replyer.calls) == 2
    assert replyer.calls[0]["candidates"]
    assert replyer.calls[1]["candidates"] == ()
    assert service._notice_state["vision_failures"] == 1
    asyncio.run(service.shutdown())


def test_typo_correction_quotes_the_typo_message(tmp_path):
    service = _service(
        tmp_path,
        replyer=_StubReplyer(ReplyPlan(messages=("你好",))),
        jianer_mate_typo_probability=1.0,
        jianer_mate_typo_correction=True,
    )
    _activate(service)
    conversation = service.store.ensure_conversation(
        "milky", "10001", "group", "222", now=1
    )
    actions = _FakeActions()
    sent = asyncio.run(
        service._deliver(
            conversation,
            _event(at=True, text="在吗"),
            actions,
            ReplyPlan(messages=("你好",)),
        )
    )
    assert sent == 1
    assert len(actions.sent) == 2
    correction = list(actions.sent[1]["message"])
    assert any(isinstance(segment, segments.Reply) for segment in correction)
    typo_text = "".join(
        str(getattr(segment, "text", ""))
        for segment in actions.sent[0]["message"]
        if isinstance(segment, segments.Text)
    )
    assert typo_text != "你好"
    asyncio.run(service.shutdown())


def test_master_switch_stops_message_handling(tmp_path):
    service = _service(tmp_path, jianer_mate_enabled=False)
    _activate(service)
    actions = _FakeActions()
    event = _event(at=True, text="在吗")
    asyncio.run(service.observe(event, actions))
    assert asyncio.run(service.handle_fallback(event, actions)) is False
    assert actions.sent == []
    assert service.store.list_conversations() == ()
    asyncio.run(service.shutdown())


def test_persona_conversion_persists_and_reset_restores_config(tmp_path):
    replyer = _StubReplyer(ReplyPlan(messages=("在的",)))
    service = _service(tmp_path, replyer=replyer)
    _activate(service)
    converted = Persona.from_value(
        {"name": "机娘", "personality": "你是一台机娘"}
    )
    converter = _StubConverter(converted)
    service.persona_converter = converter

    actions = _FakeActions()
    admin = _event(user_id="1", at=False, text="随便")
    assert asyncio.run(
        service.handle_persona_command(admin, actions, "转换", "我是一个机娘")
    ) is True
    assert converter.calls[0]["source_text"] == "我是一个机娘"
    assert service.persona_override is not None
    assert "机娘" in _sent_text(actions.sent[-1])

    # Converting from a quoted message uses the referenced text.
    quoted = _FakeActions()
    asyncio.run(
        service.handle_persona_command(
            _event(user_id="1", at=False, reply="m0"), quoted, "转换", ""
        )
    )
    assert converter.calls[-1]["source_text"] == "被引用的内容"

    # The override survives a fresh service on the same database.
    reloaded = _service(tmp_path, replyer=_StubReplyer())
    assert reloaded.persona_override is not None
    assert reloaded.persona_override.name == "机娘"
    assert "转换结果" in reloaded._status_text(
        ConversationRef("milky", "10001", "group", "222")
    )

    group_actions = _FakeActions()
    asyncio.run(
        service.handle_fallback(
            _event(at=True, text="在吗"), group_actions
        )
    )
    assert replyer.calls[-1]["persona"].name == "机娘"
    assert replyer.calls[-1]["persona_mode"] == "group"

    private_actions = _FakeActions()
    asyncio.run(
        service.handle_fallback(
            _event(group_id=None, at=False, text="早上好"), private_actions
        )
    )
    assert replyer.calls[-1]["persona_mode"] == "private"

    reset_actions = _FakeActions()
    asyncio.run(
        service.handle_persona_command(admin, reset_actions, "重置", "")
    )
    assert service.persona_override is None
    assert service.store.get_state("persona_override") == ""
    asyncio.run(
        service.handle_fallback(_event(at=True, text="在吗"), _FakeActions())
    )
    assert replyer.calls[-1]["persona"].name == service.options.persona.name
    asyncio.run(reloaded.shutdown())
    asyncio.run(service.shutdown())


def test_persona_import_reads_old_presets_and_injects_multiple_style(tmp_path):
    preset_dir = tmp_path / "prerequisites"
    preset_dir.mkdir(parents=True)
    (preset_dir / "current.json").write_text(
        '{"XingYu": {"name": "机娘", "info": "desu", "path": "机娘.txt"}}',
        encoding="utf-8",
    )
    (preset_dir / "机娘.txt").write_text(
        "你是机娘，说话简短。", encoding="utf-8"
    )
    planner = _StubPlanner(
        PlannerDecision(speak=True, reason="接话", topic="闲聊")
    )
    replyer = _StubReplyer(ReplyPlan(messages=("好耶",)))
    service = _service(tmp_path, planner=planner, replyer=replyer)
    service.options = replace(service.options, project_root=tmp_path)
    _activate(service)
    _enable_group(service)
    converted = replace(
        Persona.from_value({"name": "机娘", "personality": "你是一台机娘"}),
        multiple_reply_styles=("用1-2个字回复",),
        multiple_probability=1.0,
    )
    converter = _StubConverter(converted)
    service.persona_converter = converter

    actions = _FakeActions()
    asyncio.run(
        service.handle_persona_command(
            _event(user_id="1", at=False), actions, "导入", "机娘"
        )
    )
    assert converter.calls[0]["source_name"] == "机娘"
    assert converter.calls[0]["source_text"] == "你是机娘，说话简短。"

    async def scenario():
        group_actions = _FakeActions()
        await service.observe(_event(at=False, text="在聊什么"), group_actions)
        for _ in range(20):
            await asyncio.sleep(0.01)
            if group_actions.sent:
                break
        return group_actions

    group_actions = asyncio.run(scenario())
    assert group_actions.sent
    assert planner.calls[-1]["persona"].name == "机娘"
    assert replyer.calls[-1]["style_override"] == "用1-2个字回复"
    assert planner.calls[-1]["style_override"] == "用1-2个字回复"
    asyncio.run(service.shutdown())


def test_conversation_prompt_works_in_private_chat(tmp_path):
    service = _service(tmp_path)
    _activate(service)
    actions = _FakeActions()
    private = _event(group_id=None, user_id="1", at=False, text="设置")
    assert asyncio.run(
        service.set_conversation_prompt(private, actions, "多用短句")
    ) is True
    assert "本会话" in _sent_text(actions.sent[-1])
    conversation = service.store.get_conversation(
        "milky", "10001", "private", "1"
    )
    assert conversation is not None
    assert conversation.custom_prompt == "多用短句"

    outsider = _event(group_id=None, user_id="9", at=False, text="设置")
    denied = _FakeActions()
    asyncio.run(service.set_conversation_prompt(outsider, denied, "改一下"))
    assert "机器人管理员" in _sent_text(denied.sent[-1])
    asyncio.run(service.shutdown())


def test_name_mention_bypasses_talk_value_roll(tmp_path):
    service = _service(tmp_path, jianer_mate_talk_value_default=0.0)
    _activate(service)
    conversation = _enable_group(service)
    service.store.append_message(
        conversation.ref,
        direction="incoming",
        content="简儿在吗，帮我看看这个",
        sender_id="5",
        now=int(time.time()),
    )
    passed, reason = service.evaluate_gate(
        service.store.get_conversation_by_ref(conversation.ref),
        service.store.recent_messages(conversation.ref, limit=5),
        now=time.time(),
    )
    assert passed is True
    assert reason == "ok"

    disabled = _service(
        tmp_path / "disabled",
        jianer_mate_talk_value_default=0.0,
        jianer_mate_mentioned_reply=False,
    )
    _activate(disabled)
    disabled_conversation = _enable_group(disabled)
    disabled.store.append_message(
        disabled_conversation.ref,
        direction="incoming",
        content="简儿在吗",
        sender_id="5",
        now=int(time.time()),
    )
    passed, reason = disabled.evaluate_gate(
        disabled.store.get_conversation_by_ref(disabled_conversation.ref),
        disabled.store.recent_messages(disabled_conversation.ref, limit=5),
        now=time.time(),
    )
    assert passed is False
    assert reason == "talk_value"
    asyncio.run(service.shutdown())
    asyncio.run(disabled.shutdown())


def test_reply_at_mentions_the_speaker_only_when_enabled(tmp_path):
    replyer = _StubReplyer(ReplyPlan(messages=("在的",)))
    service = _service(
        tmp_path, replyer=replyer, jianer_mate_reply_at=True
    )
    _activate(service)
    actions = _FakeActions()
    asyncio.run(service.handle_fallback(_event(at=True, text="在吗"), actions))
    first = list(actions.sent[0]["message"])
    assert any(
        isinstance(segment, segments.At) and str(segment.qq) == "5"
        for segment in first
    )

    quiet_service = _service(
        tmp_path / "quiet", replyer=_StubReplyer(ReplyPlan(messages=("在的",)))
    )
    _activate(quiet_service)
    quiet_actions = _FakeActions()
    asyncio.run(
        quiet_service.handle_fallback(
            _event(at=True, text="在吗"), quiet_actions
        )
    )
    assert not any(
        isinstance(segment, segments.At)
        for segment in quiet_actions.sent[0]["message"]
    )
    asyncio.run(service.shutdown())
    asyncio.run(quiet_service.shutdown())


def test_failed_sends_are_not_counted_and_skip_correction_and_meme(tmp_path):
    service = _service(
        tmp_path,
        replyer=_StubReplyer(ReplyPlan(messages=("你好", "在的"), meme_id=42)),
        jianer_mate_typo_probability=1.0,
        jianer_mate_typo_correction=True,
    )
    _activate(service)
    conversation = service.store.ensure_conversation(
        "milky", "10001", "group", "222", now=1
    )
    actions = _FailingActions()

    delivered = asyncio.run(
        service._deliver(
            conversation,
            _event(at=True, text="在吗"),
            actions,
            ReplyPlan(messages=("你好", "在的"), meme_id=42),
        )
    )
    assert delivered == 0
    assert actions.attempts == 2
    assert actions.images == 0
    assert service.store.recent_messages(conversation.ref, limit=10) == ()
    asyncio.run(service.shutdown())


def test_reload_resets_vision_degradation(tmp_path):
    service = _service(tmp_path)
    _activate(service)
    assert service._replyer_supports_images("test") is True
    service._notice_state["vision_failures"] = 3
    assert service._replyer_supports_images("test") is False
    asyncio.run(service.reload())
    assert "vision_failures" not in service._notice_state
    assert service._replyer_supports_images("test") is True
    asyncio.run(service.shutdown())


def test_meme_view_command_sends_the_stored_image(tmp_path):
    store = MateStore(tmp_path / "data" / "mate.db")
    library = MemeLibrary(
        store,
        replace(MemeOptions(), max_entries=8),
        directory=tmp_path / "data" / "memes",
    )
    ingested = library.ingest(_png("purple"))
    assert ingested.status == "added"
    store.update_meme_tags(ingested.meme_id, "猫,无语")
    service = _service(tmp_path, store=store, library=library)
    _activate(service)

    actions = _FakeActions()
    asyncio.run(
        service.meme_command(
            _event(user_id="1", at=False), actions, "查看", str(ingested.meme_id)
        )
    )
    images = [
        segment
        for item in actions.sent
        for segment in item["message"]
        if isinstance(segment, segments.Image)
    ]
    assert images and images[0].file.startswith("file://")
    assert "猫,无语" in _sent_text(actions.sent[-1])

    denied = _FakeActions()
    asyncio.run(
        service.meme_command(
            _event(user_id="9", at=False), denied, "查看", str(ingested.meme_id)
        )
    )
    assert denied.sent and not any(
        isinstance(segment, segments.Image)
        for segment in denied.sent[0]["message"]
    )
    asyncio.run(service.shutdown())


def test_persona_style_add_and_remove_commands(tmp_path):
    service = _service(tmp_path)
    _activate(service)
    admin = _event(user_id="1", at=False)
    actions = _FakeActions()
    asyncio.run(
        service.handle_persona_command(
            admin, actions, "风格添加", "用1-2个字回复"
        )
    )
    assert service.persona_override is not None
    assert service.persona_override.multiple_reply_styles == ("用1-2个字回复",)

    asyncio.run(
        service.handle_persona_command(
            admin, actions, "风格添加", "用1-2个字回复"
        )
    )
    assert "已经存在" in _sent_text(actions.sent[-1])

    asyncio.run(
        service.handle_persona_command(admin, actions, "风格删除", "1")
    )
    assert service.persona_override.multiple_reply_styles == ()

    asyncio.run(
        service.handle_persona_command(admin, actions, "风格删除", "5")
    )
    assert "超出范围" in _sent_text(actions.sent[-1])

    for index in range(8):
        asyncio.run(
            service.handle_persona_command(
                admin, actions, "风格添加", f"备用风格{index}"
            )
        )
    assert len(service.persona_override.multiple_reply_styles) == 8
    asyncio.run(
        service.handle_persona_command(admin, actions, "风格添加", "第9条")
    )
    assert "最多" in _sent_text(actions.sent[-1])
    asyncio.run(service.shutdown())


def test_decisions_command_lists_recent_reasons(tmp_path):
    planner = _StubPlanner(
        PlannerDecision(speak=False, reason="没必要插话")
    )
    service = _service(tmp_path, planner=planner)
    _activate(service)
    _enable_group(service)

    async def scenario():
        await service.observe(_event(at=False, text="哈哈哈"), _FakeActions())
        for _ in range(20):
            await asyncio.sleep(0.01)

    asyncio.run(scenario())
    actions = _FakeActions()
    asyncio.run(
        service.handle_active_command(
            _event(user_id="1", at=False), actions, "决策", "5"
        )
    )
    text = _sent_text(actions.sent[-1])
    assert "最近的主动发言决策" in text
    assert "没必要插话" in text
    asyncio.run(service.shutdown())


def test_debug_mode_sends_preview_instead_of_real_messages(tmp_path):
    planner = _StubPlanner(
        PlannerDecision(speak=True, reason="话题可接", topic="打招呼")
    )
    replyer = _StubReplyer(ReplyPlan(messages=("早上好", "今天人好少")))
    service = _service(tmp_path, planner=planner, replyer=replyer)
    _activate(service)
    conversation = _enable_group(service)
    service.store.update_conversation(
        conversation.ref, debug_dry_run=True
    )

    async def scenario():
        actions = _FakeActions()
        await service.observe(_event(at=False, text="早上好呀"), actions)
        for _ in range(20):
            await asyncio.sleep(0.01)
            if actions.sent:
                break
        return actions

    actions = asyncio.run(scenario())
    assert len(actions.sent) == 1
    text = _sent_text(actions.sent[0])
    assert "[调试]" in text
    assert "早上好" in text
    decisions = service.store.recent_decisions(conversation.ref, limit=3)
    assert decisions[0].gate_reason == "dry_run"
    assert decisions[0].spoke is False
    asyncio.run(service.shutdown())


def test_planner_silence_accumulates_backoff(tmp_path):
    planner = _StubPlanner(PlannerDecision(speak=False, reason="没话说"))
    service = _service(
        tmp_path,
        planner=planner,
        jianer_mate_backoff_base_seconds=15,
        jianer_mate_backoff_cap_seconds=300,
        jianer_mate_backoff_start_count=2,
    )
    _activate(service)
    conversation = _enable_group(service)
    key = ConversationRef("milky", "10001", "group", "222")

    async def scenario():
        for expected in (1, 2):
            await service.observe(
                _event(at=False, text=f"随便说说{expected}"), _FakeActions()
            )
            for _ in range(40):
                await asyncio.sleep(0.01)
                if service._silence_streak.get(key, 0) >= expected:
                    break

    asyncio.run(scenario())
    assert service._silence_streak.get(key) == 2
    assert key in service._backoff_until
    assert service._backoff_until[key] == pytest.approx(
        service._clock() + 15, abs=5
    )

    # A new message inside the backoff window does not queue another check.
    actions = _FakeActions()
    service._schedule_proactive(
        key,
        _event(at=False, text="又来了"),
        actions,
        service.store.get_conversation_by_ref(conversation.ref),
    )
    assert key not in service._pending

    service._reset_backoff(key)
    assert key not in service._backoff_until
    assert key not in service._silence_streak
    asyncio.run(service.shutdown())


def test_activation_creates_plugin_config_and_status_shows_it(tmp_path):
    config_path = tmp_path / "mate-config.json"
    service = _service(
        tmp_path,
        jianer_mate_config_path=str(config_path),
        jianer_mate_planner_model="test",
    )
    _activate(service)
    assert config_path.is_file()
    document = json.loads(config_path.read_text(encoding="utf-8"))
    assert document["models"]["planner"] == "test"
    assert document["_readme"]
    status = service._status_text(
        ConversationRef("milky", "10001", "group", "222")
    )
    assert str(config_path) in status
    assert "已加载" in status

    # User edits in the file win on the next activation and are not overwritten.
    config_path.write_text(
        json.dumps({"models": {"planner": "edited"}}), encoding="utf-8"
    )
    reloaded = _service(tmp_path, jianer_mate_config_path=str(config_path))
    _activate(reloaded)
    assert reloaded.options.planner_model == "edited"
    assert json.loads(config_path.read_text(encoding="utf-8"))["models"][
        "planner"
    ] == "edited"
    asyncio.run(service.shutdown())
    asyncio.run(reloaded.shutdown())


def test_replyer_receives_tools_and_status_reports_them(tmp_path):
    replyer = _StubReplyer(ReplyPlan(messages=("在的",)))
    service = _service(tmp_path, replyer=replyer)
    _activate(service)
    actions = _FakeActions()
    assert asyncio.run(
        service.handle_fallback(_event(at=True, text="现在几点了"), actions)
    ) is True
    call = replyer.calls[-1]
    names = [item["function"]["name"] for item in call["tools"]]
    assert "get_current_time" in names
    assert "read_recent_chat" in names
    assert callable(call["tool_executor"])
    assert call["tool_max_rounds"] == 3
    status = service._status_text(
        ConversationRef("milky", "10001", "group", "222")
    )
    assert "工具调用：开" in status
    asyncio.run(service.shutdown())


def test_tools_can_be_disabled_by_config(tmp_path):
    replyer = _StubReplyer(ReplyPlan(messages=("在的",)))
    service = _service(
        tmp_path,
        replyer=replyer,
        jianer_mate_tools_enabled=False,
    )
    _activate(service)
    asyncio.run(
        service.handle_fallback(_event(at=True, text="几点了"), _FakeActions())
    )
    assert replyer.calls[-1]["tools"] == ()
    status = service._status_text(
        ConversationRef("milky", "10001", "group", "222")
    )
    assert "工具调用：关闭" in status
    asyncio.run(service.shutdown())


def test_suffix_is_appended_to_the_last_message(tmp_path):
    suffix_path = tmp_path / "suffix.json"
    suffix_path.write_text(
        json.dumps({"global_suffix": " ♡"}), encoding="utf-8"
    )
    service = _service(
        tmp_path,
        replyer=_StubReplyer(ReplyPlan(messages=("在的",))),
        jianer_mate_suffix_enabled=True,
        jianer_mate_suffix_config_path=str(suffix_path),
    )
    _activate(service)
    actions = _FakeActions()
    asyncio.run(service.handle_fallback(_event(at=True, text="在吗"), actions))
    assert _sent_text(actions.sent[-1]).endswith("♡")
    asyncio.run(service.shutdown())


def test_tts_sends_voice_when_enabled(tmp_path):
    class _StubSpeech:
        def __init__(self, path):
            self.path = path
            self.texts = []
            self.closed = False

        async def synthesize_artifact(self, text, options):
            self.texts.append((text, dict(options)))
            return SimpleNamespace(path=self.path)

        async def shutdown(self):
            self.closed = True

    voice_path = tmp_path / "voice.mp3"
    voice_path.write_bytes(b"ID3")
    speech = _StubSpeech(voice_path)
    service = _service(
        tmp_path,
        replyer=_StubReplyer(ReplyPlan(messages=("在的",))),
        speech=speech,
        jianer_mate_tts_enabled=True,
    )
    _activate(service)
    actions = _FakeActions()
    actions.capabilities = frozenset(
        {
            Capability.SEND_REPLY,
            Capability.SEND_IMAGE,
            Capability.SEND_AUDIO,
        }
    )
    asyncio.run(service.handle_fallback(_event(at=True, text="在吗"), actions))
    records = [
        segment
        for item in actions.sent
        for segment in item["message"]
        if isinstance(segment, segments.Record)
    ]
    assert records and records[0].file.endswith("voice.mp3")
    assert speech.texts and "在的" in speech.texts[0][0]
    asyncio.run(service.shutdown())
    assert speech.closed is True


class _StubLongTerm:
    def __init__(self):
        self.available = True
        self.error = ""
        self.created = []
        self.updated = []
        self.evidence = []

    def ensure_identity(self, **kwargs):
        del kwargs
        return "qq:5"

    def list_scoped_memories(self, **kwargs):
        del kwargs
        return ()

    def create_scoped_memory(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(id="42")

    def update_scoped_memory(self, **kwargs):
        self.updated.append(kwargs)
        return SimpleNamespace(id=kwargs.get("memory_id", ""))

    def add_scoped_memory_evidence(self, **kwargs):
        self.evidence.append(kwargs)
        return True

    def close(self):
        return None


class _StubReviewer:
    def __init__(self, actions):
        self.actions = tuple(actions)
        self.calls = []

    async def review(self, **kwargs):
        self.calls.append(kwargs)
        return self.actions


def test_memory_review_job_writes_memory_and_evidence(tmp_path):
    service = _service(tmp_path)
    _activate(service)
    conversation = _enable_group(service)
    service.store.append_message(
        conversation.ref,
        direction="incoming",
        content="我超喜欢猫",
        sender_id="5",
        sender_name="小明",
        now=int(time.time()),
    )
    long_term = _StubLongTerm()
    service.long_term = long_term
    service.memory_reviewer = _StubReviewer(
        [
            MemoryAction(
                action="create",
                scope="person",
                canonical_fact="用户喜欢猫",
                memory_text="我记得他喜欢猫。",
            )
        ]
    )
    assert service.store.enqueue_memory_job(
        conversation.ref, exchange_key="k1", canonical_user_id="qq:5"
    )
    asyncio.run(service._run_memory_jobs())

    assert long_term.created
    assert long_term.created[0]["canonical_fact"] == "用户喜欢猫"
    assert long_term.evidence
    assert "喜欢猫" in long_term.evidence[0]["excerpt"]
    assert long_term.evidence[0]["memory_id"] == "42"
    assert service.store.count_memory_jobs() == 0
    asyncio.run(service.shutdown())


def test_subagent_runner_is_wired_after_activation(tmp_path):
    service = _service(
        tmp_path,
        jianer_mate_tool_subagents_enabled=True,
    )
    _activate(service)
    assert service.tool_runtime.subagent_runner is not None
    asyncio.run(service.shutdown())
