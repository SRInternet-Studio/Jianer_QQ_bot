from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jianer.adapters import ConversationKey, ConversationKind

from plugins.JianerAI.agent_runtime.bootstrap import import_legacy_contexts
from plugins.JianerAI.agent_runtime.api_profiles import ApiModelManager
from plugins.JianerAI.agent_runtime.collector import Collector
from plugins.JianerAI.agent_runtime.content_store import ContentStore
from plugins.JianerAI.agent_runtime.core import (
    AgentCore,
    _retryable_provider_error,
    _safe_tool_log_value,
)
from plugins.JianerAI.agent_runtime.models import AgentEvent, AgentRole, SessionKey
from plugins.JianerAI.agent_runtime.profiles import ProfileError, ProfileStore
from plugins.JianerAI.agent_runtime.prompts import (
    PROFILE_SUMMARY_MARKER,
    PROFILE_SUMMARY_MERGE_SYSTEM,
    PROFILE_SUMMARY_SYSTEM,
    build_main_prompt,
)
from plugins.JianerAI.agent_runtime.sessions import AgentSessionManager
from plugins.JianerAI.agent_runtime.store import ContextStore
from plugins.JianerAI.agent_runtime.subagents import SubAgentManager
from plugins.JianerAI.providers import (
    AssistantTurn,
    ChatRequest,
    ModelConfig,
    ModelConfigError,
    ProviderResponse,
    ProviderRequestError,
    ProviderRegistry,
    ProviderToolCall,
    _build_openai_payload,
    load_model_config,
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


async def _async_value(value: str) -> str:
    return value


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
    assert store.current().display_name == "角色"
    assert store.active_name == "Role"
    assert store.active_label() == "角色 (Role)"
    assert not (tmp_path / "profiles.json").exists()


def test_profile_store_resolves_and_persists_display_names(
    tmp_path: Path,
) -> None:
    metadata = tmp_path / "current.json"
    metadata.write_text(
        json.dumps(
            {
                "XingYu": {"name": "机娘", "path": "XingYu.txt"},
                "Normal": {"name": "做我女朋友", "path": "Normal.txt"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    for key in ("XingYu", "Normal"):
        (tmp_path / f"{key}.txt").write_text(f"{key} prompt", encoding="utf-8")
    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_text(
        json.dumps(
            {
                "XingYu": "机娘 prompt",
                "Normal": "Normal prompt",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    store = ProfileStore(
        profiles_path,
        legacy_metadata=metadata,
        active_name="机娘",
    )

    assert store.active_name == "XingYu"
    assert store.current().prompt == "机娘 prompt"
    assert store.get("机娘").prompt == "机娘 prompt"

    profiles_path.write_text(
        json.dumps(
            {
                "XingYu": "磁盘上更新后的机娘 prompt",
                "Normal": {
                    "prompt": "Normal prompt",
                    "name": "做我女朋友",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert store.current().prompt == "磁盘上更新后的机娘 prompt"
    store.upsert(
        "做我女朋友",
        "更新后的 prompt",
        display_name="做我女朋友",
    )
    assert store.get("Normal").prompt == "更新后的 prompt"
    with pytest.raises(ProfileError):
        store.remove("机娘")

    store.switch("Normal")
    assert store.remove("机娘") is True
    saved = json.loads(profiles_path.read_text(encoding="utf-8"))
    assert saved["Normal"]["prompt"] == "更新后的 prompt"
    assert saved["Normal"]["inject_master"] is True
    assert "XingYu" not in saved


def test_persona_prompt_is_the_last_system_prompt_section() -> None:
    persona = "你是机娘星语。说话自然、有主见，绝不切换成通用 AI 助手腔。"
    prompt = build_main_prompt(
        profile_prompt=persona,
        inject_master=True,
        owner_ids=("10001",),
        tools="- send_group_msg(text)",
    )

    expected_persona = "# 角色\n\n" + persona
    assert prompt.endswith(expected_persona)
    assert prompt.count(expected_persona) == 1
    assert "10001" in prompt

    without_master = build_main_prompt(
        profile_prompt=persona,
        inject_master=False,
        owner_ids=("10001",),
        tools="- send_group_msg(text)",
    )
    assert "10001" not in without_master


def test_profile_switch_archive_preserves_tool_turns(tmp_path: Path) -> None:
    key = SessionKey(
        scene_type="group",
        scene_id="group-1",
        protocol="milky",
        self_id="bot-1",
    )
    history = [
        {"role": "user", "content": "查天气"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "name": "web_search",
                    "arguments": {"query": "天气"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "web_search",
            "content": '{"temperature": 20}',
        },
        {"role": "assistant", "content": "今天 20 度。"},
        {"role": "user", "content": "继续"},
        {"role": "assistant", "content": "好。"},
        {"role": "user", "content": "再看看"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-2",
                    "name": "get_weather",
                    "arguments": {"city": "黄山"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-2",
            "name": "get_weather",
            "content": '{"weather": "晴"}',
        },
        {"role": "assistant", "content": "现在是晴天。"},
    ]
    store = ContextStore(tmp_path)
    captured: list[str] = []

    async def scenario():
        await store.save_history(key, history)
        core = AgentCore(
            key,
            role=AgentRole.MAIN,
            providers=SimpleNamespace(),
            tools=ToolRegistry(),
            store=store,
            model_resolver=lambda _key: "demo",
            prompt_builder=lambda _specs: "system",
        )

        async def summarize(
            system_prompt: str,
            text: str,
            max_tokens: int,
        ) -> str:
            del system_prompt, max_tokens
            captured.append(text)
            return "事实摘要"

        changed = await core.archive_for_profile_switch(
            summarize,
            keep_turns=2,
        )
        return changed, core.history

    changed, archived = asyncio.run(scenario())
    assert changed is True
    assert archived[:2] == [
        {"role": "user", "content": PROFILE_SUMMARY_MARKER},
        {"role": "assistant", "content": "事实摘要"},
    ]
    assert archived[2:] == history[4:]
    assert "web_search" in captured[0]
    assert '"query": "天气"' in captured[0]
    assert '{"temperature": 20}' in captured[0]
    assert "get_weather" not in captured[0]


def test_profile_switch_archive_waits_for_tool_turn_boundary(
    tmp_path: Path,
) -> None:
    key = SessionKey(
        scene_type="group",
        scene_id="group-boundary",
        protocol="milky",
        self_id="bot-1",
    )
    history = [
        message
        for index in range(8)
        for message in (
            {"role": "user", "content": f"user {index}"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call-{index}",
                        "name": "lookup",
                        "arguments": {"index": index},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": f"call-{index}",
                "name": "lookup",
                "content": f"result {index}",
            },
            {"role": "assistant", "content": f"done {index}"},
        )
    ]
    store = ContextStore(tmp_path)

    async def scenario():
        await store.save_history(key, history)
        core = AgentCore(
            key,
            role=AgentRole.MAIN,
            providers=SimpleNamespace(),
            tools=ToolRegistry(),
            store=store,
            model_resolver=lambda _key: "demo",
            prompt_builder=lambda _specs: "system",
        )
        await core.ensure_loaded()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold_active_tool_turn() -> None:
            async with core._run_lock:
                entered.set()
                await release.wait()

        holder = asyncio.create_task(hold_active_tool_turn())
        await entered.wait()
        archive = asyncio.create_task(
            core.archive_for_profile_switch(
                lambda _system, _text, _tokens: _async_value("事实摘要"),
                keep_turns=6,
            )
        )
        await asyncio.sleep(0.01)
        waited = not archive.done()
        release.set()
        await asyncio.gather(holder, archive)
        return waited, archive.result()

    waited, changed = asyncio.run(scenario())
    assert waited is True
    assert changed is True


def test_profile_switch_summary_chunks_are_merged(tmp_path: Path) -> None:
    key = SessionKey(
        scene_type="private",
        scene_id="user-chunk",
        protocol="milky",
        self_id="bot-1",
    )
    history = [
        message
        for index in range(8)
        for message in (
            {"role": "user", "content": f"u{index}:" + "x" * 1200},
            {"role": "assistant", "content": f"a{index}:" + "y" * 1200},
        )
    ]
    store = ContextStore(tmp_path)
    system_prompts: list[str] = []
    summary_inputs: list[str] = []

    async def scenario():
        await store.save_history(key, history)
        core = AgentCore(
            key,
            role=AgentRole.MAIN,
            providers=SimpleNamespace(),
            tools=ToolRegistry(),
            store=store,
            model_resolver=lambda _key: "demo",
            prompt_builder=lambda _specs: "system",
        )

        async def summarize(system_prompt, text, max_tokens):
            del max_tokens
            system_prompts.append(system_prompt)
            summary_inputs.append(text)
            if system_prompt == PROFILE_SUMMARY_MERGE_SYSTEM:
                return "合并后的状态"
            return "分段摘要"

        await core.archive_for_profile_switch(
            summarize,
            keep_turns=6,
            summary_chunk_chars=2000,
        )
        return list(core.history)

    archived = asyncio.run(scenario())
    assert system_prompts.count(PROFILE_SUMMARY_SYSTEM) >= 2
    assert system_prompts[-1] == PROFILE_SUMMARY_MERGE_SYSTEM
    assert "新增分段摘要 1" in summary_inputs[-1]
    assert archived[1]["content"] == "合并后的状态"


def test_session_profile_switch_refreshes_loaded_contexts(
    tmp_path: Path,
) -> None:
    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_text(
        json.dumps(
            {
                "Alpha": {"prompt": "旧人设", "inject_master": True},
                "Beta": {"prompt": "新人设", "inject_master": False},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    profiles = ProfileStore(profiles_path, active_name="Alpha")
    store = ContextStore(tmp_path / "agent")
    key = SessionKey(
        scene_type="private",
        scene_id="user-1",
        protocol="milky",
        self_id="bot-1",
    )
    history = [
        message
        for index in range(8)
        for message in (
            {"role": "user", "content": f"user {index}"},
            {"role": "assistant", "content": f"assistant {index}"},
        )
    ]

    class Providers:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def chat_request(self, model, request):
            del model
            self.calls.append(request.message)
            return "新人设可继续使用的事实状态"

    providers = Providers()
    persisted: list[str] = []

    async def scenario():
        manager = AgentSessionManager(
            store=store,
            providers=providers,
            tools=ToolRegistry(),
            profiles=profiles,
            model_resolver=lambda _key: "demo",
            owner_ids=(),
            profile_switch_summary=True,
            profile_switch_keep_turns=6,
            profile_switch_callback=persisted.append,
        )
        core = await manager.get_main(key)
        core.history = history
        await core.save()
        result = await manager.switch_profile("Beta")
        switch_state = store.load_profile_switch()
        await manager.close()
        return core, result, switch_state

    core, result, switch_state = asyncio.run(scenario())
    assert "Beta" in result
    assert profiles.active_name == "Beta"
    assert switch_state is not None
    assert switch_state["profile"] == "Beta"
    assert persisted == ["Beta"]
    assert core.history[0]["content"] == PROFILE_SUMMARY_MARKER
    assert core.history[1]["content"] == "新人设可继续使用的事实状态"
    assert core.history[2:] == history[4:]
    assert "user 1" in providers.calls[0]
    assert "user 2" not in providers.calls[0]


def test_profile_switch_lazily_archives_unloaded_context(
    tmp_path: Path,
) -> None:
    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_text(
        json.dumps(
            {
                "Alpha": {"prompt": "旧人设", "inject_master": True},
                "Beta": {"prompt": "新人设", "inject_master": True},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    profiles = ProfileStore(profiles_path, active_name="Beta")
    store = ContextStore(tmp_path / "agent")
    key = SessionKey(
        scene_type="group",
        scene_id="group-2",
        protocol="milky",
        self_id="bot-1",
    )
    history = [
        message
        for index in range(8)
        for message in (
            {"role": "user", "content": f"old user {index}"},
            {"role": "assistant", "content": f"old assistant {index}"},
        )
    ]

    class Providers:
        def __init__(self) -> None:
            self.calls = 0

        async def chat_request(self, model, request):
            del model, request
            self.calls += 1
            return "惰性归档摘要"

    providers = Providers()

    async def scenario():
        await store.save_history(key, history)
        await asyncio.sleep(0.02)
        store.mark_profile_switch("Beta")
        manager = AgentSessionManager(
            store=store,
            providers=providers,
            tools=ToolRegistry(),
            profiles=profiles,
            model_resolver=lambda _key: "demo",
            owner_ids=(),
            profile_switch_summary=True,
            profile_switch_keep_turns=6,
        )
        core = await manager.get_main(key)
        pending_before = key in manager._profile_summary_pending
        calls_before = providers.calls
        await manager.prepare_main(core)
        archived = list(core.history)
        await manager.close()
        return pending_before, calls_before, archived

    pending, calls_before, archived = asyncio.run(scenario())
    assert pending is True
    assert calls_before == 0
    assert providers.calls == 1
    assert archived[0]["content"] == PROFILE_SUMMARY_MARKER
    assert archived[1]["content"] == "惰性归档摘要"
    assert archived[2:] == history[4:]


def test_model_config_accepts_case_insensitive_keys(tmp_path: Path) -> None:
    path = tmp_path / "lowercase.ai.json"
    path.write_text(
        json.dumps(
            {
                "friendlyname": "Lowercase model",
                "model": "deepseek-flash",
                "apikey": "test-key",
                "responsetype": "OpenAI Responses",
                "baseurl": "https://lower.example/v1",
                "temperature": 0.3,
                "toolsenabled": "false",
                "systempromptmode": "user",
            }
        ),
        encoding="utf-8",
    )

    config = load_model_config(path)
    assert config.friendly_name == "Lowercase model"
    assert config.provider == "openai_responses"
    assert config.model == "deepseek-flash"
    assert config.base_url == "https://lower.example/v1"
    assert config.temperature == 0.3
    assert config.tools_enabled == "disabled"
    assert config.system_prompt_mode == "user"

    path.write_text(
        json.dumps(
            {
                "FriendlyName": "Exact key model",
                "Model": "deepseek-flash",
                "ApiKey": "test-key",
                "ResponseType": "OpenAI Responses",
                "BaseUrl": "https://exact.example/v1",
                "baseurl": "https://lower.example/v1",
            }
        ),
        encoding="utf-8",
    )
    assert load_model_config(path).base_url == "https://exact.example/v1"


def test_agent_api_manager_prefers_configured_active_model(
    tmp_path: Path,
) -> None:
    for key, provider, model in (
        ("deepseek", "OpenAI Responses", "deepseek-flash"),
        ("gemini", "Google GenerateContent", "gemini-3.8-flash"),
    ):
        (tmp_path / f"{key}.ai.json").write_text(
            json.dumps(
                {
                    "FriendlyName": key,
                    "Model": model,
                    "ApiKey": "test-key",
                    "ResponseType": provider,
                    "BaseUrl": f"https://{key}.example/v1",
                    "ToolsEnabled": "auto",
                }
            ),
            encoding="utf-8",
        )
    registry = ProviderRegistry(tmp_path)
    manager = ApiModelManager(
        registry,
        default_model="gemini",
        active_model="deepseek",
    )
    assert manager.active == "deepseek"


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
    logs: list[str] = []

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
    logger = SimpleNamespace(info=logs.append)
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
        logger=logger,
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
    assert any("Agent tool call started" in line for line in logs)
    assert any("send_group_msg" in line for line in logs)
    assert any("Agent tool call completed" in line for line in logs)


def test_tool_log_values_redact_secrets() -> None:
    value = _safe_tool_log_value(
        {
            "api_key": "sk-secret",
            "nested": {"authorization": "Bearer secret", "text": "ok"},
        }
    )
    assert value["api_key"] == "[REDACTED]"
    assert value["nested"]["authorization"] == "[REDACTED]"
    assert value["nested"]["text"] == "ok"


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


def test_deterministic_provider_errors_are_not_retried() -> None:
    assert not _retryable_provider_error(
        ProviderRequestError(
            "responses request failed for model 'demo' (HTTP 401, AuthenticationError)"
        )
    )
    assert not _retryable_provider_error(
        ProviderRequestError(
            "chat request failed for model 'demo' (HTTP 404, NotFoundError)"
        )
    )
    assert _retryable_provider_error(
        ProviderRequestError(
            "chat request failed for model 'demo' (APIConnectionError)"
        )
    )
