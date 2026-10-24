"""Context management and cross-context communication tools."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jianer.adapters import ConversationKind

from plugins.JianerAI.agent_runtime.models import SessionKey
from plugins.JianerAI.agent_runtime.sessions import AgentSessionManager
from plugins.JianerAI.tools.contracts import (
    ToolContext,
    ToolExecutionError,
    ToolRisk,
    ToolSpec,
)

CONTEXT_TOOL_NAMES = frozenset(
    {
        "context_list",
        "context_status",
        "context_read",
        "context_send",
        "context_request",
        "context_reply",
        "summary",
        "switch_profile",
    }
)


def context_tools(
    sessions: AgentSessionManager,
    profiles: Any,
) -> tuple[ToolSpec, ...]:
    async def list_contexts(
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> str:
        del context, arguments
        return await sessions.list_contexts()

    async def status(
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> str:
        target = _target(context, arguments)
        return await sessions.context_status(target)

    async def read(
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        target = _target(context, arguments)
        return await sessions.read_context(
            target,
            limit=max(1, min(50, int(arguments.get("limit", 20)))),
        )

    async def send(
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> str:
        source = _source(context)
        target = sessions.parse_target(str(arguments.get("target") or ""))
        return await sessions.send_context(
            source,
            target,
            str(arguments.get("content") or ""),
        )

    async def request(
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> str:
        source = _source(context)
        target = sessions.parse_target(str(arguments.get("target") or ""))
        return await sessions.send_context(
            source,
            target,
            str(arguments.get("content") or ""),
            kind="request",
        )

    async def reply(
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> str:
        source = _source(context)
        target = sessions.parse_target(str(arguments.get("target") or ""))
        return await sessions.send_context(
            source,
            target,
            str(arguments.get("content") or ""),
            kind="reply",
            request_id=str(arguments.get("request_id") or ""),
        )

    async def summary(
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> str:
        target = _target(context, arguments)
        return await sessions.summarize(target, source="System Context")

    async def switch_profile(
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> str:
        del context
        name = str(arguments.get("name") or "").strip()
        if not name:
            raise ToolExecutionError("invalid_profile", "name 不能为空。")
        return await sessions.switch_profile(name)

    return (
        ToolSpec(
            name="context_list",
            description="列出所有已加载的 Main Context。",
            input_schema=_empty_schema(),
            handler=list_contexts,
            scenes=frozenset({"group", "private", "system"}),
            group="context",
            sub_visible=False,
            system_visible=True,
        ),
        ToolSpec(
            name="context_status",
            description="查看指定 Main Context 的状态。",
            input_schema=_target_schema(),
            handler=status,
            scenes=frozenset({"group", "private", "system"}),
            group="context",
            sub_visible=False,
            system_visible=True,
        ),
        ToolSpec(
            name="context_read",
            description="读取指定 Main Context 的最近原始历史。",
            input_schema=_target_schema(limit=True),
            handler=read,
            scenes=frozenset({"group", "private", "system"}),
            group="context",
            sub_visible=False,
            system_visible=True,
        ),
        ToolSpec(
            name="context_send",
            description="向另一个 Main Context 投递消息。",
            input_schema=_message_schema(),
            handler=send,
            risk=ToolRisk.MUTATING,
            scenes=frozenset({"group", "private", "system"}),
            group="context",
            sub_visible=False,
            system_visible=True,
        ),
        ToolSpec(
            name="context_request",
            description="请求另一个 Main Context 处理并答复。",
            input_schema=_message_schema(),
            handler=request,
            risk=ToolRisk.MUTATING,
            scenes=frozenset({"group", "private", "system"}),
            group="context",
            sub_visible=False,
            system_visible=True,
        ),
        ToolSpec(
            name="context_reply",
            description="回复另一 Context 的 request_id。",
            input_schema={
                **_message_schema(),
                "properties": {
                    **_message_schema()["properties"],
                    "request_id": {"type": "string", "minLength": 1},
                },
                "required": ["target", "content", "request_id"],
            },
            handler=reply,
            risk=ToolRisk.MUTATING,
            scenes=frozenset({"group", "private", "system"}),
            group="context",
            sub_visible=False,
            system_visible=True,
        ),
        ToolSpec(
            name="summary",
            description="请 System Context 总结指定 Main Context。",
            input_schema=_target_schema(),
            handler=summary,
            perm="bot_owner",
            scenes=frozenset({"system"}),
            group="memory",
            main_visible=False,
            sub_visible=False,
            system_visible=True,
        ),
        ToolSpec(
            name="switch_profile",
            description="全局切换人设并总结所有已加载 Main Context。",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1}
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            handler=switch_profile,
            perm="bot_owner",
            scenes=frozenset({"system"}),
            group="memory",
            main_visible=False,
            sub_visible=False,
            system_visible=True,
        ),
    )


def _source(context: ToolContext) -> SessionKey:
    kind = context.conversation.kind
    return SessionKey(
        scene_type=(
            "group" if kind is ConversationKind.GROUP else "private"
        ),
        scene_id=str(context.conversation.conversation_id),
        protocol=str(context.conversation.protocol),
        self_id=str(context.conversation.self_id),
    )


def _target(
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> SessionKey:
    value = str(arguments.get("target") or "").strip()
    return (
        AgentSessionManager.parse_target(value)
        if value
        else _source(context)
    )


def _empty_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


def _target_schema(*, limit: bool = False) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "target": {
            "type": "string",
            "description": "protocol:self_id:scene_type:scene_id",
        }
    }
    required = ["target"]
    if limit:
        properties["limit"] = {
            "type": "integer",
            "minimum": 1,
            "maximum": 50,
            "default": 20,
        }
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _message_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "protocol:self_id:scene_type:scene_id",
            },
            "content": {"type": "string", "minLength": 1},
        },
        "required": ["target", "content"],
        "additionalProperties": False,
    }
