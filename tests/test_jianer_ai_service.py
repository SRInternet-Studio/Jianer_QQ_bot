from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from jianer import common as Manager, segments as Segments
from jianer.adapters import (
    Capability,
    ConversationKind,
    MediaResolution,
    ResolutionStatus,
)

from plugins.JianerAI.memory import JianerMemoryStore
from plugins.JianerAI.presets import PresetStore
from plugins.JianerAI.providers import (
    AssistantTurn,
    ProviderResponse,
    ProviderToolCall,
    ToolResultTurn,
)
from plugins.JianerAI.service import JianerAIService, RuntimeOptions
from plugins.JianerAI.speech import SpeechArtifact, SpeechOptions
from plugins.JianerAI.suffix import SuffixStore


class FakeProviders:
    def __init__(self, answer: str = "模型回答。"):
        self.answer = answer
        self.calls = []
        self.models = {"model-a": "模型 A", "model-b": "模型 B"}

    def list_models(self):
        return dict(self.models)

    def get(self, key):
        if key not in self.models:
            from plugins.JianerAI.providers import UnknownModelError

            raise UnknownModelError(key)
        return SimpleNamespace(key=key, friendly_name=self.models[key])

    async def chat(
        self,
        key,
        message,
        *,
        history=(),
        system_prompt="",
        attachments=(),
    ):
        self.calls.append(
            {
                "key": key,
                "message": message,
                "history": tuple(history),
                "system_prompt": system_prompt,
                "attachments": tuple(attachments),
            }
        )
        return self.answer


class FakeSpeech:
    def __init__(self, root: Path):
        self.root = root
        self.calls = []
        self.closed = False

    async def synthesize_artifact(self, text, settings=None):
        self.calls.append((text, settings))
        path = self.root / f"speech-{len(self.calls)}.mp3"
        path.write_bytes(b"ID3-test")
        return SpeechArtifact(path=path, mime="audio/mpeg", size=8)

    async def shutdown(self):
        self.closed = True


class FakeActions:
    protocol = "onebot"

    def __init__(self, capabilities=()):
        self.capabilities = frozenset(capabilities)
        self.sent = []
        self.media_requests = []

    async def send(self, message, **target):
        self.sent.append((target, message))
        return SimpleNamespace(data=SimpleNamespace(message_id=str(len(self.sent))))

    async def resolve_media(self, request, *, conversation, policy):
        self.media_requests.append((request, conversation, policy))
        return MediaResolution(
            status=ResolutionStatus.OK,
            error_code=None,
            mime="image/png",
            size=12,
            data=b"\x89PNG\r\n\x1a\nrest",
            source="https://cdn.example.test",
        )


class RecordingLogger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(str(message))

    def exception(self, message):
        self.messages.append(str(message))


def _preset_store(tmp_path: Path) -> PresetStore:
    preset_dir = tmp_path / "prerequisites"
    preset_dir.mkdir()
    (preset_dir / "Normal.txt").write_text(
        (
            "你是{self.bot_name}，正在和{self.event_user}交流。\n"
            "工具：{agent_tools}\n{agent_tools_info}"
        ),
        encoding="utf-8",
    )
    (preset_dir / "role.txt").write_text(
        "你在扮演测试角色。",
        encoding="utf-8",
    )
    (preset_dir / "current.json").write_text(
        json.dumps(
            {
                "Normal": {
                    "name": "默认角色",
                    "uid": [],
                    "info": "已切换默认角色",
                    "path": "Normal.txt",
                },
                "role": {
                    "name": "测试角色",
                    "uid": [],
                    "info": "已切换测试角色",
                    "path": "role.txt",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return PresetStore(
        preset_dir / "current.json",
        preset_dir,
        default_key="Normal",
    )


def _service(
    tmp_path: Path,
    *,
    answer="模型回答。",
    blocked_group_ids=(),
    logger=None,
):
    providers = FakeProviders(answer)
    speech = FakeSpeech(tmp_path)
    options = RuntimeOptions(
        project_root=tmp_path,
        reminder="~",
        bot_name="简儿",
        bot_name_en="Jianer",
        default_model="model-a",
        memory_model="model-a",
        database_path=tmp_path / "memory.db",
        memory_enabled_default=True,
        memory_interval_seconds=21600,
        memory_scheduler_tick_seconds=3600,
        memory_min_new_rows=2,
        memory_topk=6,
        transcript_retention_days=30,
        tts_options=SpeechOptions(),
        blocked_group_ids=frozenset(str(item) for item in blocked_group_ids),
    )
    service = JianerAIService(
        options,
        runtime={
            "root_users": ["1"],
            "super_users": [],
            "manage_users": [],
            "confused_word": "{bot_name}不能这么做。",
            **({"logger": logger} if logger is not None else {}),
        },
        providers=providers,
        memory=JianerMemoryStore(options.database_path),
        presets=_preset_store(tmp_path),
        speech=speech,
        suffixes=SuffixStore(tmp_path / "suffix.json"),
    )
    return service, providers, speech


def test_ai_dialogue_logs_prompt_answer_scope_and_model(tmp_path: Path):
    async def scenario():
        logger = RecordingLogger()
        service, _, _ = _service(
            tmp_path,
            answer="这是模型回答。",
            logger=logger,
        )
        event = _at_event("你好，简儿")
        assert await service.handle_fallback(event, FakeActions()) is True

        logs = "\n".join(logger.messages)
        assert "JianerAI AI对话开始" in logs
        assert "JianerAI AI对话完成" in logs
        assert '"model":"model-a"' in logs
        assert '"conversation_kind":"group"' in logs
        assert '"conversation_id":"100"' in logs
        assert '"prompt":"你好，简儿"' in logs
        assert '"answer":"这是模型回答。"' in logs
        await service.shutdown()

    asyncio.run(scenario())


def test_runtime_options_enable_agent_by_default() -> None:
    options = RuntimeOptions.from_runtime(
        {"config": SimpleNamespace(others={}, black_list=[])}
    )

    assert options.agent_enabled_default is True


def _event(
    text: str,
    *,
    protocol="onebot",
    self_id="bot-1",
    user_id="42",
    group_id="100",
    message=None,
    mentioned=False,
):
    values = {
        "protocol": protocol,
        "self_id": self_id,
        "user_id": user_id,
        "message_id": f"message-{user_id}-{text}",
        "msg_str": text,
        "message": (
            Manager.Message(Segments.Text(text))
            if message is None
            else message
        ),
        "time": 1_900_000_000,
        "sender": {"nickname": f"user-{user_id}"},
        "is_mentioned": mentioned,
    }
    if group_id is not None:
        values["group_id"] = group_id
        values["conversation_id"] = group_id
    else:
        values["conversation_id"] = user_id
    return SimpleNamespace(**values)


def _at_event(text: str = "", *, extra_segments=(), **kwargs):
    self_id = str(kwargs.get("self_id", "bot-1"))
    segments = [Segments.At(self_id)]
    if text:
        segments.append(Segments.Text(text))
    segments.extend(extra_segments)
    return _event(
        text,
        message=Manager.Message(*segments),
        mentioned=True,
        **kwargs,
    )


def test_sensitive_tool_values_are_removed_from_history_transcript_and_reply(
    tmp_path: Path,
):
    async def scenario():
        logger = RecordingLogger()
        service, _, _ = _service(tmp_path, logger=logger)
        actions = FakeActions()
        secret = "group-chat-password-123"
        event = _at_event(f"请代填密码 {secret}")

        class SensitiveAgent:
            async def run(self, **kwargs):
                assert secret in kwargs["message"]
                assert any(
                    lock.locked()
                    for lock in service._memory_generation_locks.values()
                )
                kwargs["context"].sensitive_values.add(secret)
                return f"已填写 {secret}"

        service.agent = SensitiveAgent()
        await service.observe(event, actions)
        assert await service.handle_fallback(event, actions) is True

        histories = json.dumps(
            list(service._histories.values()), ensure_ascii=False, default=str
        )
        assert secret not in histories
        assert "[REDACTED]" in histories
        with sqlite3.connect(service.options.database_path) as conn:
            rows = conn.execute(
                "SELECT content FROM raw_transcript_messages"
            ).fetchall()
        transcript = "\n".join(str(row[0]) for row in rows)
        assert secret not in transcript
        assert "[REDACTED]" in transcript
        assert not any(
            lock.locked()
            for lock in service._memory_generation_locks.values()
        )
        sent = "\n".join(str(message) for _, message in actions.sent)
        assert secret not in sent
        logs = "\n".join(logger.messages)
        assert secret not in logs
        assert "[REDACTED]" in logs
        await service.shutdown()

    asyncio.run(scenario())


def test_service_exposes_privileged_web_browser_by_default(tmp_path: Path):
    async def scenario():
        service, _, _ = _service(tmp_path)
        actions = FakeActions()
        event = _event("~浏览网页")
        key = await service._conversation_key(event, actions)
        canonical = await asyncio.to_thread(
            service._canonical_identity, event, actions
        )
        context = service._tool_context(event, actions, key, canonical)
        names = {spec.name for spec in service.tools.available(context)}
        assert "web_browser" in names
        await service.shutdown()

    asyncio.run(scenario())


def test_conversation_key_is_group_shared_and_isolates_bot_protocol_and_preset(
    tmp_path: Path,
):
    async def scenario():
        service, _, _ = _service(tmp_path)
        actions = FakeActions()
        first = await service._conversation_key(
            _event("~你好", user_id="42"), actions
        )
        second = await service._conversation_key(
            _event("~你好", user_id="77"), actions
        )
        other_bot = await service._conversation_key(
            _event("~你好", self_id="bot-2"), actions
        )

        assert first == second
        assert first.kind is ConversationKind.GROUP
        assert first.self_id == "bot-1"
        assert other_bot != first

        await service.switch_persona(
            _event("~切换角色 测试角色"),
            actions,
            "测试角色",
        )
        role = await service._conversation_key(
            _event("~你好", user_id="77"), actions
        )
        assert role.preset == "role"
        assert role != first
        await service.shutdown()

    asyncio.run(scenario())


def test_private_sessions_are_separate_and_model_switch_clears_only_current(
    tmp_path: Path,
):
    async def scenario():
        service, _, _ = _service(tmp_path)
        actions = FakeActions()
        first_event = _event("你好", user_id="42", group_id=None)
        second_event = _event("你好", user_id="77", group_id=None)
        first = await service._conversation_key(first_event, actions)
        second = await service._conversation_key(second_event, actions)
        service._histories[first] = [{"role": "user", "content": "old"}]
        service._histories[second] = [{"role": "user", "content": "keep"}]

        assert await service.switch_model(first_event, actions, "model-b")
        assert first not in service._histories
        assert service._histories[second] == [
            {"role": "user", "content": "keep"}
        ]
        assert service._model_for(first) == "model-b"
        assert service._model_for(second) == "model-a"
        await service.shutdown()

    asyncio.run(scenario())


def test_group_fallback_requires_at_and_bare_at_triggers_ai(
    tmp_path: Path,
):
    async def scenario():
        service, providers, _ = _service(tmp_path)
        actions = FakeActions({Capability.SEND_REPLY})
        assert not await service.handle_fallback(_event("你好"), actions)
        assert not await service.handle_fallback(_event("~你好"), actions)
        assert providers.calls == []

        assert await service.handle_fallback(
            _event("~测试角色"), actions
        )
        assert "已切换测试角色" in str(actions.sent[-1][1])
        assert providers.calls == []

        assert await service.handle_fallback(_at_event("~你好"), actions)
        assert providers.calls[-1]["message"] == "你好"

        assert await service.handle_fallback(_at_event(), actions)
        assert providers.calls[-1]["message"] == (
            "用户在群聊中只@了你，请自然地回应对方。"
        )
        await service.shutdown()

    asyncio.run(scenario())


def test_blocked_group_rejects_ai_fallback_and_commands(tmp_path: Path):
    async def scenario():
        service, providers, _ = _service(
            tmp_path,
            blocked_group_ids={"100"},
        )
        actions = FakeActions(capabilities={Capability.SEND_REPLY})
        event = _at_event("你好")

        await service.observe(event, actions)
        assert service.memory.count_transcripts(
            protocol="onebot",
            self_id="bot-1",
            conversation_kind="group",
            conversation_id="100",
        ) == 0

        assert await service.handle_fallback(event, actions)
        assert providers.calls == []
        assert "Error 403" in str(actions.sent[-1][1])

        actions.sent.clear()
        assert await service.reject_blocked_group(event, actions)
        assert "Chat location restriction" in str(actions.sent[-1][1])

        private_event = _event("你好", group_id=None)
        assert not await service.reject_blocked_group(private_event, actions)
        await service.shutdown()

    asyncio.run(scenario())


def test_feishu_mention_and_private_messages_use_ai_without_native_media(
    tmp_path: Path,
):
    async def scenario():
        service, providers, _ = _service(tmp_path)
        feishu_actions = FakeActions({Capability.SEND_REPLY})
        feishu_actions.protocol = "feishu"
        feishu_event = _event(
            "请回答",
            protocol="feishu",
            group_id="oc-group",
            user_id="ou-user",
            mentioned=True,
        )
        assert await service.handle_fallback(feishu_event, feishu_actions)
        assert providers.calls[-1]["attachments"] == ()

        bare_feishu_event = _event(
            "",
            protocol="feishu",
            group_id="oc-group",
            user_id="ou-user",
            mentioned=True,
        )
        assert await service.handle_fallback(
            bare_feishu_event,
            feishu_actions,
        )
        assert providers.calls[-1]["message"] == (
            "用户在群聊中只@了你，请自然地回应对方。"
        )

        private_actions = FakeActions()
        assert await service.handle_fallback(
            _event("直接私聊", group_id=None),
            private_actions,
        )
        assert providers.calls[-1]["message"] == "直接私聊"
        await service.shutdown()

    asyncio.run(scenario())


def test_only_ai_reply_gets_suffix_and_private_tts_defaults_off(
    tmp_path: Path,
):
    async def scenario():
        service, _, speech = _service(tmp_path, answer="你好。")
        service.suffixes.set_global("喵")
        event = _event("直接私聊", group_id=None)
        actions = FakeActions()

        assert await service.handle_fallback(event, actions)
        assert "你好喵。" in str(actions.sent[-1][1])
        assert speech.calls == []

        await service.show_model_menu(event, actions)
        assert "AI管理菜单" in str(actions.sent[-1][1])
        assert "AI管理菜单喵" not in str(actions.sent[-1][1])
        await service.shutdown()

    asyncio.run(scenario())


def test_group_tts_defaults_on_and_resolved_media_bytes_reach_provider(
    tmp_path: Path,
):
    async def scenario():
        service, providers, speech = _service(tmp_path)
        actions = FakeActions(
            {
                Capability.SEND_REPLY,
                Capability.SEND_AUDIO,
                Capability.RESOLVE_MEDIA,
            }
        )
        event = _at_event(
            "看图",
            extra_segments=(
                Segments.Image("https://cdn.example.test/image.png"),
            ),
        )

        assert await service.handle_fallback(event, actions)
        assert len(providers.calls[-1]["attachments"]) == 1
        assert providers.calls[-1]["attachments"][0].data.startswith(b"\x89PNG")
        assert len(actions.media_requests) == 1
        assert speech.calls
        assert any(
            any(isinstance(segment, Segments.Record) for segment in sent_message)
            for _, sent_message in actions.sent
        )
        await service.shutdown()
        assert speech.closed

    asyncio.run(scenario())


def test_observer_deduplicates_group_transcript_across_active_presets(
    tmp_path: Path,
):
    async def scenario():
        service, _, _ = _service(tmp_path)
        actions = FakeActions()
        event = _event("普通群消息")
        await service.observe(event, actions)
        await service.switch_persona(event, actions, "测试角色")
        await service.observe(event, actions)

        assert service.memory.count_transcripts(
            protocol="onebot",
            self_id="bot-1",
            conversation_kind="group",
            conversation_id="100",
        ) == 1
        await service.shutdown()

    asyncio.run(scenario())


def test_memory_status_reports_new_raw_transcript_count(tmp_path: Path):
    async def scenario():
        service, _, _ = _service(tmp_path)
        actions = FakeActions()
        event = _event("待提炼的普通群消息")
        await service.observe(event, actions)

        assert await service.memory_command(event, actions, "状态")
        assert "待提炼记录: 1" in str(actions.sent[-1][1])
        await service.shutdown()

    asyncio.run(scenario())


def test_memory_generation_uses_canonical_preset_across_sessions(
    tmp_path: Path,
):
    async def scenario():
        service, providers, _ = _service(
            tmp_path,
            answer='{"memories":[{"content":"用户喜欢蓝色","weight":0.8}]}',
        )
        actions = FakeActions()
        group_event = _event("群聊事实", user_id="42", group_id="100")
        private_event = _event("私聊事实", user_id="42", group_id=None)
        await service.observe(group_event, actions)
        await service.observe(private_event, actions)

        key = await service._conversation_key(group_event, actions)
        canonical = service._canonical_identity(group_event, actions)
        created = await service._generate_memories_now(
            canonical,
            key,
            force=True,
        )

        assert created == 1
        assert "群聊事实" in providers.calls[-1]["message"]
        assert "私聊事实" in providers.calls[-1]["message"]
        memories = service.memory.list_memories(
            canonical_user_id=canonical,
            preset=key.preset,
        )
        assert [item.content for item in memories] == ["用户喜欢蓝色"]
        assert (
            service.memory.fetch_generation_batch(
                canonical_user_id=canonical,
                preset=key.preset,
            )
            is None
        )
        await service.shutdown()

    asyncio.run(scenario())


def test_memory_generation_is_single_flight_per_identity_and_preset(
    tmp_path: Path,
):
    async def scenario():
        service, providers, _ = _service(
            tmp_path,
            answer='{"memories":[{"content":"单飞事实","weight":0.5}]}',
        )
        actions = FakeActions()
        first = _event("事实一", user_id="42", group_id="100")
        second = _event("事实二", user_id="42", group_id=None)
        await service.observe(first, actions)
        await service.observe(second, actions)
        key = await service._conversation_key(first, actions)
        canonical = service._canonical_identity(first, actions)

        results = await asyncio.gather(
            service._generate_memories_now(canonical, key, force=True),
            service._generate_memories_now(canonical, key, force=True),
        )

        assert sorted(results) == [0, 1]
        assert len(providers.calls) == 1
        await service.shutdown()

    asyncio.run(scenario())


def test_valid_empty_memory_generation_advances_the_cursor(tmp_path: Path):
    async def scenario():
        service, _, _ = _service(
            tmp_path,
            answer='{"memories":[]}',
        )
        actions = FakeActions()
        event = _event("没有需要长期保存的事实", user_id="42")
        await service.observe(event, actions)
        key = await service._conversation_key(event, actions)
        canonical = service._canonical_identity(event, actions)

        assert (
            await service._generate_memories_now(
                canonical,
                key,
                force=True,
            )
            == 0
        )
        assert (
            service.memory.fetch_generation_batch(
                canonical_user_id=canonical,
                preset=key.preset,
            )
            is None
        )
        await service.shutdown()

    asyncio.run(scenario())


def test_invalid_memory_generation_is_durably_deferred(tmp_path: Path):
    async def scenario():
        service, _, _ = _service(tmp_path, answer="not-json")
        actions = FakeActions()
        event = _event("稍后应重试的事实", user_id="42")
        await service.observe(event, actions)
        key = await service._conversation_key(event, actions)
        canonical = service._canonical_identity(event, actions)

        assert (
            await service._generate_memories_now(
                canonical,
                key,
                force=True,
            )
            == 0
        )
        status = service.memory.get_memory_status(
            canonical_user_id=canonical,
            preset=key.preset,
        )
        assert status["failure_count"] == 1
        assert status["next_retry_at"] > 0
        assert service.memory.list_due_memory_scopes(
            now=status["next_retry_at"] - 1
        ) == ()
        await service.shutdown()

    asyncio.run(scenario())


def test_deleting_active_persona_resets_cached_conversations(tmp_path: Path):
    async def scenario():
        service, _, _ = _service(tmp_path)
        actions = FakeActions()
        admin_event = _event("~切换角色 测试角色", user_id="1")
        await service.switch_persona(admin_event, actions, "测试角色")
        old_key = await service._conversation_key(admin_event, actions)
        service._histories[old_key] = [{"role": "user", "content": "old"}]

        assert await service.delete_persona(
            admin_event,
            actions,
            "测试角色",
        )
        new_key = await service._conversation_key(admin_event, actions)

        assert new_key.preset == "Normal"
        assert old_key not in service._histories
        assert all(item.key != "role" for item in service.presets.list_presets())
        await service.shutdown()

    asyncio.run(scenario())


def test_admin_snapshot_updates_are_visible_after_plugin_setup(tmp_path: Path):
    async def scenario():
        service, _, _ = _service(tmp_path)
        actions = FakeActions()
        event = _event("~添加预设", user_id="2")
        definition = "新角色 简介 : 你是新角色。"

        assert await service.add_persona(event, actions, definition)
        assert all(
            item.name != "新角色"
            for item in service.presets.list_presets()
        )

        service.runtime["admins"] = ["2"]
        assert await service.add_persona(event, actions, definition)
        assert any(
            item.name == "新角色"
            for item in service.presets.list_presets()
        )
        await service.shutdown()

    asyncio.run(scenario())


class ToolLoopProviders:
    def __init__(self):
        self.models = {"model-a": "模型 A"}
        self.requests = []
        self.chat_calls = []

    def list_models(self):
        return dict(self.models)

    def get(self, key):
        if key not in self.models:
            from plugins.JianerAI.providers import UnknownModelError

            raise UnknownModelError(key)
        return SimpleNamespace(key=key, friendly_name=self.models[key])

    def supports_tools(self, key):
        return True

    async def complete_request(self, key, request):
        self.requests.append(request)
        if len(request.turns) == 0:
            call = ProviderToolCall(
                id="service-call-1",
                name="calculate_expression",
                arguments={"expression": "20+22"},
            )
            turn = AssistantTurn(tool_calls=(call,))
            return ProviderResponse(text="", tool_calls=(call,), turn=turn)
        return ProviderResponse(
            text="最终答案。",
            tool_calls=(),
            turn=AssistantTurn(text="最终答案。"),
        )

    async def chat(
        self,
        key,
        message,
        *,
        history=(),
        system_prompt="",
        attachments=(),
    ):
        self.chat_calls.append(
            {
                "key": key,
                "message": message,
                "history": tuple(history),
                "system_prompt": system_prompt,
                "attachments": tuple(attachments),
            }
        )
        return "普通回答。"


def test_service_agent_keeps_tool_turns_out_of_history_suffix_and_tts(
    tmp_path: Path,
):
    async def scenario():
        service, _, speech = _service(tmp_path)
        providers = ToolLoopProviders()
        service.providers = providers
        service.agent.providers = providers
        service.suffixes.set_global("喵")
        event = _at_event("计算")
        actions = FakeActions(
            {Capability.SEND_REPLY, Capability.SEND_AUDIO}
        )

        assert await service.handle_fallback(event, actions)
        assert len(providers.requests) == 2
        system_prompt = providers.requests[0].system_prompt
        assert "工具：calculate_expression" in system_prompt
        assert "安全计算只包含数字、括号和基础算术运算符的表达式" in system_prompt
        assert "用法：calculate_expression(expression)" in system_prompt
        assert "expression（string，必填）" in system_prompt
        assert "{agent_tools}" not in system_prompt
        assert "{agent_tools_info}" not in system_prompt
        assert "否则最终回答不得展示、列出或附带信息来源及 URL" in system_prompt
        assert "调用 web_search 本身不代表用户要求展示来源" in system_prompt
        assert "用户明确要求来源时" in system_prompt
        assert "所有最终回答必须使用纯文本" in system_prompt
        assert "不得使用 Markdown 或 HTML" in system_prompt
        assert isinstance(providers.requests[1].turns[0], AssistantTurn)
        tool_result = providers.requests[1].turns[1]
        assert isinstance(tool_result, ToolResultTurn)
        assert json.loads(tool_result.content)["data"]["result"] == 42

        key = await service._conversation_key(event, actions)
        assert service._histories[key] == [
            {"role": "user", "content": "计算"},
            {"role": "assistant", "content": "最终答案。"},
        ]
        assert any("最终答案喵。" in str(message) for _, message in actions.sent)
        assert speech.calls[-1][0] == "最终答案。"
        await service.shutdown()

    asyncio.run(scenario())


def test_agent_command_persists_session_override_and_disabled_mode_uses_chat(
    tmp_path: Path,
):
    async def scenario():
        service, _, _ = _service(tmp_path)
        providers = ToolLoopProviders()
        service.providers = providers
        service.agent.providers = providers
        event = _event("你好", group_id=None)
        actions = FakeActions()

        assert await service.configure_agent(event, actions, "关闭")
        key = await service._conversation_key(event, actions)
        stored = service.memory.get_session_settings(
            protocol=key.protocol,
            self_id=key.self_id,
            conversation_kind=key.kind.value,
            conversation_id=key.conversation_id,
            preset=key.preset,
        )
        assert stored.agent_enabled is False

        assert await service.handle_fallback(event, actions)
        assert providers.requests == []
        assert len(providers.chat_calls) == 1
        assert "工具：无\n无" in providers.chat_calls[0]["system_prompt"]
        assert "所有最终回答必须使用纯文本" in providers.chat_calls[0]["system_prompt"]

        assert await service.configure_agent(event, actions, "自动")
        stored = service.memory.get_session_settings(
            protocol=key.protocol,
            self_id=key.self_id,
            conversation_kind=key.kind.value,
            conversation_id=key.conversation_id,
            preset=key.preset,
        )
        assert stored.agent_enabled is None
        assert await service.configure_agent(event, actions, "工具")
        assert "calculate_expression" in str(actions.sent[-1][1])
        await service.shutdown()

    asyncio.run(scenario())


def test_ai_reply_markdown_is_normalized_before_history_suffix_and_send(tmp_path: Path):
    async def scenario():
        service, providers, _ = _service(tmp_path)
        providers.answer = (
            "# 天气\n\n- **多云**\n- [和风天气](https://www.qweather.com)\n"
            "```text\n请带伞\n```"
        )
        event = _event("天气", group_id=None)
        actions = FakeActions()

        assert await service.handle_fallback(event, actions)
        key = await service._conversation_key(event, actions)
        expected = "天气\n\n多云\n和风天气（https://www.qweather.com）\n请带伞"
        assert service._histories[key][-1] == {
            "role": "assistant",
            "content": expected,
        }
        sent_text = str(actions.sent[-1][1])
        assert expected in sent_text
        for marker in ("# ", "- ", "**", "```", "]("):
            assert marker not in sent_text
        await service.shutdown()

    asyncio.run(scenario())
