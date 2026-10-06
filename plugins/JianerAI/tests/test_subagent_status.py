from __future__ import annotations

import asyncio
from types import SimpleNamespace

from jianer.adapters import ConversationKey, ConversationKind

from plugins.JianerAI.tools.contracts import ToolContext
from plugins.JianerAI.tools.subagent import (
    SubAgentRegistry,
    list_subagents_tool,
    subagent_tool,
)


def _context() -> ToolContext:
    return ToolContext(
        event=SimpleNamespace(),
        actions=SimpleNamespace(capabilities=frozenset()),
        conversation=ConversationKey(
            protocol="milky",
            self_id="bot-1",
            kind=ConversationKind.GROUP,
            conversation_id="group-1",
            preset="default",
        ),
        canonical_user_id="person-1",
        runtime={},
        memory=None,
    )


def test_subagent_registry_filters_by_shared_conversation_scope():
    registry = SubAgentRegistry()
    first = registry.start(("milky", "bot-1", "group", "group-1", "default"), "check one")
    second = registry.start(("milky", "bot-1", "group", "group-2", "default"), "check two")

    active = registry.list_active(("milky", "bot-1", "group", "group-1", "default"))
    assert [item["request_id"] for item in active] == [first]
    assert active[0]["status"] == "queued"
    assert active[0]["goal"] == "check one"

    registry.update(first, status="running")
    assert registry.list_active(("milky", "bot-1", "group", "group-1", "default"))[0]["status"] == "running"
    registry.finish(first)
    registry.finish(second)
    assert registry.list_active(("milky", "bot-1", "group", "group-1", "default")) == ()


def test_subagent_tool_tracks_running_task_and_removes_it_after_completion():
    async def run():
        registry = SubAgentRegistry()
        context = _context()
        started = asyncio.Event()
        release = asyncio.Event()

        class Runner:
            async def run(self, **kwargs):
                started.set()
                await release.wait()
                return "done"

        def runner_factory(**kwargs):
            return Runner()

        spec = subagent_tool(
            providers=None,
            tools=SimpleNamespace(available=lambda _context: ()),
            logger=None,
            model_resolver=lambda _context: "demo-model",
            runner_factory=runner_factory,
            default_timeout_seconds=5,
            registry=registry,
        )
        task = asyncio.create_task(
            spec.handler(
                context,
                {"tasks": [{"goal": "long running lookup"}]},
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        active_tool = list_subagents_tool(registry)
        snapshot = await active_tool.handler(context, {})
        assert snapshot["count"] == 1
        assert snapshot["active"][0]["status"] == "running"
        assert snapshot["active"][0]["goal"] == "long running lookup"

        release.set()
        result = await asyncio.wait_for(task, timeout=1)
        assert result["results"][0]["status"] == "completed"
        assert await active_tool.handler(context, {}) == {"active": [], "count": 0}

    asyncio.run(run())
