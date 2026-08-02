from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from jianer.adapters import ConversationKey, ConversationKind

from plugins.JianerAI.agent import AgentOptions, AgentRunner
from plugins.JianerAI.memory import JianerMemoryStore, SCHEMA_VERSION
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

    def list_memories(self, **kwargs):
        self.calls.append(kwargs)
        return (
            SimpleNamespace(fact_id=7, content="用户喜欢蓝色", weight=0.8),
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
        runner = AgentRunner(provider, registry)

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


def test_agent_runner_enforces_total_tool_call_limit_and_result_order():
    class LimitedProvider(SequenceProvider):
        async def complete_request(self, model, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                calls = (
                    ProviderToolCall("first", "calculate_expression", {"expression": "1+1"}),
                    ProviderToolCall("second", "calculate_expression", {"expression": "2+2"}),
                )
                return ProviderResponse("", calls, AssistantTurn(tool_calls=calls))
            return ProviderResponse("done", (), AssistantTurn(text="done"))

    async def scenario():
        provider = LimitedProvider()
        registry = ToolRegistry()
        register_builtin_tools(registry)
        context, _, _ = _context()
        runner = AgentRunner(
            provider,
            registry,
            options=AgentOptions(max_tool_calls=1),
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
        assert [item.call_id for item in results] == ["first", "second"]
        assert json.loads(results[0].content)["data"]["result"] == 2
        assert json.loads(results[1].content)["error_code"] == "tool_call_limit"

    asyncio.run(scenario())


def _write_model_config(root: Path, provider: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "agent.ai.json").write_text(
        json.dumps(
            {
                "FriendlyName": "Agent",
                "Model": "test-model",
                "ResponseType": provider,
                "ApiKey": "test-secret",
                "BaseUrl": "https://provider.invalid/v1",
                "ToolsEnabled": "auto",
            }
        ),
        encoding="utf-8",
    )


def test_openai_provider_builds_and_replays_native_function_calls(tmp_path: Path):
    async def scenario():
        config_dir = tmp_path / "openai"
        _write_model_config(config_dir, "openai")
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
        assert payloads[0]["stream"] is False
        declaration = payloads[0]["tools"][0]["function"]
        assert declaration["name"] == "calculate_expression"
        assert declaration["strict"] is False
        assert payloads[1]["messages"][-2]["tool_calls"][0]["id"] == "openai-call-1"
        assert payloads[1]["messages"][-1]["tool_call_id"] == "openai-call-1"

    asyncio.run(scenario())


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
        assert "additionalProperties" not in declaration["parameters"]
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
            "SELECT version FROM schema_meta WHERE schema_name='jianer_ai_memory'"
        ).fetchone()[0]
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(session_settings)")
        }
    assert version == SCHEMA_VERSION == 3
    assert "agent_enabled" in columns
