from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from jianer import common as Manager, segments as Segments
from jianer.adapters import (
    Capability,
    ConversationKey,
    ConversationKind,
    MediaKind,
    MediaRequest,
    MediaResolution,
    MediaSourceKind,
    ResolutionErrorCode,
    ResolutionStatus,
)

from plugins.JianerAI.memory import JianerMemoryStore
from plugins.JianerAI.presets import PresetStore
from plugins.JianerAI.providers import (
    AssistantTurn,
    ProviderResponse,
    ProviderToolCall,
    ToolResultTurn,
    UnknownModelError,
)
from plugins.JianerAI.service import JianerAIService, RuntimeOptions
from plugins.JianerAI.speech import SpeechArtifact, SpeechOptions
from plugins.JianerAI.suffix import SuffixStore
from plugins.JianerAI.tools import ToolRisk, ToolSpec


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


class MemoryReviewProviders(FakeProviders):
    def __init__(self, answer: str, review_answer: str | Exception):
        super().__init__(answer)
        self.review_answer = review_answer
        self.review_calls = 0

    async def chat(
        self,
        key,
        message,
        *,
        history=(),
        system_prompt="",
        attachments=(),
    ):
        if "独立长期记忆审查器" in system_prompt:
            self.review_calls += 1
            self.calls.append(
                {
                    "key": key,
                    "message": message,
                    "history": tuple(history),
                    "system_prompt": system_prompt,
                    "attachments": tuple(attachments),
                }
            )
            if isinstance(self.review_answer, Exception):
                raise self.review_answer
            return self.review_answer
        return await super().chat(
            key,
            message,
            history=history,
            system_prompt=system_prompt,
            attachments=attachments,
        )


class ModerationProviders(FakeProviders):
    def __init__(
        self,
        answer: str,
        moderation_answer: str | Exception,
    ):
        super().__init__(answer)
        self.moderation_answer = moderation_answer
        self.moderation_calls = 0

    async def chat(
        self,
        key,
        message,
        *,
        history=(),
        system_prompt="",
        attachments=(),
    ):
        if "JianerAI content safety moderator" in system_prompt:
            self.moderation_calls += 1
            self.calls.append(
                {
                    "key": key,
                    "message": message,
                    "history": tuple(history),
                    "system_prompt": system_prompt,
                    "attachments": tuple(attachments),
                }
            )
            if isinstance(self.moderation_answer, Exception):
                raise self.moderation_answer
            return self.moderation_answer
        return await super().chat(
            key,
            message,
            history=history,
            system_prompt=system_prompt,
            attachments=attachments,
        )


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
    preset_dir.mkdir(exist_ok=True)
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
    memory_review_enabled=False,
    moderation_enabled=False,
    moderation_model="model-b",
    provider=None,
):
    providers = provider or FakeProviders(answer)
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
        transcript_retention_days=90,
        tts_options=SpeechOptions(),
        blocked_group_ids=frozenset(str(item) for item in blocked_group_ids),
        content_moderation_enabled=moderation_enabled,
        content_moderation_model=moderation_model,
    )
    service = JianerAIService(
        options,
        runtime={
            "config": SimpleNamespace(
                others={
                    "memory_review_external_context_enabled": (
                        memory_review_enabled
                    )
                }
            ),
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
        assert "你好，简儿" in logs
        assert "canonical_user_id" in logs
        assert "qq:42" in logs
        assert '"answer":"这是模型回答。"' in logs
        await service.shutdown()

    asyncio.run(scenario())


def test_runtime_options_enable_agent_and_disable_moderation_by_default() -> None:
    options = RuntimeOptions.from_runtime(
        {"config": SimpleNamespace(others={}, black_list=[])}
    )

    assert options.agent_enabled_default is True
    assert options.default_model == "grok"
    assert options.content_moderation_enabled is False
    assert options.content_moderation_model is None


def test_runtime_options_accept_an_explicit_moderation_model() -> None:
    options = RuntimeOptions.from_runtime(
        {
            "config": SimpleNamespace(
                others={
                    "content_moderation_enabled": True,
                    "content_moderation_model": "review-model",
                },
                black_list=[],
            )
        }
    )

    assert options.content_moderation_enabled is True
    assert options.content_moderation_model == "review-model"


def test_runtime_options_require_a_model_when_moderation_is_enabled() -> None:
    with pytest.raises(ValueError, match="content_moderation_model"):
        RuntimeOptions.from_runtime(
            {
                "config": SimpleNamespace(
                    others={"content_moderation_enabled": True},
                    black_list=[],
                )
            }
        )


def test_disabled_moderation_does_not_validate_or_construct_its_model(
    tmp_path: Path,
) -> None:
    service, _, _ = _service(
        tmp_path,
        moderation_enabled=False,
        moderation_model="model-that-is-not-loaded",
    )

    assert service.moderator is None
    asyncio.run(service.shutdown())


def test_enabled_moderation_validates_the_selected_model(
    tmp_path: Path,
) -> None:
    with pytest.raises(UnknownModelError):
        _service(
            tmp_path,
            moderation_enabled=True,
            moderation_model="model-that-is-not-loaded",
        )


def test_safe_request_is_moderated_before_main_model(tmp_path: Path):
    class PayloadCaptureProvider:
        def __init__(self):
            self.models = {
                "model-a": "模型 A",
                "model-b": "审核模型",
            }
            self.moderation_calls = []
            self.requests = []

        def list_models(self):
            return dict(self.models)

        def get(self, key):
            if key not in self.models:
                from plugins.JianerAI.providers import UnknownModelError

                raise UnknownModelError(key)
            return SimpleNamespace(
                key=key,
                friendly_name=self.models[key],
                model=key,
            )

        def supports_tools(self, key):
            return True

        async def chat(
            self,
            key,
            message,
            *,
            history=(),
            system_prompt="",
            attachments=(),
        ):
            self.moderation_calls.append(
                {
                    "key": key,
                    "message": message,
                    "history": tuple(history),
                    "system_prompt": system_prompt,
                    "attachments": tuple(attachments),
                }
            )
            return (
                '{"decision":"allow","categories":[],'
                '"reason":"普通科普","refusal":""}'
            )

        async def complete_request(self, key, request):
            self.requests.append((key, request))
            return ProviderResponse(
                text="这是安全的主模型回答。",
                tool_calls=(),
                turn=AssistantTurn(text="这是安全的主模型回答。"),
            )

    async def scenario():
        control_root = tmp_path / "without-moderation"
        reviewed_root = tmp_path / "with-moderation"
        control_root.mkdir()
        reviewed_root.mkdir()
        control_provider = PayloadCaptureProvider()
        reviewed_provider = PayloadCaptureProvider()
        control_service, _, _ = _service(
            control_root,
            moderation_enabled=False,
            provider=control_provider,
        )
        reviewed_service, _, _ = _service(
            reviewed_root,
            moderation_enabled=True,
            moderation_model="model-b",
            provider=reviewed_provider,
        )
        control_actions = FakeActions()
        reviewed_actions = FakeActions()

        assert await control_service.handle_fallback(
            _event("请解释光合作用", group_id=None),
            control_actions,
        )
        assert await reviewed_service.handle_fallback(
            _event("请解释光合作用", group_id=None),
            reviewed_actions,
        )

        assert len(reviewed_provider.moderation_calls) == 1
        review_call = reviewed_provider.moderation_calls[0]
        assert review_call["key"] == "model-b"
        review_payload = json.loads(review_call["message"])
        assert review_payload["current_request"]["text"] == "请解释光合作用"
        persona_template = review_payload["persona_template"]
        assert "你是简儿，正在和user-42交流。" in persona_template
        assert "工具：无" in persona_template
        assert "persona_id" not in persona_template
        assert review_call["history"] == ()

        assert len(control_provider.requests) == 1
        assert len(reviewed_provider.requests) == 1
        assert reviewed_provider.requests[0] == control_provider.requests[0]
        _, main_request = reviewed_provider.requests[0]
        assert "当前请求已经经过独立的前置审核" not in (
            main_request.system_prompt
        )
        assert "这是安全的主模型回答。" in str(
            reviewed_actions.sent[-1][1]
        )
        await control_service.shutdown()
        await reviewed_service.shutdown()

    asyncio.run(scenario())


def test_grok_main_model_keeps_complete_persona_after_moderation(tmp_path: Path):
    async def scenario():
        provider = ModerationProviders(
            "Grok 的安全回答。",
            '{"decision":"allow","categories":[],"reason":"普通科普",'
            '"refusal":""}',
        )
        service, _, _ = _service(
            tmp_path,
            moderation_enabled=True,
            provider=provider,
        )
        provider.models["grok"] = "Grok"
        actions = FakeActions()
        event = _event("请解释光合作用", group_id=None)
        assert await service.switch_persona(event, actions, "测试角色")
        key = await service._conversation_key(event, actions)
        service._models[key] = "grok"
        actions.sent.clear()

        assert await service.handle_fallback(event, actions) is True

        assert provider.calls[-1]["key"] == "grok"
        system_prompt = provider.calls[-1]["system_prompt"]
        assert "你在扮演测试角色。" in system_prompt
        assert "当前请求已经经过独立的前置审核" not in system_prompt
        review_payload = json.loads(provider.calls[0]["message"])
        assert review_payload["persona_template"] == "你在扮演测试角色。"
        assert "Grok 的安全回答。" in str(actions.sent[-1][1])
        await service.shutdown()

    asyncio.run(scenario())


def test_disallowed_request_gets_persona_refusal_without_main_model_or_history(
    tmp_path: Path,
):
    async def scenario():
        provider = ModerationProviders(
            "主模型绝不应收到这条请求。",
            json.dumps(
                {
                    "decision": "refuse",
                    "categories": ["sexual_explicit"],
                    "reason": "请求生成露骨色情内容",
                    "refusal": "这种内容我就不写啦，我们换成含蓄的恋爱故事吧。",
                },
                ensure_ascii=False,
            ),
        )
        service, _, _ = _service(
            tmp_path,
            moderation_enabled=True,
            moderation_model="model-b",
            provider=provider,
        )
        actions = FakeActions()
        event = _event("请生成露骨色情内容", group_id=None)
        service.suffixes.set_for_identity("qq:42", "[UNTRUSTED_SUFFIX]")
        await service.observe(event, actions)

        assert await service.handle_fallback(event, actions) is True

        assert provider.moderation_calls == 1
        assert len(provider.calls) == 1
        assert provider.calls[0]["key"] == "model-b"
        assert "这种内容我就不写啦" in str(actions.sent[-1][1])
        assert "UNTRUSTED_SUFFIX" not in str(actions.sent[-1][1])
        key = await service._conversation_key(event, actions)
        assert service._histories.get(key, []) == []
        assert service.memory.query_conversation_episodes(
            preset=key.preset,
            protocol=key.protocol,
            self_id=key.self_id,
            conversation_kind=key.kind.value,
            conversation_id=key.conversation_id,
        ) == ()
        messages = service.memory.query_recent_chat(
            protocol=key.protocol,
            self_id=key.self_id,
            conversation_kind=key.kind.value,
            conversation_id=key.conversation_id,
            limit=10,
        )
        assert [item.content for item in messages] == [
            "[内容已由安全审核隐藏]"
        ]
        await service.shutdown()

    asyncio.run(scenario())


def test_invalid_moderation_response_fails_closed(tmp_path: Path):
    async def scenario():
        provider = ModerationProviders(
            "主模型绝不应在审核故障时运行。",
            "not-json",
        )
        service, _, _ = _service(
            tmp_path,
            moderation_enabled=True,
            provider=provider,
        )
        actions = FakeActions()

        assert await service.handle_fallback(
            _event("普通问题", group_id=None),
            actions,
        )

        assert provider.moderation_calls == 1
        assert len(provider.calls) == 1
        assert "没法完成必要的安全检查" in str(actions.sent[-1][1])
        await service.shutdown()

    asyncio.run(scenario())


def test_service_admits_only_the_builtin_scoped_memory_writes(tmp_path: Path):
    service, _, _ = _service(tmp_path)
    event = _event("请记住我喜欢蓝莓", group_id=None)
    actions = FakeActions()
    context = service._tool_context(
        event,
        actions,
        ConversationKey(
            protocol="onebot",
            self_id="bot-1",
            kind=ConversationKind.PRIVATE,
            conversation_id="42",
            preset="Normal",
        ),
        "qq:42",
    )
    assert {spec.name for spec in service.tools.available(context)} >= {
        "create_my_memory",
        "update_my_memory",
    }

    registration = service.register_tool(
        ToolSpec(
            name="unlisted_plugin_write",
            description="must require central admission",
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            handler=lambda *_: None,
            risk=ToolRisk.MUTATING,
        )
    )
    assert "unlisted_plugin_write" not in {
        spec.name for spec in service.tools.available(context)
    }
    assert service.unregister_tool(registration) is True
    asyncio.run(service.shutdown())


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
        key = await service._conversation_key(event, actions)
        rows = service.memory.query_recent_chat(
            protocol=key.protocol,
            self_id=key.self_id,
            conversation_kind=key.kind.value,
            conversation_id=key.conversation_id,
            limit=100,
        )
        transcript = "\n".join(item.content for item in rows)
        assert secret not in transcript
        assert "[REDACTED]" in transcript
        episodes = service.memory.query_conversation_episodes(
            preset=key.preset,
            protocol=key.protocol,
            self_id=key.self_id,
            conversation_kind=key.kind.value,
            conversation_id=key.conversation_id,
            query="代填密码",
        )
        episode_text = "\n".join(
            f"{item.user_content}\n{item.assistant_content}"
            for item in episodes
        )
        assert secret not in episode_text
        assert "[REDACTED]" in episode_text
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
        assert providers.calls[-1]["message"].endswith("\n你好")
        assert '"display_name":"user-42"' in providers.calls[-1]["message"]
        assert '"user_id":"42"' in providers.calls[-1]["message"]

        assert await service.handle_fallback(_at_event(), actions)
        assert providers.calls[-1]["message"].endswith(
            "\n"
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
        assert providers.calls[-1]["message"].endswith(
            "\n"
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


def test_milky_fake_ip_media_falls_back_to_bounded_download(
    tmp_path: Path,
):
    class FakeMilkyActions(FakeActions):
        protocol = "milky"

        async def resolve_media(self, request, *, conversation, policy):
            self.media_requests.append((request, conversation, policy))
            if request.kind is MediaSourceKind.REMOTE_URL:
                return MediaResolution(
                    status=ResolutionStatus.REJECTED,
                    error_code=ResolutionErrorCode.ORIGIN_NOT_ALLOWED,
                    mime=None,
                    size=0,
                    data=None,
                    source="https://multimedia.nt.qq.com.cn",
                )
            return MediaResolution(
                status=ResolutionStatus.OK,
                error_code=None,
                mime="image/png",
                size=12,
                data=b"\x89PNG\r\n\x1a\nrest",
                source="data:image/png",
            )

    async def scenario():
        service, providers, _ = _service(tmp_path)
        downloaded = []

        async def fake_download(request, policy):
            downloaded.append((request, policy))
            return b"\x89PNG\r\n\x1a\nrest"

        service._download_milky_fake_ip_media = fake_download
        actions = FakeMilkyActions(
            {Capability.SEND_REPLY, Capability.RESOLVE_MEDIA}
        )
        event = _at_event(
            "这是什么",
            protocol="milky",
            extra_segments=(
                Segments.Image(
                    "https://multimedia.nt.qq.com.cn/download?token=secret"
                ),
            ),
        )

        assert await service.handle_fallback(event, actions)
        assert len(downloaded) == 1
        assert [
            request.kind for request, _, _ in actions.media_requests
        ] == [MediaSourceKind.REMOTE_URL, MediaSourceKind.DATA_URI]
        assert len(providers.calls[-1]["attachments"]) == 1
        assert providers.calls[-1]["attachments"][0].mime == "image/png"
        await service.shutdown()

    asyncio.run(scenario())


def test_milky_fake_ip_fallback_is_limited_to_official_https_origin():
    service = object.__new__(JianerAIService)
    key = SimpleNamespace(protocol="milky")
    rejected = MediaResolution(
        status=ResolutionStatus.REJECTED,
        error_code=ResolutionErrorCode.ORIGIN_NOT_ALLOWED,
        mime=None,
        size=0,
        data=None,
        source="https://multimedia.nt.qq.com.cn",
    )

    def request(locator: str) -> MediaRequest:
        return MediaRequest(
            kind=MediaSourceKind.REMOTE_URL,
            media_kind=MediaKind.IMAGE,
            locator=locator,
        )

    assert service._should_use_milky_fake_ip_fallback(
        key,
        request("https://multimedia.nt.qq.com.cn/download?token=secret"),
        rejected,
    )
    assert not service._should_use_milky_fake_ip_fallback(
        key,
        request("http://multimedia.nt.qq.com.cn/download"),
        rejected,
    )
    assert not service._should_use_milky_fake_ip_fallback(
        key,
        request("https://multimedia.nt.qq.com.cn.evil.test/download"),
        rejected,
    )
    assert not service._should_use_milky_fake_ip_fallback(
        SimpleNamespace(protocol="onebot"),
        request("https://multimedia.nt.qq.com.cn/download"),
        rejected,
    )


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
        assert "content 必须写成当前人设自己的第一人称主观回忆" in (
            providers.calls[-1]["message"]
        )
        assert "群内背景都不要输出" in providers.calls[-1]["message"]
        assert "当前完整人设模板" in providers.calls[-1]["message"]
        assert "你是简儿，正在和qq:42交流。" in (
            providers.calls[-1]["message"]
        )
        assert "工具：无" in providers.calls[-1]["message"]
        assert "严格按照输入中的完整人设模板" in (
            providers.calls[-1]["system_prompt"]
        )
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


def test_memory_generation_receives_the_complete_long_persona_template(
    tmp_path: Path,
):
    async def scenario():
        service, providers, _ = _service(
            tmp_path,
            answer='{"memories":[{"content":"我记得你喜欢蓝色。","weight":0.8}]}',
        )
        long_template = (
            "完整人设开头：你正在和{self.event_user}交流。\n"
            + ("这是不可省略的完整角色设定。" * 600)
            + "\n完整人设结尾：用户ID是{self.event_user_id}，口癖是哼哼。"
        )
        service.presets.upsert(
            key="LongPersona",
            name="长人设",
            info="完整长人设",
            template=long_template,
        )
        actions = FakeActions()
        event = _event("我长期喜欢蓝色", group_id=None)
        assert await service.switch_persona(event, actions, "LongPersona")
        await service.observe(event, actions)
        key = await service._conversation_key(event, actions)
        canonical = service._canonical_identity(event, actions)

        created = await service._generate_memories_now(
            canonical,
            key,
            force=True,
        )

        assert created == 1
        rendered = service._render_persona_template(
            key.preset,
            event_user=canonical,
            canonical=canonical,
        )
        assert len(rendered) > 6000
        assert rendered in providers.calls[-1]["message"]
        assert "完整人设开头" in providers.calls[-1]["message"]
        assert "完整人设结尾" in providers.calls[-1]["message"]
        await service.shutdown()

    asyncio.run(scenario())


def test_successful_dialogue_persists_and_reinjects_persona_episode(
    tmp_path: Path,
):
    async def scenario():
        service, providers, _ = _service(
            tmp_path,
            answer="好呀，我记得我们聊过这本书。",
        )
        actions = FakeActions()
        first = _event("我刚读完《银河系漫游指南》", group_id=None)
        assert await service.handle_fallback(first, actions)
        key = await service._conversation_key(first, actions)
        episodes = service.memory.query_conversation_episodes(
            preset=key.preset,
            protocol=key.protocol,
            self_id=key.self_id,
            conversation_kind=key.kind.value,
            conversation_id=key.conversation_id,
            query="银河系漫游指南",
        )
        assert [(item.user_content, item.assistant_content) for item in episodes] == [
            (
                "我刚读完《银河系漫游指南》",
                "好呀，我记得我们聊过这本书。",
            )
        ]

        with service._state_lock:
            service._histories.clear()
        providers.answer = "当然记得。"
        second = _event("我们上次聊的是哪本书？", group_id=None)
        assert await service.handle_fallback(second, actions)
        prompt = providers.calls[-1]["system_prompt"]
        assert "当前人设在这个会话里聊过的相关片段" in prompt
        assert "我刚读完《银河系漫游指南》" in prompt
        assert "好呀，我记得我们聊过这本书。" in prompt
        await service.shutdown()

    asyncio.run(scenario())


def test_successful_reply_records_outgoing_and_reviews_memory_exactly_once(
    tmp_path: Path,
):
    async def scenario():
        provider = MemoryReviewProviders(
            "我会记住的。",
            json.dumps(
                {
                    "decision": "apply",
                    "actions": [
                        {
                            "operation": "create",
                            "scope": "person",
                            "memory_id": None,
                            "canonical_fact": "用户长期喜欢蓝莓蛋糕",
                            "memory_text": "我记得你一直很喜欢蓝莓蛋糕。",
                            "importance": 0.8,
                            "confidence": 0.95,
                            "reason": "用户明确表达了长期偏好",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        )
        service, _, _ = _service(
            tmp_path,
            memory_review_enabled=True,
            provider=provider,
        )
        actions = FakeActions()
        event = _at_event("请记住，我长期喜欢蓝莓蛋糕")
        long_template = (
            "完整审查人设开头：你正在和{self.event_user}交流。\n"
            + ("这是回复后记忆审查不可省略的完整角色设定。" * 450)
            + "\n完整审查人设结尾：用户ID是{self.event_user_id}，口癖是哼。"
        )
        service.presets.upsert(
            key="ReviewPersona",
            name="审查长人设",
            info="完整审查长人设",
            template=long_template,
        )
        assert await service.switch_persona(
            event,
            actions,
            "ReviewPersona",
        )
        actions.sent.clear()
        await service.observe(event, actions)
        assert await service.handle_fallback(event, actions)
        while service._background_tasks:
            await asyncio.gather(
                *tuple(service._background_tasks),
                return_exceptions=True,
            )
            await asyncio.sleep(0)

        key = await service._conversation_key(event, actions)
        messages = service.memory.query_recent_chat(
            protocol=key.protocol,
            self_id=key.self_id,
            conversation_kind=key.kind.value,
            conversation_id=key.conversation_id,
            limit=10,
        )
        assert [item.direction for item in messages] == [
            "incoming",
            "outgoing",
        ]
        assert messages[-1].content == "我会记住的。"
        records = service.memory.list_memories(
            canonical_user_id="qq:42",
            preset=key.preset,
        )
        assert len(records) == 1
        assert records[0].canonical_fact == "用户长期喜欢蓝莓蛋糕"
        assert records[0].content == "我记得你一直很喜欢蓝莓蛋糕。"
        assert records[0].source_count == 1
        assert len(records[0].evidence) == 1
        episodes = service.memory.query_conversation_episodes(
            preset=key.preset,
            protocol=key.protocol,
            self_id=key.self_id,
            conversation_kind=key.kind.value,
            conversation_id=key.conversation_id,
            query="蓝莓蛋糕",
        )
        assert len(episodes) == 1
        assert episodes[0].review_state == "completed"
        assert provider.review_calls == 1
        review_payload = json.loads(provider.calls[-1]["message"])
        rendered = service._render_persona_template(
            key.preset,
            event_user="user-42",
            canonical="qq:42",
        )
        assert len(rendered) > 6000
        assert review_payload["persona_template"] == rendered
        assert "完整审查人设开头" in review_payload["persona_template"]
        assert "完整审查人设结尾" in review_payload["persona_template"]
        service._schedule_memory_review(key.preset, event.message_id)
        await asyncio.sleep(0.05)
        assert provider.review_calls == 1
        with sqlite3.connect(service.options.database_path) as conn:
            assert conn.execute(
                "SELECT status FROM job_memory_reviews"
            ).fetchone()[0] == "completed"
            assert conn.execute(
                "SELECT operation FROM audit_memory_actions"
            ).fetchone()[0] == "create"
        await service.shutdown()

    asyncio.run(scenario())


def test_send_failure_does_not_create_episode_or_review_job(tmp_path: Path):
    class FailingActions(FakeActions):
        async def send(self, message, **target):
            raise RuntimeError("send failed")

    async def scenario():
        provider = MemoryReviewProviders(
            "不会发送成功。",
            '{"decision":"no-op","actions":[]}',
        )
        service, _, _ = _service(
            tmp_path,
            memory_review_enabled=True,
            provider=provider,
        )
        actions = FailingActions()
        event = _at_event("这条回复会发送失败")
        await service.observe(event, actions)
        with pytest.raises(RuntimeError, match="send failed"):
            await service.handle_fallback(event, actions)
        key = await service._conversation_key(event, actions)
        assert service.memory.query_conversation_episodes(
            preset=key.preset,
            protocol=key.protocol,
            self_id=key.self_id,
            conversation_kind=key.kind.value,
            conversation_id=key.conversation_id,
        ) == ()
        with sqlite3.connect(service.options.database_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM job_memory_reviews"
            ).fetchone()[0] == 0
        assert provider.review_calls == 0
        await service.shutdown()

    asyncio.run(scenario())


def test_memory_review_updates_an_existing_person_memory(tmp_path: Path):
    async def scenario():
        service, _, _ = _service(tmp_path)
        canonical = service.memory.resolve_identity(
            "onebot", "bot-1", "42"
        )
        existing = service.memory.create_scoped_memory(
            scope="person",
            canonical_user_id=canonical,
            preset="Normal",
            canonical_fact="用户长期喜欢蓝莓蛋糕",
            content="我记得你一直喜欢蓝莓蛋糕。",
            importance=0.7,
            confidence=0.8,
        )
        await service.shutdown()

        provider = MemoryReviewProviders(
            "原来如此。",
            json.dumps(
                {
                    "decision": "apply",
                    "actions": [
                        {
                            "operation": "update",
                            "scope": "person",
                            "memory_id": str(existing.fact_id),
                            "canonical_fact": "用户长期喜欢草莓蛋糕",
                            "memory_text": "我记得你现在一直更喜欢草莓蛋糕。",
                            "importance": 0.85,
                            "confidence": 0.95,
                            "reason": "用户明确纠正了长期偏好",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        )
        service, _, _ = _service(
            tmp_path,
            memory_review_enabled=True,
            provider=provider,
        )
        actions = FakeActions()
        event = _at_event("纠正一下，我现在长期更喜欢草莓蛋糕")
        await service.observe(event, actions)
        assert await service.handle_fallback(event, actions)
        while service._background_tasks:
            await asyncio.gather(
                *tuple(service._background_tasks),
                return_exceptions=True,
            )
            await asyncio.sleep(0)

        records = service.memory.list_memories(
            canonical_user_id=canonical,
            preset="Normal",
        )
        assert len(records) == 1
        assert records[0].fact_id == existing.fact_id
        assert records[0].canonical_fact == "用户长期喜欢草莓蛋糕"
        assert records[0].content == "我记得你现在一直更喜欢草莓蛋糕。"
        assert records[0].source_count == 1
        with sqlite3.connect(service.options.database_path) as conn:
            assert conn.execute(
                "SELECT operation FROM audit_memory_actions"
            ).fetchone()[0] == "update"
        await service.shutdown()

    asyncio.run(scenario())


def test_failed_memory_review_recovers_after_service_restart(tmp_path: Path):
    async def scenario():
        failing_provider = MemoryReviewProviders(
            "我先回答你。",
            RuntimeError("temporary reviewer outage"),
        )
        service, _, _ = _service(
            tmp_path,
            memory_review_enabled=True,
            provider=failing_provider,
        )
        actions = FakeActions()
        event = _at_event("今天只是随便聊聊")
        await service.observe(event, actions)
        assert await service.handle_fallback(event, actions)
        while service._background_tasks:
            await asyncio.gather(
                *tuple(service._background_tasks),
                return_exceptions=True,
            )
            await asyncio.sleep(0)
        with sqlite3.connect(service.options.database_path) as conn:
            failed = conn.execute(
                "SELECT status, attempt_count, next_retry_at, last_error "
                "FROM job_memory_reviews"
            ).fetchone()
        assert failed[0] == "failed"
        assert failed[1] == 1
        assert failed[2] > 0
        assert "temporary reviewer outage" in failed[3]
        assert failing_provider.review_calls == 1
        await service.shutdown()

        # Simulate that the durable exponential-backoff deadline elapsed while
        # the process was offline, then construct a fresh service instance.
        with sqlite3.connect(tmp_path / "memory.db") as conn:
            conn.execute(
                "UPDATE job_memory_reviews SET next_retry_at = 0"
            )
        recovered_provider = MemoryReviewProviders(
            "不会再次生成主回复。",
            '{"decision":"no-op","actions":[]}',
        )
        recovered, _, _ = _service(
            tmp_path,
            memory_review_enabled=True,
            provider=recovered_provider,
        )
        await recovered._ensure_started()
        while recovered._background_tasks:
            await asyncio.gather(
                *tuple(recovered._background_tasks),
                return_exceptions=True,
            )
            await asyncio.sleep(0)
        with sqlite3.connect(recovered.options.database_path) as conn:
            completed = conn.execute(
                "SELECT status, attempt_count, last_error "
                "FROM job_memory_reviews"
            ).fetchone()
            operation = conn.execute(
                "SELECT operation FROM audit_memory_actions"
            ).fetchone()[0]
        assert completed == ("completed", 2, None)
        assert operation == "no-op"
        assert recovered_provider.review_calls == 1
        assert recovered_provider.calls[0]["history"] == ()
        await recovered.shutdown()

    asyncio.run(scenario())


def test_memory_review_cannot_recreate_a_user_deleted_memory(tmp_path: Path):
    async def scenario():
        service, _, _ = _service(tmp_path)
        canonical = service.memory.resolve_identity(
            "onebot", "bot-1", "42"
        )
        original = service.memory.create_scoped_memory(
            scope="person",
            canonical_user_id=canonical,
            preset="Normal",
            canonical_fact="用户长期喜欢蓝莓蛋糕",
            content="我记得你一直喜欢蓝莓蛋糕。",
        )
        assert original is not None
        assert service.memory.delete_memory(
            canonical_user_id=canonical,
            preset="Normal",
            memory_id=original.fact_id,
        )
        await service.shutdown()

        provider = MemoryReviewProviders(
            "聊点别的吧。",
            json.dumps(
                {
                    "decision": "apply",
                    "actions": [
                        {
                            "operation": "create",
                            "scope": "person",
                            "memory_id": None,
                            "canonical_fact": "用户长期喜欢蓝莓蛋糕",
                            "memory_text": "我记得你一直喜欢蓝莓蛋糕。",
                            "importance": 0.8,
                            "confidence": 0.9,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        )
        service, _, _ = _service(
            tmp_path,
            memory_review_enabled=True,
            provider=provider,
        )
        actions = FakeActions()
        event = _at_event("今天又提到一次蓝莓蛋糕")
        await service.observe(event, actions)
        assert await service.handle_fallback(event, actions)
        while service._background_tasks:
            await asyncio.gather(
                *tuple(service._background_tasks),
                return_exceptions=True,
            )
            await asyncio.sleep(0)

        assert service.memory.list_memories(
            canonical_user_id=canonical,
            preset="Normal",
        ) == ()
        assert len(
            service.memory.list_suppressions(
                canonical_user_id=canonical,
                preset="Normal",
            )
        ) == 1
        with sqlite3.connect(service.options.database_path) as conn:
            audit = conn.execute(
                "SELECT status, error_code FROM audit_memory_actions"
            ).fetchone()
            job_status = conn.execute(
                "SELECT status FROM job_memory_reviews"
            ).fetchone()[0]
        assert audit == ("suppressed", "deleted_tombstone")
        assert job_status == "completed"
        await service.shutdown()

    asyncio.run(scenario())


def test_disabling_external_review_context_does_not_leave_pending_jobs(
    tmp_path: Path,
):
    async def scenario():
        service, provider, _ = _service(
            tmp_path,
            memory_review_enabled=False,
        )
        actions = FakeActions()
        event = _at_event("这轮不允许发送历史给审查模型")
        await service.observe(event, actions)
        assert await service.handle_fallback(event, actions)
        key = await service._conversation_key(event, actions)
        episodes = service.memory.query_conversation_episodes(
            preset=key.preset,
            protocol=key.protocol,
            self_id=key.self_id,
            conversation_kind=key.kind.value,
            conversation_id=key.conversation_id,
        )
        assert len(episodes) == 1
        assert episodes[0].review_state == "completed"
        with sqlite3.connect(service.options.database_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM job_memory_reviews"
            ).fetchone()[0] == 0
        assert len(provider.calls) == 1
        await service.shutdown()

    asyncio.run(scenario())


def test_memory_review_parser_rejects_invalid_scope_ids_and_sensitive_data():
    parse = JianerAIService._parse_memory_review_actions
    allowed = {"person": {"7"}, "group": {"9"}}
    assert parse(
        '{"decision":"no-op","actions":[]}',
        allowed_ids=allowed,
        group_allowed=True,
    ) == ()
    with pytest.raises(ValueError, match="raw JSON"):
        parse(
            '```json\n{"decision":"no-op","actions":[]}\n```',
            allowed_ids=allowed,
            group_allowed=True,
        )
    with pytest.raises(ValueError, match="one to three"):
        parse(
            json.dumps(
                {
                    "decision": "apply",
                    "actions": [
                        {
                            "operation": "create",
                            "scope": "person",
                            "canonical_fact": f"稳定事实 {index}",
                            "memory_text": f"我记得稳定事实 {index}。",
                        }
                        for index in range(4)
                    ],
                }
            ),
            allowed_ids=allowed,
            group_allowed=True,
        )
    with pytest.raises(ValueError, match="invented"):
        parse(
            json.dumps(
                {
                    "decision": "apply",
                    "actions": [
                        {
                            "operation": "update",
                            "scope": "person",
                            "memory_id": "404",
                            "canonical_fact": "用户喜欢蛋糕",
                            "memory_text": "我记得你喜欢蛋糕。",
                        }
                    ],
                }
            ),
            allowed_ids=allowed,
            group_allowed=True,
        )
    with pytest.raises(ValueError, match="private"):
        parse(
            json.dumps(
                {
                    "decision": "apply",
                    "actions": [
                        {
                            "operation": "create",
                            "scope": "group",
                            "canonical_fact": "群约定周五见",
                            "memory_text": "我记得大家约好周五见。",
                        }
                    ],
                }
            ),
            allowed_ids=allowed,
            group_allowed=False,
        )
    with pytest.raises(ValueError, match="sensitive"):
        parse(
            json.dumps(
                {
                    "decision": "apply",
                    "actions": [
                        {
                            "operation": "create",
                            "scope": "person",
                            "canonical_fact": "用户 token 是 sk-abcdefghijk12345",
                            "memory_text": "我记得你的 token。",
                        }
                    ],
                }
            ),
            allowed_ids=allowed,
            group_allowed=True,
        )
    with pytest.raises(ValueError, match="sensitive"):
        parse(
            json.dumps(
                {
                    "decision": "apply",
                    "actions": [
                        {
                            "operation": "create",
                            "scope": "person",
                            "canonical_fact": "用户密码是 123456",
                            "memory_text": "我记得你的密码。",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            allowed_ids=allowed,
            group_allowed=True,
        )


def test_group_prompt_reads_only_current_persona_and_current_group_memory(
    tmp_path: Path,
):
    async def scenario():
        service, providers, _ = _service(tmp_path)
        actions = FakeActions()
        canonical = service.memory.resolve_identity("onebot", "bot-1", "42")
        service.memory.create_group_memory(
            preset="Normal",
            protocol="onebot",
            self_id="bot-1",
            group_id="100",
            canonical_user_id=canonical,
            content="我记得这个群约好每周五看电影呀。",
        )
        service.memory.create_group_memory(
            preset="Normal",
            protocol="onebot",
            self_id="bot-1",
            group_id="200",
            canonical_user_id=canonical,
            content="我记得另一个群只在周日聊天。",
        )
        service.memory.create_group_memory(
            preset="role",
            protocol="onebot",
            self_id="bot-1",
            group_id="100",
            canonical_user_id=canonical,
            content="这是另一个人设对同群的记忆。",
        )

        await service.observe(
            _event("这个群刚刚在聊草莓蛋糕", group_id="100"),
            actions,
        )
        await service.observe(
            _event("另一个群刚刚在聊机密项目", group_id="200"),
            actions,
        )
        event = _at_event("我们什么时候看电影？", group_id="100")
        assert await service.handle_fallback(event, actions)
        prompt = providers.calls[-1]["system_prompt"]
        assert "[scope=group memory_id=" in prompt
        assert "我记得这个群约好每周五看电影呀。" in prompt
        assert "我记得另一个群只在周日聊天。" not in prompt
        assert "这是另一个人设对同群的记忆。" not in prompt
        assert "这个群刚刚在聊草莓蛋糕" in prompt
        assert "另一个群刚刚在聊机密项目" not in prompt
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
        created = next(
            item
            for item in service.presets.list_presets()
            if item.name == "新角色"
        )
        assert created.key in {
            item.preset for item in service.memory.list_persona_partitions()
        }
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
        assert "用户在当前消息中明确要求记住某件事" in system_prompt
        assert "普通对话不要为了后台整理而主动调用写记忆工具" in system_prompt
        assert "回复成功后系统会另行执行独立记忆审查" in system_prompt
        assert "read_recent_chat" in system_prompt
        assert "search_current_chat" in system_prompt
        assert isinstance(providers.requests[1].turns[0], AssistantTurn)
        tool_result = providers.requests[1].turns[1]
        assert isinstance(tool_result, ToolResultTurn)
        assert json.loads(tool_result.content)["data"]["result"] == 42

        key = await service._conversation_key(event, actions)
        assert service._histories[key] == [
            {
                "role": "user",
                "content": providers.requests[0].message,
            },
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


def test_group_ai_prefers_card_and_labels_users_in_shared_history(
    tmp_path: Path,
):
    async def scenario():
        service, providers, _ = _service(tmp_path)
        actions = FakeActions({Capability.SEND_REPLY})
        first = _at_event("查一下深圳盐田区天气", user_id="42")
        first.sender = SimpleNamespace(
            nickname="成员甲QQ昵称",
            card="成员甲群名片",
            user_id="42",
        )
        second = _at_event("再查一下上海天气", user_id="77")
        second.sender = SimpleNamespace(
            nickname="成员乙QQ昵称",
            card="成员乙群名片",
            user_id="77",
        )

        assert await service.handle_fallback(first, actions)
        first_call = providers.calls[-1]
        assert "正在和成员甲群名片交流" in first_call["system_prompt"]
        assert "正在和成员甲QQ昵称交流" not in first_call["system_prompt"]
        assert '"display_name":"成员甲群名片"' in first_call["message"]
        assert '"user_id":"42"' in first_call["message"]
        assert '"canonical_user_id":"qq:42"' in first_call["message"]
        key = await service._conversation_key(second, actions)
        tool_context = service._tool_context(
            second,
            actions,
            key,
            "qq:77",
        )
        assert tool_context.history[0]["content"] == first_call["message"]

        assert await service.handle_fallback(second, actions)
        second_call = providers.calls[-1]
        assert "正在和成员乙群名片交流" in second_call["system_prompt"]
        assert "正在和成员甲群名片交流" not in second_call["system_prompt"]
        assert '"display_name":"成员乙群名片"' in second_call["message"]
        assert '"user_id":"77"' in second_call["message"]
        assert '"canonical_user_id":"qq:77"' in second_call["message"]
        assert second_call["history"][0] == {
            "role": "user",
            "content": first_call["message"],
        }
        assert '"display_name":"成员甲群名片"' in (
            second_call["history"][0]["content"]
        )
        assert "必须按其中的 user_id 和 canonical_user_id 区分不同成员" in (
            second_call["system_prompt"]
        )
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
        sent_texts = [
            "".join(
                str(getattr(segment, "text", ""))
                for segment in message
                if isinstance(segment, Segments.Text)
            )
            for _, message in actions.sent
        ]
        assert sent_texts == [
            "天气",
            "多云\n和风天气（https://www.qweather.com）\n请带伞",
        ]
        sent_text = "\n\n".join(sent_texts)
        assert sent_text == expected
        for marker in ("# ", "- ", "**", "```", "]("):
            assert marker not in sent_text
        await service.shutdown()

    asyncio.run(scenario())


def test_ai_reply_sends_up_to_five_paragraphs_separately_without_forwarding(
    tmp_path: Path,
):
    class ParagraphActions(FakeActions):
        def __init__(self):
            super().__init__({Capability.SEND_REPLY})
            self.forward_calls = []

        async def send_group_forward_msg(self, **kwargs):
            self.forward_calls.append(kwargs)

        async def send_forward_msg(self, **kwargs):
            self.forward_calls.append(kwargs)

    async def scenario():
        answer = (
            "第一段第一行\n第一段第二行   \n\n"
            "第二段\n\n第三段\n\n第四段\n\n第五段   \n\n"
        )
        service, _, _ = _service(tmp_path, answer=answer)
        event = _at_event("请分段回答")
        actions = ParagraphActions()

        assert await service.handle_fallback(event, actions)
        assert len(actions.sent) == 5
        text_parts = [
            [
                str(getattr(segment, "text", ""))
                for segment in message
                if isinstance(segment, Segments.Text)
            ]
            for _, message in actions.sent
        ]
        assert text_parts == [
            ["第一段第一行\n第一段第二行"],
            ["第二段"],
            ["第三段"],
            ["第四段"],
            ["第五段"],
        ]
        assert any(
            isinstance(segment, Segments.Reply)
            for segment in actions.sent[0][1]
        )
        assert all(
            not any(
                isinstance(segment, Segments.Reply)
                for segment in message
            )
            for _, message in actions.sent[1:]
        )
        assert actions.forward_calls == []
        await service.shutdown()

    asyncio.run(scenario())


def test_ai_reply_over_five_paragraphs_sends_only_group_forward(
    tmp_path: Path,
):
    class ForwardActions(FakeActions):
        def __init__(self):
            super().__init__(
                {
                    Capability.SEND_REPLY,
                    Capability.NATIVE_GROUP_FORWARD,
                }
            )
            self.forward_calls = []

        async def send_group_forward_msg(self, **kwargs):
            self.forward_calls.append(kwargs)

    async def scenario():
        answer = "\n\n".join(f"第{index}段" for index in range(1, 7))
        service, _, _ = _service(tmp_path, answer=answer)
        event = _at_event("请详细回答")
        actions = ForwardActions()

        assert await service.handle_fallback(event, actions)
        assert actions.sent == []
        assert len(actions.forward_calls) == 1
        call = actions.forward_calls[0]
        assert call["group_id"] == event.group_id
        nodes = list(call["message"])
        assert len(nodes) == 6
        assert all(isinstance(node, Segments.CustomNode) for node in nodes)
        assert [
            node.to_json()["data"]["content"][0]["data"]["text"]
            for node in nodes
        ] == [f"第{index}段" for index in range(1, 7)]
        assert all(
            node.to_json()["data"]["user_id"] == event.self_id
            for node in nodes
        )
        assert all(
            node.to_json()["data"]["nick_name"] == "简儿"
            for node in nodes
        )
        await service.shutdown()

    asyncio.run(scenario())


def test_ai_reply_over_five_paragraphs_without_native_forward_sends_one_text(
    tmp_path: Path,
):
    async def scenario():
        paragraphs = [f"第{index}段" for index in range(1, 7)]
        service, _, _ = _service(
            tmp_path,
            answer="\n\n".join(paragraphs),
        )
        event = _at_event("请详细回答")
        actions = FakeActions({Capability.SEND_REPLY})

        assert await service.handle_fallback(event, actions)
        assert len(actions.sent) == 1
        sent_segments = list(actions.sent[0][1])
        assert isinstance(sent_segments[0], Segments.Reply)
        assert isinstance(sent_segments[1], Segments.Text)
        assert sent_segments[1].text == "\n\n".join(paragraphs)
        await service.shutdown()

    asyncio.run(scenario())
