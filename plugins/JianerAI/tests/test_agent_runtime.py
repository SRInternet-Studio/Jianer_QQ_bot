from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jianer.adapters import ConversationKey, ConversationKind

from plugins.JianerAI.agent_runtime.bootstrap import import_legacy_contexts
from plugins.JianerAI.agent_runtime.collector import Collector
from plugins.JianerAI.agent_runtime.content_store import ContentStore
from plugins.JianerAI.agent_runtime.core import AgentCore
from plugins.JianerAI.agent_runtime.models import AgentEvent, AgentRole, SessionKey
from plugins.JianerAI.agent_runtime.profiles import ProfileStore
from plugins.JianerAI.agent_runtime.store import ContextStore
from plugins.JianerAI.agent_runtime.subagents import SubAgentManager
from plugins.JianerAI.providers import (
    AssistantTurn,
    ChatRequest,
    ModelConfig,
    ModelConfigError,
    ProviderResponse,
    ProviderToolCall,
    _build_openai_payload,
)
from plugins.JianerAI.tools.contracts import (
    ToolCall,
    ToolContext,
    ToolRisk,
    ToolSpec,
)
from plugins.JianerAI.tools.decorators import AgentToolBase, tool
from plugins.JianerAI.tools.registry import ToolRegistry


def _context() -> ToolContext:
    conversation = ConversationKey(
        protocol="milky",
        self_id="bot-1",
        kind=ConversationKind.GROUP,
        conversation_id="group-1",
        preset="default",
    )
    return ToolContext(
        event=SimpleNamespace(group_id="group-1", user_id="user-1"),
        actions=SimpleNamespace(capabilities=frozenset()),
        conversation=conversation,
        canonical_user_id="qq:user-1",
        runtime={},
        memory=None,
        ev_type="group",
        scene_id="group-1",
        perm_group="member",
        principal_id="user-1",
        self_id="bot-1",
        role="main",
    )


def test_context_store_round_trip(tmp_path: Path) -> None:
    key = SessionKey(
        scene_type="group",
        scene_id="123",
        protocol="milky",
        self_id="bot",
    )
    store = ContextStore(tmp_path)

    async def scenario():
        await store.save_history(key, [{"role": "user", "content": "hello"}])
        await store.save_tasks(key, ["follow up"])
        return (
            await store.load_history(key),
            await store.load_tasks(key),
        )

    history, tasks = asyncio.run(scenario())
    assert history == [{"role": "user", "content": "hello"}]
    assert tasks == ["follow up"]
    metadata = json.loads(
        store.metadata_path(key).read_text(encoding="utf-8")
    )
    assert metadata["session"] == key.value


def test_profile_store_bootstraps_raw_legacy_templates(tmp_path: Path) -> None:
    (tmp_path / "Role.txt").write_text("原始人设全文", encoding="utf-8")
    metadata = tmp_path / "current.json"
    metadata.write_text(
        json.dumps(
            {
                "Role": {
                    "name": "角色",
                    "info": "",
                    "path": "Role.txt",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = ProfileStore(
        tmp_path / "profiles.json",
        legacy_metadata=metadata,
        active_name="Role",
    )

    assert store.current().prompt == "原始人设全文"
    assert store.current().inject_master is True
    assert not (tmp_path / "profiles.json").exists()


def test_collector_compresses_and_preserves_recent_events() -> None:
    async def scenario():
        collector = Collector(max_buffer=4, keep_after_compress=2)
        for index in range(4):
            await collector.append_passive(
                AgentEvent(
                    type="message",
                    scene_type="group",
                    scene_id="1",
                    payload={"text": str(index)},
                )
            )
        return collector.buffer

    buffer = asyncio.run(scenario())
    assert buffer[0]["compressed"] is True
    assert buffer[0]["count"] == 2
    assert len(buffer) == 3


def test_tool_registry_keeps_legacy_execute_and_adds_hype_dispatch() -> None:
    registry = ToolRegistry(
        allowed_risks=frozenset({ToolRisk.READ_ONLY, ToolRisk.MUTATING}),
    )
    spec = ToolSpec(
        name="echo",
        description="echo",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        handler=lambda _context, arguments: arguments["text"],
    )
    registry.register(spec)
    context = _context()

    async def scenario():
        old = await registry.execute(
            ToolCall("1", "echo", {"text": "legacy"}),
            context,
        )
        hype = await registry.dispatch("echo", {"text": "hype"}, context)
        return old, hype

    old, hype = asyncio.run(scenario())
    assert old.ok is True
    assert json.loads(old.content)["data"] == "legacy"
    assert json.loads(hype)["data"] == "hype"
    assert registry.schema("main")[0]["function"]["name"] == "echo"


def test_decorated_tool_binds_to_compatibility_registry() -> None:
    class DemoTools(AgentToolBase):
        @tool(group="demo")
        async def greet(self, ctx, name: str) -> str:
            """Greet one user."""
            return f"hello {name}"

    registry = ToolRegistry()
    registry.register_decorated(DemoTools())
    spec = registry.specs[0]
    assert spec.name == "greet"
    assert spec.group == "demo"
    assert spec.input_schema["required"] == ["name"]


def test_agent_core_executes_tool_then_finishes_and_persists_history(
    tmp_path: Path,
) -> None:
    executed: list[str] = []

    def send_handler(context, arguments):
        del context
        executed.append(str(arguments["text"]))
        return {"sent": True}

    registry = ToolRegistry(
        allowed_risks=frozenset({ToolRisk.READ_ONLY, ToolRisk.MUTATING}),
    )
    registry.register(
        ToolSpec(
            name="send_group_msg",
            description="send",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            handler=send_handler,
            risk=ToolRisk.MUTATING,
            scenes=frozenset({"group"}),
            group="qq",
        )
    )

    class Providers:
        def __init__(self):
            self.calls = 0

        async def complete_agent_turn(self, model, request):
            del model, request
            self.calls += 1
            if self.calls == 1:
                call = ProviderToolCall(
                    "call-1",
                    "send_group_msg",
                    {"text": "sent via tool"},
                )
                return ProviderResponse(
                    "",
                    (call,),
                    AssistantTurn(tool_calls=(call,)),
                )
            return ProviderResponse("{}", (), AssistantTurn(text="{}"))

    providers = Providers()
    key = SessionKey(
        scene_type="group",
        scene_id="group-1",
        protocol="milky",
        self_id="bot-1",
    )
    core = AgentCore(
        key,
        role=AgentRole.MAIN,
        providers=providers,
        tools=registry,
        store=ContextStore(tmp_path),
        model_resolver=lambda _key: "demo",
        prompt_builder=lambda specs: "system",
    )

    result = asyncio.run(
        core.event_handler('{"event":"hello"}', context=_context())
    )
    assert result.rounds == 2
    assert result.tool_calls == 1
    assert executed == ["sent via tool"]
    loaded = asyncio.run(ContextStore(tmp_path).load_history(key))
    assert len(loaded) == 3
    assert loaded[-2]["role"] == "assistant"
    assert loaded[-1]["role"] == "tool"


def test_subagents_are_discovered_after_manager_restart(tmp_path: Path) -> None:
    tools = ToolRegistry()
    manager = SubAgentManager(
        root=tmp_path / "subagents",
        providers=SimpleNamespace(),
        tools=tools,
        model_resolver=lambda _key: "demo",
    )
    owner = SessionKey(
        scene_type="group",
        scene_id="group-1",
        protocol="milky",
        self_id="bot-1",
    )

    message = asyncio.run(
        manager.create(
            name="researcher",
            prompt="research only",
            parent=_context(),
            owner=owner,
        )
    )
    assert "#1" in message
    assert "#1" in manager.list()

    restarted = SubAgentManager(
        root=tmp_path / "subagents",
        providers=SimpleNamespace(),
        tools=tools,
        model_resolver=lambda _key: "demo",
    )
    assert "researcher" in restarted.list()


def test_raw_history_replays_tool_calls_and_outputs() -> None:
    config = ModelConfig(
        key="demo",
        friendly_name="demo",
        provider="OpenAI Chat Completions",
        model="demo",
        api_key="test",
    )
    request = ChatRequest(
        message="continue",
        history_items=(
            {"role": "user", "content": "first"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": "lookup",
                        "arguments": {"query": "x"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "lookup",
                "content": '{"ok":true}',
            },
        ),
    )

    payload = _build_openai_payload(config, request)
    messages = payload["messages"]
    assert messages[0]["role"] == "user"
    assert messages[1]["tool_calls"][0]["id"] == "call-1"
    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == "call-1"


def test_content_store_preserves_large_tool_results(tmp_path: Path) -> None:
    store = ContentStore(tmp_path)
    metadata = store.put(
        "prefix " + "x" * 2000 + " suffix",
        source="lookup",
        title="lookup",
    )
    assert metadata["chars"] > 2000
    assert "suffix" in store.read(metadata["content_id"], limit=3000)["content"]
    assert (
        store.search(metadata["content_id"], "suffix")["count"]
        == 1
    )


def test_anthropic_protocol_is_rejected() -> None:
    with pytest.raises(ModelConfigError):
        ModelConfig(
            key="legacy",
            friendly_name="legacy",
            provider="Anthropic Messages",
            model="claude",
            api_key="test",
        )


def test_registry_filters_hype_permissions_and_scenes() -> None:
    registry = ToolRegistry(
        allowed_risks=frozenset({ToolRisk.READ_ONLY}),
    )
    registry.register(
        ToolSpec(
            name="admin_only",
            description="admin",
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            handler=lambda _context, _arguments: "ok",
            perm="any_admin",
            scenes=frozenset({"group"}),
        )
    )
    context = _context()
    assert [spec.name for spec in registry.available(context)] == []
    privileged = __import__("dataclasses").replace(
        context,
        perm_group="any_admin",
    )
    assert [spec.name for spec in registry.available(privileged)] == [
        "admin_only"
    ]


def test_legacy_context_import_is_one_time(tmp_path: Path) -> None:
    partition = SimpleNamespace(
        conversation_kind="group",
        conversation_id="group-1",
        protocol="milky",
        self_id="bot-1",
    )
    records = [
        SimpleNamespace(content="hello", direction="incoming"),
        SimpleNamespace(content="hi", direction="outgoing"),
    ]

    class Memory:
        @staticmethod
        def list_chat_partitions():
            return (partition,)

        @staticmethod
        def query_recent_chat(**_kwargs):
            return tuple(records)

    store = ContextStore(tmp_path)
    marker = tmp_path / "imported"

    async def scenario():
        first = await import_legacy_contexts(
            Memory(),
            store,
            marker=marker,
        )
        second = await import_legacy_contexts(
            Memory(),
            store,
            marker=marker,
        )
        return first, second

    assert asyncio.run(scenario()) == (1, 0)
    key = SessionKey(
        scene_type="group",
        scene_id="group-1",
        protocol="milky",
        self_id="bot-1",
    )
    assert len(asyncio.run(store.load_history(key))) == 2
