from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from jianer.adapters import ConversationKey, ConversationKind

from plugins.JianerAI.agent import AgentRunner
from plugins.JianerAI.providers import (
    AssistantTurn,
    ProviderResponse,
    ProviderToolCall,
)
from plugins.JianerAI.tools import (
    ToolCall,
    ToolContext,
    ToolRegistry,
    ToolRisk,
    register_builtin_tools,
)
from plugins.JianerAI.tools import web_search as web_search_module
from plugins.JianerAI.tools.web_search import web_search_tool


def _context() -> ToolContext:
    return ToolContext(
        event=SimpleNamespace(
            protocol="onebot",
            self_id="bot-search",
            user_id="user-search",
            group_id="group-search",
        ),
        actions=SimpleNamespace(protocol="onebot", capabilities=frozenset()),
        conversation=ConversationKey(
            protocol="onebot",
            self_id="bot-search",
            kind=ConversationKind.GROUP,
            conversation_id="group-search",
            preset="Normal",
        ),
        canonical_user_id="qq:user-search",
        runtime={},
        memory=SimpleNamespace(),
    )


def _fake_ddgs(results=(), *, error: Exception | None = None):
    class FakeDDGS:
        instances = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.calls = []
            self.closed = False
            self.__class__.instances.append(self)

        def text(self, query, **kwargs):
            self.calls.append((query, kwargs))
            if error is not None:
                raise error
            return results

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            self.closed = True

    return FakeDDGS


def _decoded(result):
    return json.loads(result.content)


def test_web_search_normalizes_results_and_honors_backend(monkeypatch):
    async def scenario():
        fake = _fake_ddgs(
            [
                {
                    "title": "  First   result  ",
                    "href": "https://example.com/first",
                    "body": "  First   summary  ",
                },
                {
                    "title": "Duplicate",
                    "href": "https://example.com/first",
                    "body": "ignored",
                },
                {
                    "title": "Second",
                    "url": "http://example.net/second",
                    "snippet": "Second summary",
                },
                {
                    "title": "Unsafe",
                    "href": "javascript:alert(1)",
                    "body": "ignored",
                },
            ]
        )
        monkeypatch.setattr(web_search_module, "_load_ddgs_class", lambda: fake)
        monkeypatch.setenv("DDGS_BACKEND", "GOOGLE")
        registry = ToolRegistry()
        spec = web_search_tool()
        registration = registry.register(spec)
        try:
            result = await registry.execute(
                ToolCall(
                    id="search-1",
                    name="web_search",
                    arguments={
                        "query": "  JianerCore   Agent  ",
                        "max_results": 2,
                        "timelimit": "w",
                    },
                ),
                _context(),
            )
        finally:
            await registry.shutdown()

        assert registration.name == "web_search"
        assert "除非用户明确要求" in spec.description
        assert "不要在最终回答中展示来源或 URL" in spec.description
        assert result.ok is True
        assert result.error_code is None
        payload = _decoded(result)["data"]
        assert payload == {
            "query": "JianerCore Agent",
            "backend": "google",
            "result_count": 2,
            "results": [
                {
                    "title": "First result",
                    "url": "https://example.com/first",
                    "snippet": "First summary",
                },
                {
                    "title": "Second",
                    "url": "http://example.net/second",
                    "snippet": "Second summary",
                },
            ],
        }
        instance = fake.instances[0]
        assert instance.kwargs == {"timeout": 8}
        assert instance.calls == [
            (
                "JianerCore Agent",
                {
                    "region": "cn-zh",
                    "safesearch": "moderate",
                    "timelimit": "w",
                    "max_results": 2,
                    "page": 1,
                    "backend": "google",
                },
            )
        ]
        assert instance.closed is True

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": ""},
        {"query": "x" * 501},
        {"query": "valid", "max_results": 0},
        {"query": "valid", "max_results": 9},
        {"query": "valid", "timelimit": "h"},
    ],
)
def test_web_search_schema_rejects_invalid_arguments(arguments):
    async def scenario():
        registry = ToolRegistry()
        registry.register(web_search_tool())
        try:
            result = await registry.execute(
                ToolCall(id="bad-search", name="web_search", arguments=arguments),
                _context(),
            )
        finally:
            await registry.shutdown()
        assert result.ok is False
        assert result.error_code == "invalid_arguments"

    asyncio.run(scenario())


def test_web_search_empty_results_and_builtin_registration(monkeypatch):
    async def scenario():
        fake = _fake_ddgs([])
        monkeypatch.setattr(web_search_module, "_load_ddgs_class", lambda: fake)
        monkeypatch.delenv("DDGS_BACKEND", raising=False)
        registry = ToolRegistry()
        register_builtin_tools(registry)
        try:
            specs = registry.available(_context())
            assert "web_search" in {spec.name for spec in specs}
            spec = next(spec for spec in specs if spec.name == "web_search")
            assert spec.risk is ToolRisk.READ_ONLY
            assert spec.timeout_seconds == 12.0
            result = await registry.execute(
                ToolCall(
                    id="empty-search",
                    name="web_search",
                    arguments={"query": "no result query"},
                ),
                _context(),
            )
        finally:
            await registry.shutdown()
        assert result.ok is True
        assert _decoded(result)["data"] == {
            "query": "no result query",
            "backend": "auto",
            "result_count": 0,
            "results": [],
        }
        _, kwargs = fake.instances[0].calls[0]
        assert kwargs["max_results"] == 5
        assert kwargs["timelimit"] is None

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["backend", "dependency", "upstream"])
def test_web_search_failures_are_sanitized(monkeypatch, failure):
    async def scenario():
        registry = ToolRegistry()
        registry.register(web_search_tool())
        if failure == "backend":
            monkeypatch.setenv("DDGS_BACKEND", "startpage")
        elif failure == "dependency":
            monkeypatch.setattr(
                web_search_module,
                "_load_ddgs_class",
                lambda: (_ for _ in ()).throw(
                    RuntimeError("private dependency detail")
                ),
            )
        else:
            fake = _fake_ddgs(error=RuntimeError("private upstream response"))
            monkeypatch.setattr(
                web_search_module,
                "_load_ddgs_class",
                lambda: fake,
            )
        try:
            result = await registry.execute(
                ToolCall(
                    id=f"failed-{failure}",
                    name="web_search",
                    arguments={"query": "safe query"},
                ),
                _context(),
            )
        finally:
            await registry.shutdown()
        assert result.ok is False
        assert result.error_code == "tool_failed"
        assert "private" not in result.content
        assert _decoded(result)["message"] == "工具执行失败。"

    asyncio.run(scenario())


def test_agent_calls_web_search_and_uses_returned_url_when_requested(monkeypatch):
    class FakeProviders:
        def __init__(self):
            self.requests = []

        def supports_tools(self, model):
            return True

        async def complete_request(self, model, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                assert [tool.name for tool in request.tools] == ["web_search"]
                call = ProviderToolCall(
                    id="agent-search-1",
                    name="web_search",
                    arguments={"query": "JianerCore", "max_results": 1},
                )
                turn = AssistantTurn(tool_calls=(call,))
                return ProviderResponse("", (call,), turn)
            tool_payload = json.loads(request.turns[-1].content)
            assert tool_payload["data"]["results"][0]["url"] == (
                "https://example.com/article"
            )
            answer = "参考来源：https://example.com/article"
            return ProviderResponse(answer, (), AssistantTurn(text=answer))

        async def chat(self, *args, **kwargs):
            raise AssertionError("Agent should not fall back to plain chat")

    async def scenario():
        fake = _fake_ddgs(
            [
                {
                    "title": "JianerCore",
                    "href": "https://example.com/article",
                    "body": "A source snippet",
                }
            ]
        )
        monkeypatch.setattr(web_search_module, "_load_ddgs_class", lambda: fake)
        monkeypatch.delenv("DDGS_BACKEND", raising=False)
        registry = ToolRegistry()
        registry.register(web_search_tool())
        providers = FakeProviders()
        try:
            answer = await AgentRunner(providers, registry).run(
                model="search-model",
                message="搜索 JianerCore 并给出来源链接",
                history=(),
                system_prompt="搜索后给出来源。",
                attachments=(),
                context=_context(),
                enabled=True,
            )
        finally:
            await registry.shutdown()
        assert answer == "参考来源：https://example.com/article"
        assert len(providers.requests) == 2

    asyncio.run(scenario())
