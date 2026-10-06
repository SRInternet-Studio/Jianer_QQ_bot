from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from plugins.JianerAI.agent import AgentKernel
from plugins.JianerAI.providers import (
    AssistantTurn,
    ChatRequest,
    FunctionTool,
    ProviderRegistry,
    ProviderResponse,
    ProviderStreamEvent,
    ProviderToolCall,
)
from plugins.JianerAI.tools.builtin import _current_chat_messages
from plugins.JianerAI.tools.contracts import ToolCall, ToolResult, ToolRisk, ToolSpec
from plugins.JianerAI.tools.platform_api import (
    LARK_API_CATALOG,
    LARK_API_TOOL_NAMES,
    MILKY_API_TOOL_NAMES,
    ONEBOT_API_CATALOG,
    ONEBOT_API_TOOL_NAMES,
    PLATFORM_API_TOOL_NAMES,
    PlatformTransport,
    parse_platform_command,
    register_platform_api_tools,
)
from plugins.JianerAI.tools.registry import ToolRegistry


def _registry(tmp_path: Path, provider: str, transport: Any) -> ProviderRegistry:
    config_dir = tmp_path / "aiconfig"
    config_dir.mkdir()
    (config_dir / "demo.ai.json").write_text(
        json.dumps(
            {
                "Provider": provider,
                "Model": "demo-model",
                "ApiKey": "test-key",
            }
        ),
        encoding="utf-8",
    )
    return ProviderRegistry(config_dir, transport=transport)


def _tool() -> FunctionTool:
    return FunctionTool(
        name="lookup",
        description="Look up a value.",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
    )


def test_openai_stream_preserves_text_and_joins_split_tool_arguments(tmp_path):
    seen_payloads = []

    async def transport(provider, config, payload):
        seen_payloads.append(dict(payload))

        async def chunks():
            yield {"choices": [{"delta": {"content": "我来帮你查一下。"}}]}
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "lookup", "arguments": '{"que'},
                                }
                            ]
                        }
                    }
                ]
            }
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"name": "", "arguments": 'ry":"flux"}'},
                                }
                            ]
                        }
                    }
                ]
            }

        return chunks()

    async def run():
        registry = _registry(tmp_path, "openai", transport)
        return [
            event
            async for event in registry.stream_agent_turn(
                "demo",
                ChatRequest(message="查一下", tools=(_tool(),)),
            )
        ]

    events = asyncio.run(run())
    assert seen_payloads[0]["stream"] is True
    assert [event.kind for event in events] == [
        "text_delta",
        "tool_call_delta",
        "completed",
    ]
    response = events[-1].response
    assert response is not None
    assert response.text == "我来帮你查一下。"
    assert response.tool_calls[0].id == "call_1"
    assert response.tool_calls[0].arguments == '{"query":"flux"}'


def test_gemini_stream_yields_text_and_complete_function_call(tmp_path):
    async def transport(provider, config, payload):
        assert provider == "gemini"

        async def chunks():
            yield {
                "candidates": [
                    {"content": {"role": "model", "parts": [{"text": "我先查一下。"}]}}
                ]
            }
            yield {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "lookup",
                                        "args": {"query": "flux"},
                                    }
                                }
                            ],
                        }
                    }
                ]
            }

        return chunks()

    async def run():
        registry = _registry(tmp_path, "gemini", transport)
        return [
            event
            async for event in registry.stream_agent_turn(
                "demo",
                ChatRequest(message="查一下", tools=(_tool(),)),
            )
        ]

    events = asyncio.run(run())
    assert [event.kind for event in events] == [
        "text_delta",
        "tool_call_delta",
        "completed",
    ]
    response = events[-1].response
    assert response is not None
    assert response.text == "我先查一下。"
    assert response.tool_calls[0].arguments == {"query": "flux"}
    assert response.turn.provider_content == {
        "role": "model",
        "parts": [
            {"text": "我先查一下。"},
            {"functionCall": {"name": "lookup", "args": {"query": "flux"}}},
        ],
    }


def test_agent_delivers_model_text_before_executing_streamed_tool():
    order = []

    class Providers:
        def supports_tools(self, model):
            return True

        async def stream_agent_turn(self, model, request, *, request_id=None):
            if not request.turns:
                yield ProviderStreamEvent("text_delta", text="我来帮你查。")
                yield ProviderStreamEvent("tool_call_delta")
                call = ProviderToolCall("call_1", "lookup", {"query": "x"})
                yield ProviderStreamEvent(
                    "completed",
                    response=ProviderResponse(
                        "我来帮你查。",
                        (call,),
                        AssistantTurn("我来帮你查。", (call,)),
                    ),
                )
            else:
                yield ProviderStreamEvent("text_delta", text="查到了。")
                yield ProviderStreamEvent(
                    "completed",
                    response=ProviderResponse(
                        "查到了.", (), AssistantTurn("查到了.")
                    ),
                )

    class Tools:
        def available(self, context):
            return (
                ToolSpec(
                    name="lookup",
                    description="Look up a value.",
                    input_schema={"type": "object", "properties": {}},
                    handler=lambda *_: None,
                    risk=ToolRisk.READ_ONLY,
                ),
            )

        async def execute(self, call, context):
            order.append("tool")
            return ToolResult(call.id, call.name, True, "ok")

    class Observer:
        async def on_assistant_delta(self, text):
            order.append(("text", text))

        async def on_tool_call_delta(self):
            order.append("tool_call")

        async def on_tool_start(self, call):
            order.append("tool_start")

    context = SimpleNamespace(
        conversation=SimpleNamespace(
                protocol="milky",
                self_id="bot",
                kind=SimpleNamespace(value="group"),
                conversation_id="983497968",
            preset="default",
        ),
        event=SimpleNamespace(user_id="user"),
        actor=None,
        memory=None,
        sensitive_values=set(),
    )

    async def run():
        return await AgentKernel(Providers(), Tools()).run(
            model="demo",
            message="查一下",
            history=(),
            system_prompt="",
            attachments=(),
            context=context,
            enabled=True,
            observer=Observer(),
        )

    assert asyncio.run(run()) == "查到了."
    assert order.index(("text", "我来帮你查。")) < order.index("tool")
    assert "tool_call" in order


def test_agent_cancels_and_closes_stream_on_interruption():
    closed = asyncio.Event()
    interrupt = asyncio.Event()

    class Providers:
        async def stream_agent_turn(self, model, request, *, request_id=None):
            try:
                yield ProviderStreamEvent("text_delta", text="正在查")
                await asyncio.Event().wait()
            finally:
                closed.set()

    class Observer:
        async def on_assistant_delta(self, text):
            interrupt.set()

    class Tools:
        def available(self, context):
            return ()

    context = SimpleNamespace(conversation=None, actor=None, memory=None)

    async def run():
        try:
            await AgentKernel(Providers(), Tools()).run(
                model="demo",
                message="查一下",
                history=(),
                system_prompt="",
                attachments=(),
                context=context,
                enabled=False,
                observer=Observer(),
                interrupt_event=interrupt,
            )
        except Exception:
            pass
        await asyncio.wait_for(closed.wait(), timeout=1)

    asyncio.run(run())


def test_milky_essence_uses_adapter_and_chat_tool_exposes_platform_id():
    from jianer.LecAdapters.MilkyLib.translator import msg_enid

    class Actions:
        protocol = "milky"

        def __init__(self):
            self.calls = []
            self.connection = SimpleNamespace(http_send=self.http_send)

        def http_send(self, endpoint, params, **kwargs):
            self.calls.append((endpoint, params, kwargs))
            return {"status": "ok", "retcode": 0, "data": {}}

    actions = Actions()
    transport = PlatformTransport(actions)
    result = asyncio.run(
        transport.call(
            "set_group_essence_message",
            {"group_id": 983497968, "message_seq": 3943, "is_set": True},
        )
    )
    assert actions.calls[0][:2] == (
        "set_group_essence_message",
        {"group_id": 983497968, "message_seq": 3943, "is_set": True},
    )
    assert result["status"] == "ok"

    record = SimpleNamespace(
        id="3943",
        external_message_id=str(msg_enid(1, 3943, 983497968)),
        direction="incoming",
        sender_name="member",
        sender_canonical_id="qq:1",
        content="hello",
        occurred_at=123,
        message_type="text",
    )
    context = SimpleNamespace(
        memory=SimpleNamespace(query_recent_chat=lambda **kwargs: (record,)),
        conversation=SimpleNamespace(
            protocol="milky",
            self_id="bot",
            kind=SimpleNamespace(value="group"),
            conversation_id="983497968",
        ),
    )
    output = asyncio.run(_current_chat_messages(context, query="", limit=10))
    assert output["messages"][0]["id"] == "3943"
    assert output["messages"][0]["platform_message_id"] == str(
        msg_enid(1, 3943, 983497968)
    )
    assert output["messages"][0]["platform_group_id"] == 983497968
    assert output["messages"][0]["platform_message_seq"] == 3943

    context.conversation.protocol = "onebot"
    non_milky = asyncio.run(_current_chat_messages(context, query="", limit=10))
    assert non_milky["messages"][0]["platform_group_id"] is None
    assert non_milky["messages"][0]["platform_message_seq"] is None

    context.conversation.protocol = "milky"
    context.conversation.kind.value = "private"
    private = asyncio.run(_current_chat_messages(context, query="", limit=10))
    assert private["messages"][0]["platform_group_id"] is None
    assert private["messages"][0]["platform_message_seq"] is None

    context.conversation.kind.value = "group"
    context.conversation.conversation_id = "123"
    mismatched_group = asyncio.run(
        _current_chat_messages(context, query="", limit=10)
    )
    assert mismatched_group["messages"][0]["platform_group_id"] is None
    assert mismatched_group["messages"][0]["platform_message_seq"] is None


def test_milky_catalog_registers_typed_endpoint_tools_and_hides_raw_passthrough():
    registry = ToolRegistry(
        allowed_risks=frozenset({ToolRisk.READ_ONLY, ToolRisk.MUTATING}),
        allowed_mutating_tools=PLATFORM_API_TOOL_NAMES,
    )
    specs = register_platform_api_tools(registry)
    assert len(MILKY_API_TOOL_NAMES) == 65
    assert MILKY_API_TOOL_NAMES.issubset({spec.name for spec in specs})

    essence = next(
        spec for spec in specs if spec.name == "milky_set_group_essence_message"
    )
    assert essence.input_schema["properties"]["group_id"]["type"] == "integer"
    assert essence.input_schema["properties"]["message_seq"]["type"] == "integer"
    is_set_schema = essence.input_schema["properties"]["is_set"]
    assert is_set_schema["anyOf"][0]["type"] == "boolean"
    assert essence.input_schema["required"] == ["group_id", "message_seq"]
    assert essence.input_schema["additionalProperties"] is False

    context = SimpleNamespace(
        conversation=SimpleNamespace(protocol="milky"),
        actions=SimpleNamespace(protocol="milky", capabilities=()),
        actor=SimpleNamespace(is_privileged=True),
        tool_permissions=None,
    )
    available_names = {spec.name for spec in registry.available(context)}
    assert "milky_set_group_essence_message" in available_names
    assert "call_platform_api" not in available_names
    assert "platform_command" not in available_names


def test_milky_api_failure_is_returned_as_failed_tool_result():
    class Actions:
        protocol = "milky"

        def __init__(self, response):
            self.connection = SimpleNamespace(
                http_send=lambda *args, **kwargs: response
            )

    registry = ToolRegistry(
        allowed_risks=frozenset({ToolRisk.READ_ONLY, ToolRisk.MUTATING}),
        allowed_mutating_tools=PLATFORM_API_TOOL_NAMES,
    )
    register_platform_api_tools(registry)
    context = SimpleNamespace(
        conversation=SimpleNamespace(protocol="milky"),
        actions=Actions(
            {"status": "failed", "retcode": 404, "message": "not found"}
        ),
        actor=SimpleNamespace(is_privileged=True),
        tool_permissions=None,
    )
    result = asyncio.run(
        registry.execute(
            ToolCall(
                id="call_essence",
                name="milky_set_group_essence_message",
                arguments={"group_id": 983497968, "message_seq": 3943},
            ),
            context,
        )
    )
    assert not result.ok
    assert result.error_code == "milky_api_failed"
    assert '"ok":false' in result.content


def test_milky_static_tool_rejects_wrong_parameter_types_before_http_call():
    calls = []

    class Actions:
        protocol = "milky"
        connection = SimpleNamespace(
            http_send=lambda *args, **kwargs: calls.append((args, kwargs))
        )

    registry = ToolRegistry(
        allowed_risks=frozenset({ToolRisk.READ_ONLY, ToolRisk.MUTATING}),
        allowed_mutating_tools=PLATFORM_API_TOOL_NAMES,
    )
    register_platform_api_tools(registry)
    context = SimpleNamespace(
        conversation=SimpleNamespace(protocol="milky"),
        actions=Actions(),
        actor=SimpleNamespace(is_privileged=True),
        tool_permissions=None,
    )
    result = asyncio.run(
        registry.execute(
            ToolCall(
                id="call_essence_bad",
                name="milky_set_group_essence_message",
                arguments={"group_id": "983497968", "message_seq": 3943},
            ),
            context,
        )
    )
    assert not result.ok
    assert result.error_code == "invalid_arguments"
    assert not calls


def test_onebot_catalog_is_static_typed_and_arbitrary_action_is_rejected():
    registry = ToolRegistry(
        allowed_risks=frozenset({ToolRisk.READ_ONLY, ToolRisk.MUTATING}),
        allowed_mutating_tools=PLATFORM_API_TOOL_NAMES,
    )
    specs = register_platform_api_tools(registry)
    by_name = {spec.name: spec for spec in specs}
    assert len(ONEBOT_API_TOOL_NAMES) == 38
    assert ONEBOT_API_TOOL_NAMES.issubset(by_name)
    assert "call_platform_api" not in by_name
    ban = by_name["onebot_set_group_ban"]
    assert ban.input_schema["properties"]["group_id"]["type"] == "integer"
    assert ban.input_schema["properties"]["duration"]["type"] == "integer"
    assert ban.input_schema["additionalProperties"] is False

    class Actions:
        protocol = "onebot"

        def __init__(self):
            self.called = []
            self.custom = SimpleNamespace()

    actions = Actions()

    async def call_unknown():
        return await PlatformTransport(actions).call("arbitrary_action", {"anything": 1})

    try:
        asyncio.run(call_unknown())
    except Exception as exc:
        assert getattr(exc, "code", None) == "unknown_action"
    else:
        raise AssertionError("unknown OneBot action was accepted")
    assert parse_platform_command('not_an_action {"anything": 1}') is None


def test_onebot_message_segments_have_fixed_types_and_closed_fields(monkeypatch):
    from plugins.JianerAI.tools import platform_api

    calls = []

    class Custom:
        async def send_group_msg(self, **kwargs):
            calls.append(kwargs)
            return "echo"

    class Actions:
        protocol = "onebot"
        capabilities = ()

        def __init__(self):
            self.custom = Custom()

    monkeypatch.setattr(platform_api, "_onebot_reports", lambda: None)
    registry = ToolRegistry(
        allowed_risks=frozenset({ToolRisk.READ_ONLY, ToolRisk.MUTATING}),
        allowed_mutating_tools=PLATFORM_API_TOOL_NAMES,
    )
    register_platform_api_tools(registry)
    context = SimpleNamespace(
        conversation=SimpleNamespace(protocol="onebot"),
        actions=Actions(),
        actor=SimpleNamespace(is_privileged=True),
        tool_permissions=None,
    )
    valid = asyncio.run(
        registry.execute(
            ToolCall(
                id="call_onebot_send",
                name="onebot_send_group_msg",
                arguments={
                    "group_id": 12345,
                    "message": [{"type": "at", "data": {"qq": "all"}}],
                },
            ),
            context,
        )
    )
    assert valid.ok
    assert calls == [
        {
            "group_id": 12345,
            "message": [{"type": "at", "data": {"qq": "all"}}],
        }
    ]

    invalid = asyncio.run(
        registry.execute(
            ToolCall(
                id="call_onebot_send_bad",
                name="onebot_send_group_msg",
                arguments={
                    "group_id": 12345,
                    "message": [{"type": "made_up", "data": {"anything": 1}}],
                },
            ),
            context,
        )
    )
    assert not invalid.ok
    assert invalid.error_code == "invalid_arguments"
    assert len(calls) == 1


def test_lark_catalog_builds_fixed_message_request_and_checks_api_code():
    assert len(LARK_API_TOOL_NAMES) == len(LARK_API_CATALOG) == 9
    calls = []

    class Client:
        def __init__(self, response):
            self.response = response

        def _request(self, method, path, *, params=None, json_body=None):
            calls.append((method, path, params, json_body))
            return self.response

    actions = SimpleNamespace(
        protocol="lark",
        client=Client({"code": 0, "data": {"message_id": "om_x"}}),
    )
    api = next(api for api in LARK_API_CATALOG if api["name"] == "lark_send_message")
    response = asyncio.run(
        PlatformTransport(actions)._lark(
            api,
            {
                "receive_id_type": "chat_id",
                "receive_id": "oc_chat",
                "text": "hello",
            },
        )
    )
    assert response["code"] == 0
    assert calls[-1][0:3] == (
        "POST",
        "/open-apis/im/v1/messages",
        {"receive_id_type": "chat_id"},
    )
    assert json.loads(calls[-1][3]["content"]) == {"text": "hello"}

    actions.client.response = {"code": 230001, "msg": "not found"}
    try:
        asyncio.run(
            PlatformTransport(actions)._lark(
                next(api for api in LARK_API_CATALOG if api["name"] == "lark_get_message"),
                {"message_id": "om_missing"},
            )
        )
    except Exception as exc:
        assert getattr(exc, "code", None) == "lark_api_failed"
    else:
        raise AssertionError("nonzero Lark OAPI code was reported as success")
