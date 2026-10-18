import asyncio
import json

import pytest

from plugins.JianerMate.providers import (
    ChatImage,
    ModelEntry,
    OpenAICompatibleClient,
    OPENAI_CHAT_COMPLETIONS,
    ProviderError,
    ResponseParseError,
    extract_json_object,
    load_models,
    normalize_response_type,
    parse_json_object,
)


def test_response_type_normalisation():
    assert normalize_response_type("OpenAI") == OPENAI_CHAT_COMPLETIONS
    assert normalize_response_type("chat_completions") == OPENAI_CHAT_COMPLETIONS
    assert normalize_response_type("Gemini") == "google_generate_content"
    assert normalize_response_type("Anthropic Messages") == "anthropic_messages"


def test_load_models_marks_unsupported_entries(tmp_path):
    (tmp_path / "gpt.ai.json").write_text(
        '{"FriendlyName": "GPT", "Model": "gpt-4o", "ResponseType": "OpenAI",'
        ' "ApiKey": "sk-test", "BaseUrl": "https://api.example.com/v1"}',
        encoding="utf-8",
    )
    (tmp_path / "gemini.ai.json").write_text(
        '{"Model": "gemini-2", "ResponseType": "Google GenerateContent",'
        ' "ApiKey": "x"}',
        encoding="utf-8",
    )
    (tmp_path / "broken.ai.json").write_text("{", encoding="utf-8")

    entries, errors = load_models(tmp_path)
    assert entries["gpt"].supported is True
    assert entries["gpt"].endpoint == "https://api.example.com/v1/chat/completions"
    assert entries["gemini"].supported is False
    assert "不在本插件支持范围内" in entries["gemini"].unsupported_reason
    assert "broken.ai.json" in errors


def test_load_models_reads_multiple_directories(tmp_path):
    first = tmp_path / "aiconfig"
    second = tmp_path / "aiconfig-extra"
    first.mkdir()
    second.mkdir()
    (first / "alpha.ai.json").write_text(
        '{"Model": "m1", "ResponseType": "OpenAI", "ApiKey": "k"}',
        encoding="utf-8",
    )
    (second / "beta.ai.json").write_text(
        '{"Model": "m2", "ResponseType": "Google GenerateContent", "ApiKey": "k"}',
        encoding="utf-8",
    )
    (second / "alpha.ai.json").write_text(
        '{"Model": "m3", "ResponseType": "OpenAI", "ApiKey": "k"}',
        encoding="utf-8",
    )
    entries, errors = load_models([first, second])
    assert set(entries) == {"alpha", "beta"}
    assert entries["alpha"].model == "m1"
    assert entries["beta"].supported is False
    assert any("重复" in message for message in errors.values())


def test_extract_json_object_handles_fences_and_nesting():
    payload = parse_json_object(
        '当然可以：\n```json\n{"speak": true, "topic": "天气 {今天}",'
        ' "nested": {"a": 1}}\n```'
    )
    assert payload["speak"] is True
    assert payload["topic"] == "天气 {今天}"
    with pytest.raises(ValueError):
        extract_json_object("no json here")


def test_parse_json_object_tolerates_trailing_commas():
    payload = parse_json_object(
        '```json\n{"personality": "机娘", "aliases": ["小简",],\n'
        ' "styles": [{"name": "短句",},],}\n```'
    )
    assert payload["personality"] == "机娘"
    assert payload["aliases"] == ["小简"]
    assert payload["styles"] == [{"name": "短句"}]
    # commas inside strings must survive the clean-up untouched
    assert parse_json_object('{"a": "1, 2,", "b": [1, 2],}') == {
        "a": "1, 2,",
        "b": [1, 2],
    }


class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []
        self.closed = False

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        return self.responses.pop(0)

    async def close(self):
        self.closed = True


def _entry(**overrides):
    values = {
        "key": "test",
        "friendly_name": "Test",
        "response_type": OPENAI_CHAT_COMPLETIONS,
        "model": "test-model",
        "api_key": "sk-test",
        "base_url": "https://api.example.com/v1",
    }
    values.update(overrides)
    return ModelEntry(**values)


def test_client_inlines_images_and_retries_without_json_mode():
    async def scenario():
        session = _FakeSession(
            [
                _FakeResponse(400, '{"error": "response_format unsupported"}'),
                _FakeResponse(
                    200,
                    '{"choices": [{"message": {"content": "{\\"speak\\": true}"}}]}',
                ),
            ]
        )
        client = OpenAICompatibleClient(
            {"test": _entry()}, session=session, max_retries=1
        )
        payload = await client.complete_json(
            "test",
            system_prompt="sys",
            user_text="hello",
            images=[ChatImage(data=b"\x89PNG", mime="image/png")],
        )
        assert payload == {"speak": True}
        assert session.posts[0]["json"]["response_format"] == {"type": "json_object"}
        assert "response_format" not in session.posts[1]["json"]
        content = session.posts[0]["json"]["messages"][1]["content"]
        assert content[0]["type"] == "text"
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        await client.close()
        # An injected session belongs to the caller and must not be closed.
        assert session.closed is False

    asyncio.run(scenario())


def test_client_rejects_unsupported_models():
    async def scenario():
        client = OpenAICompatibleClient({}, session=_FakeSession([]))
        with pytest.raises(Exception):
            await client.complete_text(
                "missing", system_prompt="s", user_text="u"
            )

    asyncio.run(scenario())


def test_client_reports_http_errors_with_status_and_retries():
    async def scenario():
        session = _FakeSession(
            [
                _FakeResponse(500, "upstream exploded"),
                _FakeResponse(
                    200, '{"choices": [{"message": {"content": "ok"}}]}'
                ),
            ]
        )
        client = OpenAICompatibleClient(
            {"test": _entry()}, session=session, max_retries=1
        )
        text = await client.complete_text(
            "test", system_prompt="s", user_text="u"
        )
        assert text == "ok"
        assert len(session.posts) == 2

        failing = OpenAICompatibleClient(
            {"test": _entry()},
            session=_FakeSession([_FakeResponse(500, "boom")]),
            max_retries=0,
        )
        with pytest.raises(ProviderError) as captured:
            await failing.complete_text(
                "test", system_prompt="s", user_text="u"
            )
        assert captured.value.status == 500

    asyncio.run(scenario())


def test_client_wraps_unparsable_json():
    async def scenario():
        session = _FakeSession(
            [
                _FakeResponse(
                    200,
                    '{"choices": [{"message": {"content": "不是 JSON"}}]}',
                )
            ]
        )
        client = OpenAICompatibleClient(
            {"test": _entry()}, session=session, max_retries=0
        )
        with pytest.raises(ResponseParseError):
            await client.complete_json(
                "test", system_prompt="s", user_text="u"
            )

    asyncio.run(scenario())


def _tool_call_response(name="get_current_time", arguments="{}"):
    return _FakeResponse(
        200,
        json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": arguments,
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        ),
    )


def test_client_runs_tool_calls_before_json_answer():
    async def scenario():
        session = _FakeSession(
            [
                _tool_call_response(),
                _FakeResponse(
                    200,
                    json.dumps(
                        {
                            "choices": [
                                {
                                    "message": {
                                        "content": '{"messages": ["现在十点"]}'
                                    }
                                }
                            ]
                        }
                    ),
                ),
            ]
        )
        client = OpenAICompatibleClient(
            {"test": _entry()}, session=session, max_retries=0
        )
        calls = []

        async def executor(call_id, name, arguments):
            calls.append((call_id, name, arguments))
            return "2026-10-18 10:00:00"

        payload = await client.complete_json_with_tools(
            "test",
            system_prompt="sys",
            user_text="几点了",
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_current_time",
                        "description": "clock",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            executor=executor,
        )
        assert payload == {"messages": ["现在十点"]}
        assert calls == [("call-1", "get_current_time", {})]
        first_payload = session.posts[0]["json"]
        assert first_payload["tools"][0]["function"]["name"] == "get_current_time"
        follow_up = session.posts[1]["json"]["messages"]
        assert follow_up[-2]["role"] == "assistant"
        assert follow_up[-2]["tool_calls"][0]["id"] == "call-1"
        assert follow_up[-1] == {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "2026-10-18 10:00:00",
        }

    asyncio.run(scenario())


def test_tool_loop_stops_at_max_rounds():
    async def scenario():
        session = _FakeSession([_tool_call_response(), _tool_call_response()])
        client = OpenAICompatibleClient(
            {"test": _entry()}, session=session, max_retries=0
        )

        async def executor(call_id, name, arguments):
            return "ok"

        with pytest.raises(ProviderError) as captured:
            await client.complete_json_with_tools(
                "test",
                system_prompt="sys",
                user_text="几点了",
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "get_current_time",
                            "description": "clock",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
                executor=executor,
                max_rounds=1,
            )
        assert "轮次" in str(captured.value)
        assert len(session.posts) == 2

    asyncio.run(scenario())


def test_client_runs_tools_in_text_mode():
    async def scenario():
        session = _FakeSession(
            [
                _tool_call_response(),
                _FakeResponse(
                    200,
                    json.dumps(
                        {"choices": [{"message": {"content": "结论：可以"}}]}
                    ),
                ),
            ]
        )
        client = OpenAICompatibleClient(
            {"test": _entry()}, session=session, max_retries=0
        )
        calls = []

        async def executor(call_id, name, arguments):
            calls.append(name)
            return "ok"

        text = await client.complete_text_with_tools(
            "test",
            system_prompt="sys",
            user_text="子任务",
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_current_time",
                        "description": "clock",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            executor=executor,
        )
        assert text == "结论：可以"
        assert calls == ["get_current_time"]
        assert "response_format" not in session.posts[0]["json"]

    asyncio.run(scenario())


class _StubBridge:
    available = True

    def __init__(self, payload='{"messages": ["桥接回复"]}'):
        self.payload = payload
        self.calls = []

    def supports(self, key):
        return key == "gemini"

    async def complete_text(self, key, **kwargs):
        self.calls.append({"key": key, **kwargs})
        return self.payload


def test_non_openai_models_are_served_through_the_bridge():
    async def scenario():
        bridge = _StubBridge()
        client = OpenAICompatibleClient({}, bridge=bridge)
        assert client.has_model("gemini") is True
        assert client.serves("gemini") is True
        assert client.serves("missing") is False

        text = await client.complete_text(
            "gemini", system_prompt="sys", user_text="你好"
        )
        assert text == '{"messages": ["桥接回复"]}'
        payload = await client.complete_json(
            "gemini", system_prompt="sys", user_text="你好"
        )
        assert payload == {"messages": ["桥接回复"]}

        # Tools are unavailable on the bridged path; the call still succeeds.
        plan = await client.complete_json_with_tools(
            "gemini",
            system_prompt="sys",
            user_text="你好",
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_current_time",
                        "description": "clock",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            executor=None,
        )
        assert plan == {"messages": ["桥接回复"]}
        assert len(bridge.calls) == 3
        assert all("images" in call for call in bridge.calls)

    asyncio.run(scenario())


def test_real_bridge_reads_the_repo_aiconfig():
    from plugins.JianerMate.providers import JianerAIBridge

    bridge = JianerAIBridge("aiconfig")
    assert bridge.available is True, bridge.error
    # aiconfig/example.ai.json is a Google GenerateContent entry.
    assert bridge.supports("example") is True
    assert "example" in bridge.model_keys()


def test_real_bridge_accepts_multiple_directories(tmp_path):
    from plugins.JianerMate.providers import JianerAIBridge

    extra = tmp_path / "aiconfig-extra"
    extra.mkdir()
    (extra / "bridge-extra.ai.json").write_text(
        json.dumps(
            {
                "FriendlyName": "桥接备用",
                "Model": "gemini-2.5-flash",
                "ResponseType": "Google GenerateContent",
                "ApiKey": "sk-x",
                "BaseUrl": "https://example.invalid/",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    bridge = JianerAIBridge(("aiconfig", extra))
    assert bridge.available is True, bridge.error
    assert bridge.supports("example") is True
    assert bridge.supports("bridge-extra") is True
    assert set(bridge.model_keys()) >= {"example", "bridge-extra"}
    assert bridge.supports("missing") is False


class _ToolBridge(_StubBridge):
    def __init__(self):
        super().__init__('{"messages": ["工具后的结论"]}')
        self.tool_kwargs = None

    def supports_tools(self, key):
        return True

    async def complete_text_with_tools(self, key, **kwargs):
        self.tool_kwargs = {"key": key, **kwargs}
        return self.payload


def test_bridged_models_use_the_tool_loop_when_supported():
    async def scenario():
        bridge = _ToolBridge()
        client = OpenAICompatibleClient({}, bridge=bridge)
        executed = []

        async def executor(call_id, name, arguments):
            executed.append(name)
            return "ok"

        plan = await client.complete_json_with_tools(
            "gemini",
            system_prompt="sys",
            user_text="现在几点",
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_current_time",
                        "description": "clock",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            executor=executor,
        )
        assert plan == {"messages": ["工具后的结论"]}
        assert bridge.tool_kwargs is not None
        assert bridge.tool_kwargs["tools"][0]["function"]["name"] == (
            "get_current_time"
        )
        assert bridge.tool_kwargs["executor"] is executor

    asyncio.run(scenario())
