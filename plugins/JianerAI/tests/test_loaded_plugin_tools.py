from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from plugins.JianerAI.tools.contracts import ToolContext, ToolRisk, ToolSpec
from plugins.JianerAI.tools.loaded_plugins import load_active_plugin_tools
from plugins.JianerAI.tools.registry import ToolRegistry


def test_active_plugins_must_explicitly_export_and_tool_calls_hold_generation_lease():
    events = []

    async def run():
        async def handler(context, arguments):
            events.append(("handler", arguments["query"]))
            return {"value": arguments["query"]}

        spec = ToolSpec(
            name="lookup",
            description="Look up an item.",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=handler,
            risk=ToolRisk.READ_ONLY,
        )
        opted_in = SimpleNamespace(
            provide_agent_tools=lambda context: (spec,)
        )
        implicit = SimpleNamespace(internal_helper=lambda: "must not be exposed")
        manager = SimpleNamespace(
            plugins={
                "plugins.example": SimpleNamespace(module=opted_in),
                "plugins.implicit": SimpleNamespace(module=implicit),
            }
        )

        @asynccontextmanager
        async def lease(generation):
            assert generation is manager
            events.append("lease-enter")
            try:
                yield generation
            finally:
                events.append("lease-exit")

        registry = ToolRegistry()
        loaded = await load_active_plugin_tools(
            registry,
            manager,
            runtime={},
            service=object(),
            lease_factory=lease,
        )
        assert loaded == ("plugins.example",)
        context = SimpleNamespace(
            conversation=SimpleNamespace(protocol="test"),
            actions=SimpleNamespace(capabilities=frozenset()),
            actor=None,
            tool_permissions=None,
        )
        available = registry.available(context)
        assert len(available) == 1
        assert available[0].name.startswith("plugin_plugins_example_")
        assert "internal_helper" not in " ".join(item.name for item in available)

        result = await registry.execute(
            SimpleNamespace(
                id="call_1",
                name=available[0].name,
                arguments={"query": "item"},
            ),
            context,
        )
        assert result.ok is True
        assert events == ["lease-enter", ("handler", "item"), "lease-exit"]

    asyncio.run(run())


def test_provider_factory_can_return_a_typed_tool_provider():
    async def run():
        spec = ToolSpec(
            name="status",
            description="Read the plugin status.",
            input_schema={"type": "object", "properties": {}},
            handler=lambda *_: "ready",
        )
        provider = SimpleNamespace(provide_tools=lambda context: (spec,))
        module = SimpleNamespace(create_agent_tool_provider=lambda context: provider)
        manager = SimpleNamespace(
            plugins={"plugins.status": SimpleNamespace(module=module)}
        )

        @asynccontextmanager
        async def lease(generation):
            yield generation

        registry = ToolRegistry()
        loaded = await load_active_plugin_tools(
            registry,
            manager,
            runtime={},
            service=None,
            lease_factory=lease,
        )
        assert loaded == ("plugins.status",)
        assert registry.plugins == ("jianerbot-host-plugin-plugins-status",)

    asyncio.run(run())
