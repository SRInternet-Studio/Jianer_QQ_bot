from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from plugins.JianerAI.tools.contracts import ToolExecutionError, ToolRisk
from plugins.JianerAI.tools.mcp_client import MCPToolPlugin, normalize_input_schema
from plugins.JianerAI.tools.registry import ToolRegistry


class _FakeSession:
    def __init__(self, tools, result=None):
        self.tools = tools
        self.result = result or SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(type="text", text="ok")],
            structuredContent=None,
        )
        self.initialized = 0
        self.calls = []

    async def initialize(self):
        self.initialized += 1

    async def list_tools(self):
        return SimpleNamespace(tools=self.tools)

    async def call_tool(self, *, name, arguments):
        self.calls.append((name, arguments))
        return self.result


def _remote_tool(name, schema=None, description="remote tool"):
    return SimpleNamespace(
        name=name,
        description=description,
        inputSchema=schema
        or {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "query"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    )


def test_mcp_plugin_discovers_namespaced_tools_and_closes_live_sessions():
    async def run():
        session = _FakeSession([_remote_tool("search"), _remote_tool("write")])
        lifecycle = []

        @asynccontextmanager
        async def connector(config):
            lifecycle.append(("open", config.name))
            try:
                yield session
            finally:
                lifecycle.append(("close", config.name))

        plugin = MCPToolPlugin(
            [
                {
                    "name": "docs server",
                    "transport": "stdio",
                    "command": "docs-mcp",
                    "mutating_tools": ["write"],
                }
            ],
            session_connector=connector,
        )
        specs = await plugin.provide_tools()
        assert [spec.name for spec in specs] == [
            "mcp_docs_server_search",
            "mcp_docs_server_write",
        ]
        assert specs[0].risk is ToolRisk.READ_ONLY
        assert specs[1].risk is ToolRisk.MUTATING
        assert session.initialized == 1

        registry = ToolRegistry(
            allowed_risks=frozenset({ToolRisk.READ_ONLY, ToolRisk.MUTATING})
        )
        for spec in specs:
            registry.register(spec)
        result = await specs[0].handler(None, {"query": "mcp"})
        assert result == {"content": [{"type": "text", "text": "ok"}]}
        assert session.calls == [("search", {"query": "mcp"})]

        await plugin.shutdown()
        assert lifecycle == [("open", "docs server"), ("close", "docs server")]

    asyncio.run(run())


def test_mcp_provider_skips_schema_keywords_registry_cannot_validate():
    async def run():
        session = _FakeSession(
            [
                _remote_tool(
                    "supported",
                    {
                        "type": "object",
                        "properties": {
                            "page": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 20,
                            }
                        },
                        "required": [],
                    },
                ),
                _remote_tool(
                    "unsupported",
                    {
                        "type": "object",
                        "properties": {"query": {"type": "string", "format": "uri"}},
                    },
                ),
            ]
        )

        @asynccontextmanager
        async def connector(config):
            yield session

        plugin = MCPToolPlugin(
            [{"name": "schemas", "transport": "streamable_http", "url": "http://mcp"}],
            session_connector=connector,
        )
        specs = await plugin.provide_tools()
        assert [spec.name for spec in specs] == ["mcp_schemas_supported"]
        assert plugin.errors and "unsupported schema keywords" in plugin.errors[0]
        registry = ToolRegistry()
        registry.register(specs[0])
        await plugin.shutdown()

    asyncio.run(run())


def test_mcp_provider_caps_large_results_and_propagates_tool_errors_safely():
    async def run():
        session = _FakeSession(
            [_remote_tool("large")],
            result=SimpleNamespace(
                isError=False,
                content=[SimpleNamespace(type="text", text="x" * 2000)],
                structuredContent=None,
            ),
        )

        @asynccontextmanager
        async def connector(config):
            yield session

        plugin = MCPToolPlugin(
            [
                {
                    "name": "limits",
                    "transport": "stdio",
                    "command": "fake",
                    "max_output_chars": 256,
                }
            ],
            session_connector=connector,
        )
        spec = (await plugin.provide_tools())[0]
        payload = await spec.handler(None, {})
        assert payload["truncated"] is True
        assert len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) <= 256

        session.result = SimpleNamespace(
            isError=True,
            content=[SimpleNamespace(type="text", text="remote failure")],
            structuredContent=None,
        )
        with pytest.raises(ToolExecutionError) as captured:
            await spec.handler(None, {})
        assert getattr(captured.value, "code", None) == "mcp_tool_error"
        assert "remote failure" in str(captured.value)
        await plugin.shutdown()

    asyncio.run(run())


def test_schema_normalization_handles_nullable_properties_and_rejects_refs():
    schema = normalize_input_schema(
        {
            "type": "object",
            "properties": {
                "cursor": {"type": ["string", "null"], "default": None}
            },
            "required": [],
            "title": "ignored metadata",
        }
    )
    assert schema["properties"]["cursor"]["anyOf"] == [
        {"type": "string", "default": None},
        {"type": "null", "default": None},
    ]

    with pytest.raises(ValueError, match="unsupported schema keywords"):
        normalize_input_schema(
            {"type": "object", "properties": {"x": {"$ref": "#/definitions/X"}}}
        )
