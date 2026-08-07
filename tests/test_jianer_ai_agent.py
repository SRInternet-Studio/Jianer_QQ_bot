from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from jianer.adapters import ConversationKey, ConversationKind

from plugins.JianerAI.agent import AgentOptions, AgentRunner
from plugins.JianerAI.memory import (
    JianerMemoryStore,
    MemoryMigrationRequiredError,
    SCHEMA_VERSION,
)
from plugins.JianerAI.providers import (
    AssistantTurn,
    ProviderRegistry,
    ProviderResponse,
    ProviderToolCall,
    ToolResultTurn,
    ToolsUnsupportedError,
    ProviderRequestError,
)
from plugins.JianerAI.tools import (
    BUILTIN_MUTATING_TOOL_NAMES,
    ToolCall,
    ToolContext,
    ToolRegistry,
    ToolRisk,
    ToolSpec,
    register_builtin_tools,
)


class FakeMemory:
    def __init__(self):
        self.calls = []
        self.write_calls = []

    def list_memories(self, **kwargs):
        self.calls.append(kwargs)
        return (
            SimpleNamespace(fact_id=7, content="用户喜欢蓝色", weight=0.8),
        )

    def create_memory(self, **kwargs):
        self.write_calls.append(("create", kwargs))
        return SimpleNamespace(
            fact_id=8,
            content=kwargs["content"],
            weight=1.0,
            outcome="inserted",
        )

    def update_memory(self, **kwargs):
        self.write_calls.append(("update", kwargs))
        if str(kwargs["memory_id"]) == "404":
            return None
        return SimpleNamespace(
            fact_id=int(kwargs["memory_id"]),
            content=kwargs["content"],
            weight=1.0,
            outcome="updated",
        )


class FakeActions:
    protocol = "onebot"
    capabilities = frozenset()

    def __init__(self):
        self.profile_ids = []
        self.group_ids = []

    async def get_stranger_info(self, user_id):
        self.profile_ids.append(user_id)
        return SimpleNamespace(
            data=SimpleNamespace(
                user_id=user_id,
                nickname="适配器昵称",
                sex="unknown",
                age=0,
            )
        )

    async def get_group_info(self, group_id):
        self.group_ids.append(group_id)
        return SimpleNamespace(
            data=SimpleNamespace(
                group_id=group_id,
                group_name="测试群",
                member_count=12,
                max_member_count=200,
            )
        )


class RecordingLogger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(str(message))


def _context(protocol="onebot"):
    actions = FakeActions()
    actions.protocol = protocol
    memory = FakeMemory()
    event = SimpleNamespace(
        protocol=protocol,
        self_id="bot-1",
        user_id="user-42",
        group_id="group-100",
        sender={"nickname": "事件昵称", "card": "群名片"},
    )
    key = ConversationKey(
        protocol=protocol,
        self_id="bot-1",
        kind=ConversationKind.GROUP,
        conversation_id="group-100",
        preset="Normal",
    )
    return (
        ToolContext(
            event=event,
            actions=actions,
            conversation=key,
            canonical_user_id="qq:user-42",
            runtime={},
            memory=memory,
        ),
        actions,
        memory,
    )


def _decode(result):
    return json.loads(result.content)


def test_builtin_tools_are_current_context_scoped_across_adapters():
    async def scenario(protocol):
        registry = ToolRegistry()
        register_builtin_tools(registry)
        context, actions, memory = _context(protocol)

        profile = await registry.execute(
            ToolCall("profile", "get_current_user_profile", {}), context
        )
        chat = await registry.execute(
            ToolCall("chat", "get_current_chat_info", {}), context
        )
        memories = await registry.execute(
            ToolCall("memory", "list_my_memories", {"limit": 3}), context
        )

        assert _decode(profile)["data"]["nickname"] == "适配器昵称"
        assert _decode(chat)["data"]["group_name"] == "测试群"
        assert _decode(memories)["data"]["memories"][0]["id"] == "7"
        assert actions.profile_ids == ["user-42"]
        assert actions.group_ids == ["group-100"]
        assert all(isinstance(item, str) for item in actions.profile_ids + actions.group_ids)
        assert memory.calls == [
            {
                "canonical_user_id": "qq:user-42",
                "preset": "Normal",
                "limit": 3,
            }
        ]

    for protocol in ("onebot", "milky", "feishu"):
        asyncio.run(scenario(protocol))


def test_builtin_memory_writes_are_explicitly_admitted_and_context_scoped():
    async def scenario(protocol):
        registry = ToolRegistry(
            allowed_risks=frozenset(
                {ToolRisk.READ_ONLY, ToolRisk.MUTATING}
            ),
            allowed_mutating_tools=BUILTIN_MUTATING_TOOL_NAMES,
        )
        register_builtin_tools(registry)
        context, _, memory = _context(protocol)

        created = await registry.execute(
            ToolCall(
                "create",
                "create_my_memory",
                {"content": "用户喜欢蓝莓蛋糕"},
            ),
            context,
        )
        updated = await registry.execute(
            ToolCall(
                "update",
                "update_my_memory",
                {"memory_id": "8", "content": "用户喜欢草莓蛋糕"},
            ),
            context,
        )
        missing = await registry.execute(
            ToolCall(
                "missing",
                "update_my_memory",
                {"memory_id": "404", "content": "不存在"},
            ),
            context,
        )
        invalid = await registry.execute(
            ToolCall(
                "invalid",
                "update_my_memory",
                {"memory_id": "not-an-id", "content": "无效"},
            ),
            context,
        )

        assert _decode(created)["data"] == {
            "preset": "Normal",
            "scope": "person",
            "status": "inserted",
            "memory": {
                "id": "8",
                "content": "用户喜欢蓝莓蛋糕",
                "weight": 1.0,
            },
        }
        assert _decode(updated)["data"]["memory"] == {
            "id": "8",
            "content": "用户喜欢草莓蛋糕",
            "weight": 1.0,
        }
        assert missing.error_code == "memory_not_found"
        assert invalid.error_code == "invalid_memory_id"
        assert memory.write_calls == [
            (
                "create",
                {
                    "canonical_user_id": "qq:user-42",
                    "preset": "Normal",
                    "content": "用户喜欢蓝莓蛋糕",
                },
            ),
            (
                "update",
                {
                    "canonical_user_id": "qq:user-42",
                    "preset": "Normal",
                    "memory_id": "8",
                    "content": "用户喜欢草莓蛋糕",
                },
            ),
            (
                "update",
                {
                    "canonical_user_id": "qq:user-42",
                    "preset": "Normal",
                    "memory_id": "404",
                    "content": "不存在",
                },
            ),
        ]

    for protocol in ("onebot", "milky", "feishu"):
        asyncio.run(scenario(protocol))


def test_builtin_memory_tools_route_group_scope_to_current_group_only(
    tmp_path: Path,
):
    async def scenario():
        store = JianerMemoryStore(tmp_path / "group-tools.db")
        canonical = store.resolve_identity("onebot", "bot-1", "user-42")
        base_context, actions, _ = _context("onebot")
        context = ToolContext(
            event=base_context.event,
            actions=actions,
            conversation=base_context.conversation,
            canonical_user_id=canonical,
            runtime={},
            memory=store,
        )
        registry = ToolRegistry(
            allowed_risks=frozenset(
                {ToolRisk.READ_ONLY, ToolRisk.MUTATING}
            ),
            allowed_mutating_tools=BUILTIN_MUTATING_TOOL_NAMES,
        )
        register_builtin_tools(registry)

        created = await registry.execute(
            ToolCall(
                "create-group",
                "create_my_memory",
                {
                    "scope": "group",
                    "content": "我记得这个群周五一起看电影呀。",
                },
            ),
            context,
        )
        created_data = _decode(created)["data"]
        memory_id = created_data["memory"]["id"]
        assert created_data["scope"] == "group"

        listed = await registry.execute(
            ToolCall(
                "list-group",
                "list_my_memories",
                {"scope": "group", "limit": 5},
            ),
            context,
        )
        assert _decode(listed)["data"]["memories"] == [
            {
                "id": memory_id,
                "scope": "group",
                "content": "我记得这个群周五一起看电影呀。",
                "weight": 1.0,
                "canonical_fact": "我记得这个群周五一起看电影呀。",
                "confidence": 1.0,
                "source_count": 1,
            }
        ]

        updated = await registry.execute(
            ToolCall(
                "update-group",
                "update_my_memory",
                {
                    "scope": "group",
                    "memory_id": memory_id,
                    "content": "我记得这个群改成周六一起看电影啦。",
                },
            ),
            context,
        )
        assert _decode(updated)["data"]["memory"]["content"] == (
            "我记得这个群改成周六一起看电影啦。"
        )
        assert store.list_group_memories(
            preset="Normal",
            protocol="onebot",
            self_id="bot-1",
            group_id="another-group",
        ) == ()

        private_context = ToolContext(
            event=SimpleNamespace(
                protocol="onebot",
                self_id="bot-1",
                user_id="user-42",
            ),
            actions=actions,
            conversation=ConversationKey(
                protocol="onebot",
                self_id="bot-1",
                kind=ConversationKind.PRIVATE,
                conversation_id="user-42",
                preset="Normal",
            ),
            canonical_user_id=canonical,
            runtime={},
            memory=store,
        )
        denied = await registry.execute(
            ToolCall(
                "private-group",
                "create_my_memory",
                {"scope": "group", "content": "不能写入"},
            ),
            private_context,
        )
        assert denied.error_code == "group_memory_requires_group_chat"

    asyncio.run(scenario())


def test_builtin_memory_tools_preserve_canonical_and_persona_styled_text(
    tmp_path: Path,
):
    async def scenario():
        store = JianerMemoryStore(tmp_path / "styled-memory-tools.db")
        canonical = store.resolve_identity("onebot", "bot-1", "user-42")
        base_context, actions, _ = _context("onebot")
        context = ToolContext(
            event=base_context.event,
            actions=actions,
            conversation=base_context.conversation,
            canonical_user_id=canonical,
            runtime={},
            memory=store,
        )
        registry = ToolRegistry(
            allowed_risks=frozenset(
                {ToolRisk.READ_ONLY, ToolRisk.MUTATING}
            ),
            allowed_mutating_tools=BUILTIN_MUTATING_TOOL_NAMES,
        )
        register_builtin_tools(registry, include_web_browser=False)

        created = await registry.execute(
            ToolCall(
                "styled-create",
                "create_my_memory",
                {
                    "canonical_fact": "用户长期喜欢蓝莓蛋糕",
                    "memory_text": "我会好好记着，你一直喜欢蓝莓蛋糕。",
                    "importance": 0.75,
                    "confidence": 0.9,
                },
            ),
            context,
        )
        memory_id = _decode(created)["data"]["memory"]["id"]
        listed = await registry.execute(
            ToolCall(
                "styled-list",
                "list_my_memories",
                {"scope": "person", "limit": 5},
            ),
            context,
        )
        first = _decode(listed)["data"]["memories"][0]
        assert first["id"] == memory_id
        assert first["canonical_fact"] == "用户长期喜欢蓝莓蛋糕"
        assert first["content"] == "我会好好记着，你一直喜欢蓝莓蛋糕。"
        assert first["confidence"] == pytest.approx(0.9)
        assert first["source_count"] == 1

        updated = await registry.execute(
            ToolCall(
                "styled-update",
                "update_my_memory",
                {
                    "memory_id": memory_id,
                    "canonical_fact": "用户长期喜欢草莓蛋糕",
                    "memory_text": "我已经改好啦，你现在一直更喜欢草莓蛋糕。",
                    "importance": 0.8,
                    "confidence": 0.95,
                },
            ),
            context,
        )
        assert _decode(updated)["data"]["memory"]["id"] == memory_id
        records = store.list_memories(
            canonical_user_id=canonical,
            preset="Normal",
        )
        assert len(records) == 1
        assert records[0].canonical_fact == "用户长期喜欢草莓蛋糕"
        assert records[0].content == "我已经改好啦，你现在一直更喜欢草莓蛋糕。"
        assert records[0].confidence == pytest.approx(0.95)
        assert records[0].source_count == 2
        await registry.shutdown()

    asyncio.run(scenario())


def test_recent_chat_tools_are_locked_to_the_current_conversation(
    tmp_path: Path,
):
    async def scenario():
        store = JianerMemoryStore(tmp_path / "chat-tools.db")
        canonical = store.resolve_identity("onebot", "bot-1", "user-42")
        store.record_transcript(
            protocol="onebot",
            self_id="bot-1",
            conversation_kind="group",
            conversation_id="group-100",
            message_id="current-1",
            sender_canonical_id=canonical,
            sender_name="当前用户",
            content="当前群正在聊蓝莓蛋糕",
            preset="Normal",
        )
        store.record_transcript(
            protocol="onebot",
            self_id="bot-1",
            conversation_kind="group",
            conversation_id="group-200",
            message_id="other-1",
            sender_canonical_id=canonical,
            sender_name="另一个群的用户",
            content="另一个群的秘密内容",
            preset="Normal",
        )
        base_context, actions, _ = _context("onebot")
        context = ToolContext(
            event=base_context.event,
            actions=actions,
            conversation=base_context.conversation,
            canonical_user_id=canonical,
            runtime={},
            memory=store,
        )
        registry = ToolRegistry()
        register_builtin_tools(registry, include_web_browser=False)

        recent = await registry.execute(
            ToolCall("recent", "read_recent_chat", {"limit": 100}),
            context,
        )
        recent_data = _decode(recent)["data"]
        assert recent_data["scope"] == "current_chat"
        assert [item["text"] for item in recent_data["messages"]] == [
            "当前群正在聊蓝莓蛋糕"
        ]
        searched = await registry.execute(
            ToolCall(
                "search",
                "search_current_chat",
                {"query": "蓝莓", "limit": 10},
            ),
            context,
        )
        assert [
            item["text"]
            for item in _decode(searched)["data"]["messages"]
        ] == ["当前群正在聊蓝莓蛋糕"]
        escaped = await registry.execute(
            ToolCall(
                "escape",
                "read_recent_chat",
                {"conversation_id": "group-200"},
            ),
            context,
        )
        assert escaped.error_code == "invalid_arguments"
        await registry.shutdown()

    asyncio.run(scenario())


def test_mutating_tool_admission_does_not_open_other_plugin_writes():
    context, _, _ = _context()
    default_registry = ToolRegistry()
    register_builtin_tools(default_registry)
    default_names = {item.name for item in default_registry.available(context)}
    assert BUILTIN_MUTATING_TOOL_NAMES.isdisjoint(default_names)

    registry = ToolRegistry(
        allowed_risks=frozenset({ToolRisk.READ_ONLY, ToolRisk.MUTATING}),
        allowed_mutating_tools=BUILTIN_MUTATING_TOOL_NAMES,
    )
    register_builtin_tools(registry)
    registry.register(
        ToolSpec(
            name="unlisted_plugin_write",
            description="must remain unavailable",
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            handler=lambda *_: "should not run",
            risk=ToolRisk.MUTATING,
        )
    )
    names = {item.name for item in registry.available(context)}
    assert BUILTIN_MUTATING_TOOL_NAMES.issubset(names)
    assert "unlisted_plugin_write" not in names
    denied = asyncio.run(
        registry.execute(
            ToolCall("write", "unlisted_plugin_write", {}),
            context,
        )
    )
    assert denied.error_code == "tool_not_allowed"


def test_tool_registry_validates_schema_risk_timeout_and_calculator():
    async def slow(context, arguments):
        await asyncio.sleep(0.05)
        return "late"

    async def scenario():
        registry = ToolRegistry()
        register_builtin_tools(registry)
        context, _, _ = _context()

        result = await registry.execute(
            ToolCall("calc", "calculate_expression", {"expression": "(2 + 3) * 4"}),
            context,
        )
        assert _decode(result)["data"]["result"] == 20

        unsafe = registry.register(
            ToolSpec(
                name="unsafe_action",
                description="not exposed",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                handler=lambda *_: "no",
                risk=ToolRisk.MUTATING,
            )
        )
        assert "unsafe_action" not in {item.name for item in registry.available(context)}
        denied = await registry.execute(ToolCall("unsafe", "unsafe_action", {}), context)
        assert denied.error_code == "tool_not_allowed"
        assert registry.unregister(unsafe)

        registry.register(
            ToolSpec(
                name="slow_tool",
                description="times out",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                handler=slow,
                timeout_seconds=0.01,
            )
        )
        timed_out = await registry.execute(ToolCall("slow", "slow_tool", {}), context)
        assert timed_out.error_code == "tool_timeout"
        invalid = await registry.execute(
            ToolCall("bad", "calculate_expression", {"expression": "__import__('os')"}),
            context,
        )
        assert invalid.error_code == "tool_failed"
        unknown = await registry.execute(
            ToolCall("missing", "does_not_exist", {}), context
        )
        assert unknown.error_code == "unknown_tool"

        registry.register(
            ToolSpec(
                name="large_result",
                description="large result",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                handler=lambda *_: "x" * 2000,
                max_output_chars=256,
            )
        )
        large = await registry.execute(
            ToolCall("large", "large_result", {}), context
        )
        assert _decode(large)["truncated"] is True

    asyncio.run(scenario())

    registry = ToolRegistry()
    with pytest.raises(ValueError, match="unsupported schema keywords"):
        registry.register(
            ToolSpec(
                name="bad_schema",
                description="bad",
                input_schema={"type": "object", "oneOf": []},
                handler=lambda *_: None,
            )
        )


def test_tool_registry_shutdown_cancels_inflight_handlers():
    async def scenario():
        started = asyncio.Event()

        async def wait_forever(context, arguments):
            started.set()
            await asyncio.Event().wait()

        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="wait_forever",
                description="waits until shutdown",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                handler=wait_forever,
                timeout_seconds=30,
            )
        )
        context, _, _ = _context()
        execution = asyncio.create_task(
            registry.execute(ToolCall("wait", "wait_forever", {}), context)
        )
        await started.wait()
        await registry.shutdown()
        assert execution.cancelled() or isinstance(
            (await asyncio.gather(execution, return_exceptions=True))[0],
            asyncio.CancelledError,
        )
        assert registry.available(context) == ()

    asyncio.run(scenario())


def test_tool_registry_shutdown_invokes_each_async_tool_cleanup_once():
    async def scenario():
        calls = []

        async def cleanup():
            calls.append("closed")

        registry = ToolRegistry()
        for name in ("first_cleanup", "second_cleanup"):
            registry.register(
                ToolSpec(
                    name=name,
                    description="cleanup probe",
                    input_schema={
                        "type": "object",
                        "properties": {},
                        "required": [],
                        "additionalProperties": False,
                    },
                    handler=lambda *_: None,
                    shutdown=cleanup,
                )
            )
        await registry.shutdown()
        await registry.shutdown()
        assert calls == ["closed"]

    asyncio.run(scenario())


class SequenceProvider:
    def __init__(self):
        self.requests = []
        self.chat_calls = []
        self.marked = []

    def supports_tools(self, model):
        return True

    async def complete_request(self, model, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            call = ProviderToolCall("call-1", "calculate_expression", {"expression": "6*7"})
            turn = AssistantTurn(tool_calls=(call,))
            return ProviderResponse("", (call,), turn)
        return ProviderResponse("结果是 42。", (), AssistantTurn(text="结果是 42。"))

    async def chat(self, model, message, **kwargs):
        self.chat_calls.append((model, message, kwargs))
        return "普通回答"

    def mark_tools_unsupported(self, model):
        self.marked.append(model)


def test_agent_runner_executes_native_tool_loop_and_keeps_structured_turns():
    async def scenario():
        provider = SequenceProvider()
        registry = ToolRegistry()
        register_builtin_tools(registry)
        context, _, _ = _context()
        logger = RecordingLogger()
        runner = AgentRunner(provider, registry, logger=logger)

        answer = await runner.run(
            model="model-a",
            message="六乘七是多少",
            history=(),
            system_prompt="test",
            attachments=(),
            context=context,
            enabled=True,
        )

        assert answer == "结果是 42。"
        assert len(provider.requests) == 2
        assert provider.requests[0].tools
        assert isinstance(provider.requests[1].turns[0], AssistantTurn)
        tool_result = provider.requests[1].turns[1]
        assert isinstance(tool_result, ToolResultTurn)
        assert json.loads(tool_result.content)["data"]["result"] == 42
        assert provider.chat_calls == []
        logs = "\n".join(logger.messages)
        assert "JianerAI tool call 开始" in logs
        assert "JianerAI tool call 完成" in logs
        assert '"tool":"calculate_expression"' in logs
        assert '"expression":"6*7"' in logs
        assert '"result":42' in logs

    asyncio.run(scenario())


def test_agent_runner_can_create_then_update_current_scoped_memory(
    tmp_path: Path,
):
    class MemorySequenceProvider(SequenceProvider):
        async def complete_request(self, model, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                call = ProviderToolCall(
                    "create-memory",
                    "create_my_memory",
                    {"content": "用户喜欢蓝莓蛋糕"},
                )
                return ProviderResponse(
                    "",
                    (call,),
                    AssistantTurn(tool_calls=(call,)),
                )
            if len(self.requests) == 2:
                created = json.loads(request.turns[-1].content)
                call = ProviderToolCall(
                    "update-memory",
                    "update_my_memory",
                    {
                        "memory_id": created["data"]["memory"]["id"],
                        "content": "用户喜欢草莓蛋糕",
                    },
                )
                return ProviderResponse(
                    "",
                    (call,),
                    AssistantTurn(tool_calls=(call,)),
                )
            return ProviderResponse(
                "已经更新记忆。",
                (),
                AssistantTurn(text="已经更新记忆。"),
            )

    async def scenario():
        provider = MemorySequenceProvider()
        registry = ToolRegistry(
            allowed_risks=frozenset(
                {ToolRisk.READ_ONLY, ToolRisk.MUTATING}
            ),
            allowed_mutating_tools=BUILTIN_MUTATING_TOOL_NAMES,
        )
        register_builtin_tools(
            registry,
            include_web_browser=False,
            project_root=tmp_path,
        )
        base_context, actions, _ = _context()
        store = JianerMemoryStore(tmp_path / "memory-tools.db")
        canonical = store.resolve_identity("onebot", "bot-1", "user-42")
        context = ToolContext(
            event=base_context.event,
            actions=actions,
            conversation=base_context.conversation,
            canonical_user_id=canonical,
            runtime={},
            memory=store,
        )

        answer = await AgentRunner(provider, registry).run(
            model="model-a",
            message="请先记住我喜欢蓝莓蛋糕，然后改成草莓蛋糕",
            history=(),
            system_prompt="test",
            attachments=(),
            context=context,
            enabled=True,
        )

        assert answer == "已经更新记忆。"
        assert len(provider.requests) == 3
        records = store.list_memories(
            canonical_user_id=canonical,
            preset="Normal",
        )
        assert len(records) == 1
        assert records[0].content == "用户喜欢草莓蛋糕"
        assert {
            item["content_snapshot"]
            for item in store.list_suppressions(
                canonical_user_id=canonical,
                preset="Normal",
            )
        } == {"用户喜欢蓝莓蛋糕"}
        await registry.shutdown()

    asyncio.run(scenario())


def test_agent_tool_logs_redact_browser_fill_values():
    secret = "browser-password-123"

    class BrowserProvider(SequenceProvider):
        async def complete_request(self, model, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                call = ProviderToolCall(
                    "browser-1",
                    "web_browser",
                    {
                        "action": "fill",
                        "element_ref": "e1",
                        "value": secret,
                    },
                )
                return ProviderResponse(
                    "",
                    (call,),
                    AssistantTurn(tool_calls=(call,)),
                )
            return ProviderResponse("done", (), AssistantTurn(text="done"))

    async def scenario():
        provider = BrowserProvider()
        registry = ToolRegistry(
            allowed_risks=frozenset({ToolRisk.PRIVILEGED})
        )

        async def fill(context, arguments):
            context.sensitive_values.add(str(arguments["value"]))
            return {"filled": arguments["value"]}

        registry.register(
            ToolSpec(
                name="web_browser",
                description="test browser fill",
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string"},
                        "element_ref": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["action", "element_ref", "value"],
                    "additionalProperties": False,
                },
                handler=fill,
                risk=ToolRisk.PRIVILEGED,
            )
        )
        context, _, _ = _context()
        logger = RecordingLogger()
        runner = AgentRunner(provider, registry, logger=logger)

        assert await runner.run(
            model="model-a",
            message="fill password",
            history=(),
            system_prompt="",
            attachments=(),
            context=context,
            enabled=True,
        ) == "done"
        logs = "\n".join(logger.messages)
        assert secret not in logs
        assert "[REDACTED]" in logs

    asyncio.run(scenario())


def test_agent_runner_falls_back_only_for_explicit_tool_unsupported():
    class UnsupportedProvider(SequenceProvider):
        async def complete_request(self, model, request):
            raise ToolsUnsupportedError("tools rejected")

    async def scenario():
        provider = UnsupportedProvider()
        registry = ToolRegistry()
        register_builtin_tools(registry)
        context, _, _ = _context()
        runner = AgentRunner(provider, registry)

        assert await runner.run(
            model="legacy-model",
            message="hello",
            history=(),
            system_prompt="",
            attachments=(),
            context=context,
            enabled=True,
        ) == "普通回答"
        assert provider.marked == ["legacy-model"]
        assert len(provider.chat_calls) == 1

    asyncio.run(scenario())


def test_agent_runner_executes_more_than_eight_tool_calls_and_preserves_order():
    class ManyCallsProvider(SequenceProvider):
        async def complete_request(self, model, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                calls = tuple(
                    ProviderToolCall(
                        f"call-{index}",
                        "calculate_expression",
                        {"expression": f"{index}+1"},
                    )
                    for index in range(12)
                )
                return ProviderResponse("", calls, AssistantTurn(tool_calls=calls))
            return ProviderResponse("done", (), AssistantTurn(text="done"))

    async def scenario():
        provider = ManyCallsProvider()
        registry = ToolRegistry()
        register_builtin_tools(registry)
        context, _, _ = _context()
        runner = AgentRunner(
            provider,
            registry,
            options=AgentOptions(max_parallel_calls=3),
        )
        assert await runner.run(
            model="model-a",
            message="two calls",
            history=(),
            system_prompt="",
            attachments=(),
            context=context,
            enabled=True,
        ) == "done"
        results = provider.requests[1].turns[1:]
        assert [item.call_id for item in results] == [
            f"call-{index}" for index in range(12)
        ]
        assert [
            json.loads(item.content)["data"]["result"] for item in results
        ] == [index + 1 for index in range(12)]

    asyncio.run(scenario())


def _write_model_config(
    root: Path,
    provider: str,
    **overrides,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    config = {
        "FriendlyName": "Agent",
        "Model": "test-model",
        "ResponseType": provider,
        "ApiKey": "test-secret",
        "BaseUrl": "https://provider.invalid/v1",
        "ToolsEnabled": "auto",
    }
    config.update(overrides)
    (root / "agent.ai.json").write_text(
        json.dumps(config),
        encoding="utf-8",
    )


def test_openai_provider_builds_and_replays_native_function_calls(tmp_path: Path):
    async def scenario():
        config_dir = tmp_path / "openai"
        _write_model_config(
            config_dir,
            "openai",
            EmptyResponseRetries=1,
        )
        payloads = []

        async def transport(provider, config, payload):
            payloads.append(payload)
            if len(payloads) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "openai-call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "calculate_expression",
                                            "arguments": '{"expression":"8*8"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            return {"choices": [{"message": {"content": "64"}}]}

        providers = ProviderRegistry(config_dir, transport=transport)
        registry = ToolRegistry()
        register_builtin_tools(registry)
        context, _, _ = _context()
        answer = await AgentRunner(providers, registry).run(
            model="agent",
            message="8*8",
            history=(),
            system_prompt="system",
            attachments=(),
            context=context,
            enabled=True,
        )

        assert answer == "64"
        assert len(payloads) == 2
        assert payloads[0]["stream"] is False
        declaration = payloads[0]["tools"][0]["function"]
        assert declaration["name"] == "calculate_expression"
        assert declaration["strict"] is False
        assert payloads[1]["messages"][-2]["tool_calls"][0]["id"] == "openai-call-1"
        assert payloads[1]["messages"][-1]["tool_call_id"] == "openai-call-1"

    asyncio.run(scenario())


def test_provider_retries_configured_empty_response_once(tmp_path: Path):
    async def scenario():
        config_dir = tmp_path / "empty-retry"
        _write_model_config(
            config_dir,
            "openai",
            EmptyResponseRetries=1,
        )
        payloads = []

        async def transport(provider, config, payload):
            payloads.append(payload)
            if len(payloads) == 1:
                return {"choices": [{"message": {"content": None}}]}
            return {"choices": [{"message": {"content": "recovered"}}]}

        providers = ProviderRegistry(config_dir, transport=transport)
        assert providers.get("agent").empty_response_retries == 1

        answer = await providers.chat("agent", "hello")

        assert answer == "recovered"
        assert len(payloads) == 2
        assert payloads[0] == payloads[1]

    asyncio.run(scenario())


def test_provider_rejects_excessive_empty_response_retries(tmp_path: Path):
    config_dir = tmp_path / "invalid-empty-retry"
    _write_model_config(
        config_dir,
        "openai",
        EmptyResponseRetries=3,
    )

    providers = ProviderRegistry(config_dir)

    assert "agent.ai.json" in providers.load_errors
    assert "EmptyResponseRetries" in providers.load_errors["agent.ai.json"]


def test_openai_provider_converts_deepseek_dsml_text_to_tool_calls(tmp_path: Path):
    async def scenario():
        config_dir = tmp_path / "dsml"
        _write_model_config(config_dir, "openai")
        payloads = []

        async def transport(provider, config, payload):
            payloads.append(payload)
            if len(payloads) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '< | | DSML | | tool_calls>\n'
                                    '< | | DSML | | invoke name="calculate_expression">\n'
                                    '< | | DSML | | parameter name="expression" string="true">'
                                    '6*7</ | | DSML | | parameter>\n'
                                    '</ | | DSML | | invoke>\n'
                                    '</ | | DSML | | tool_calls>'
                                )
                            }
                        }
                    ]
                }
            return {"choices": [{"message": {"content": "结果是 42。"}}]}

        providers = ProviderRegistry(config_dir, transport=transport)
        registry = ToolRegistry()
        register_builtin_tools(registry)
        context, _, _ = _context()
        answer = await AgentRunner(providers, registry).run(
            model="agent",
            message="六乘七是多少",
            history=(),
            system_prompt="system",
            attachments=(),
            context=context,
            enabled=True,
        )

        assert answer == "结果是 42。"
        replay = payloads[1]["messages"][-2]
        assert replay["tool_calls"][0]["function"]["name"] == "calculate_expression"
        result = json.loads(payloads[1]["messages"][-1]["content"])
        assert result["data"]["result"] == 42
        assert "DSML" not in answer

    asyncio.run(scenario())


def test_openai_provider_never_returns_malformed_dsml_as_user_text(tmp_path: Path):
    async def scenario():
        config_dir = tmp_path / "bad-dsml"
        _write_model_config(config_dir, "openai")

        async def transport(provider, config, payload):
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '<｜DSML｜tool_calls>'
                                '<｜DSML｜invoke name="web_search">broken'
                            )
                        }
                    }
                ]
            }

        providers = ProviderRegistry(config_dir, transport=transport)
        registry = ToolRegistry()
        register_builtin_tools(registry)
        context, _, _ = _context()
        with pytest.raises(ProviderRequestError):
            await AgentRunner(providers, registry).run(
                model="agent",
                message="搜索",
                history=(),
                system_prompt="system",
                attachments=(),
                context=context,
                enabled=True,
            )

    asyncio.run(scenario())


def test_dsml_never_leaks_and_agent_has_no_independent_step_limit(
    tmp_path: Path,
):
    async def scenario():
        config_dir = tmp_path / "final-dsml"
        _write_model_config(config_dir, "openai")
        payloads = []
        dsml = (
            '< | | DSML | | tool_calls>'
            '< | | DSML | | invoke name="calculate_expression">'
            '< | | DSML | | parameter name="expression" string="true">'
            '1+1</ | | DSML | | parameter>'
            '</ | | DSML | | invoke>'
            '</ | | DSML | | tool_calls>'
        )

        async def transport(provider, config, payload):
            payloads.append(payload)
            if len(payloads) <= 6:
                return {"choices": [{"message": {"content": dsml}}]}
            return {"choices": [{"message": {"content": "finished"}}]}

        providers = ProviderRegistry(config_dir, transport=transport)
        registry = ToolRegistry()
        register_builtin_tools(registry)
        context, _, _ = _context()
        answer = await AgentRunner(providers, registry).run(
            model="agent",
            message="计算",
            history=(),
            system_prompt="system",
            attachments=(),
            context=context,
            enabled=True,
        )
        assert answer == "finished"
        assert len(payloads) == 7
        assert all("tools" in payload for payload in payloads)
        assert "DSML" not in answer

    asyncio.run(scenario())


def test_gemini_provider_preserves_signature_and_replays_function_response(tmp_path: Path):
    async def scenario():
        config_dir = tmp_path / "gemini"
        _write_model_config(config_dir, "gemini")
        payloads = []

        async def transport(provider, config, payload):
            payloads.append(payload)
            if len(payloads) == 1:
                return {
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "functionCall": {
                                            "id": "gemini-call-1",
                                            "name": "calculate_expression",
                                            "args": {"expression": "9*9"},
                                        },
                                        "thoughtSignature": "opaque-signature",
                                    }
                                ],
                            }
                        }
                    ]
                }
            return {
                "candidates": [
                    {"content": {"role": "model", "parts": [{"text": "81"}]}}
                ]
            }

        providers = ProviderRegistry(config_dir, transport=transport)
        registry = ToolRegistry()
        register_builtin_tools(registry)
        context, _, _ = _context("feishu")
        answer = await AgentRunner(providers, registry).run(
            model="agent",
            message="9*9",
            history=(),
            system_prompt="system",
            attachments=(),
            context=context,
            enabled=True,
        )

        assert answer == "81"
        declaration = payloads[0]["tools"][0]["functionDeclarations"][0]
        assert declaration["name"] == "calculate_expression"
        assert declaration["parametersJsonSchema"]["additionalProperties"] is False
        model_content = payloads[1]["contents"][-2]
        assert model_content["parts"][0]["thoughtSignature"] == "opaque-signature"
        response = payloads[1]["contents"][-1]["parts"][0]["functionResponse"]
        assert response["id"] == "gemini-call-1"
        assert response["name"] == "calculate_expression"
        assert response["response"]["data"]["result"] == 81

    asyncio.run(scenario())


def test_schema_v2_session_settings_migrate_agent_override_idempotently(tmp_path: Path):
    path = tmp_path / "old-v2.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_meta (
                schema_name TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            INSERT INTO schema_meta VALUES('jianer_ai_memory', 2, 1);
            CREATE TABLE conversations (
                conversation_pk INTEGER PRIMARY KEY AUTOINCREMENT,
                protocol TEXT NOT NULL,
                self_id TEXT NOT NULL,
                conversation_kind TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL,
                UNIQUE(protocol, self_id, conversation_kind, conversation_id)
            );
            INSERT INTO conversations VALUES(
                1, 'onebot', 'bot-1', 'private', 'user-1', 1, 1
            );
            CREATE TABLE session_settings (
                session_id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_pk INTEGER NOT NULL,
                preset_key TEXT NOT NULL,
                model TEXT,
                persona TEXT NOT NULL DEFAULT '',
                tts_enabled INTEGER NOT NULL,
                ai_enabled INTEGER NOT NULL DEFAULT 1,
                memory_enabled INTEGER NOT NULL DEFAULT 1,
                memory_interval_seconds INTEGER NOT NULL DEFAULT 21600,
                last_generated_at INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                UNIQUE(conversation_pk, preset_key)
            );
            INSERT INTO session_settings VALUES(
                1, 1, 'Normal', 'model-a', '', 0, 1, 1, 21600, 0, 1
            );
            """
        )

    with pytest.raises(MemoryMigrationRequiredError):
        JianerMemoryStore(path)
    migration_store = JianerMemoryStore(path, initialize=False)
    backup = migration_store.migrate_to_v5()
    assert backup is not None and backup.is_file()
    store = JianerMemoryStore(path)
    settings = store.get_session_settings(
        protocol="onebot",
        self_id="bot-1",
        conversation_kind="private",
        conversation_id="user-1",
        preset="Normal",
    )
    assert settings.model == "model-a"
    assert settings.agent_enabled is None

    settings = store.set_session_settings(
        protocol="onebot",
        self_id="bot-1",
        conversation_kind="private",
        conversation_id="user-1",
        preset="Normal",
        agent_enabled=False,
    )
    assert settings.agent_enabled is False
    assert store.foreign_key_check() == ()

    JianerMemoryStore(path)
    with sqlite3.connect(path) as conn:
        version = conn.execute(
                "SELECT version FROM sys_schema "
                "WHERE schema_name='jianer_ai_memory'"
        ).fetchone()[0]
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(cfg_session_settings)")
        }
    assert version == SCHEMA_VERSION == 5
    assert "agent_enabled" in columns
