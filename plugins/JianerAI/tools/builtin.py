from __future__ import annotations

import ast
import asyncio
import logging
import math
import operator
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from plugins.JianerAI.memory import MemoryConflictError, MemoryWriteResult
from plugins.JianerAI.tools.contracts import (
    ToolContext,
    ToolExecutionError,
    ToolRisk,
    ToolSpec,
)
from plugins.JianerAI.tools.github_repository import github_repository_tool
from plugins.JianerAI.tools.html_card import html_card_tool
from plugins.JianerAI.tools.qweather import register_qweather_tools
from plugins.JianerAI.tools.registry import ToolRegistry
from plugins.JianerAI.tools.web_browser import (
    BrowserOptions,
    web_browser_tool,
)
from plugins.JianerAI.tools.web_search import web_search_tool


_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPERATORS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_MAX_ABSOLUTE_NUMBER = 10**100
BUILTIN_MUTATING_TOOL_NAMES = frozenset(
    {"create_my_memory", "update_my_memory"}
)


def register_builtin_tools(
    registry: ToolRegistry,
    *,
    browser_options: BrowserOptions | None = None,
    include_web_browser: bool = True,
    project_root: Path | None = None,
    logger: logging.Logger | None = None,
) -> None:
    registry.register(web_search_tool())
    registry.register(github_repository_tool())
    registry.register(html_card_tool())
    if include_web_browser:
        registry.register(web_browser_tool(browser_options))
    registry.register(
        ToolSpec(
            name="get_current_time",
            description="获取机器人主机当前时区的日期、时间和 UTC 偏移。",
            input_schema=_empty_schema(),
            handler=_get_current_time,
        )
    )
    registry.register(
        ToolSpec(
            name="calculate_expression",
            description="安全计算只包含数字、括号和基础算术运算符的表达式。",
            input_schema={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "要计算的算术表达式。",
                        "minLength": 1,
                        "maxLength": 256,
                    }
                },
                "required": ["expression"],
                "additionalProperties": False,
            },
            handler=_calculate_expression,
        )
    )
    registry.register(
        ToolSpec(
            name="get_current_user_profile",
            description="获取当前发言人的基础资料；不能查询其他用户。",
            input_schema=_empty_schema(),
            handler=_get_current_user_profile,
        )
    )
    registry.register(
        ToolSpec(
            name="get_current_chat_info",
            description="获取当前群聊或私聊会话的基础信息；不能查询其他会话。",
            input_schema=_empty_schema(),
            handler=_get_current_chat_info,
        )
    )
    registry.register(
        ToolSpec(
            name="list_my_memories",
            description=(
                "列出当前角色对当前发言人或当前群保存的长期记忆；"
                "只能访问当前对话范围。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["person", "group"],
                        "description": (
                            "person 表示当前发言人，group 表示当前群；"
                            "私聊只能使用 person。"
                        ),
                        "default": "person",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回条数，范围 1 到 10。",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 5,
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=_list_my_memories,
        )
    )
    registry.register(
        ToolSpec(
            name="create_my_memory",
            description=(
                "为当前角色创建一条值得跨会话保留的记忆。person 只属于当前"
                "发言人，group 只属于当前群；不能替其他用户、群或角色写入。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["person", "group"],
                        "description": (
                            "稳定的个人信息用 person；群内共同约定、长期事件或"
                            "群体关系用 group。私聊只能使用 person。"
                        ),
                        "default": "person",
                    },
                    "content": {
                        "type": "string",
                        "description": "兼容旧调用；新调用请使用 memory_text。",
                        "minLength": 1,
                        "maxLength": 1000,
                    },
                    "canonical_fact": {
                        "type": "string",
                        "description": "用于去重和冲突判断的中性事实摘要。",
                        "minLength": 1,
                        "maxLength": 1000,
                    },
                    "memory_text": {
                        "type": "string",
                        "description": (
                            "以当前角色自己的第一人称语气、价值观和思考方式写成"
                            "的单条、完整、明确记忆。"
                        ),
                        "minLength": 1,
                        "maxLength": 1000,
                    },
                    "importance": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "default": 0.7,
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "default": 0.9,
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=_create_my_memory,
            risk=ToolRisk.MUTATING,
        )
    )
    registry.register(
        ToolSpec(
            name="update_my_memory",
            description=(
                "按 list_my_memories 返回的 scope 和 ID，修正当前角色对当前"
                "发言人或当前群的既有长期记忆。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["person", "group"],
                        "description": "必须与待修改记忆的 scope 相同。",
                        "default": "person",
                    },
                    "memory_id": {
                        "type": "string",
                        "description": "list_my_memories 返回的记忆 ID。",
                        "minLength": 1,
                        "maxLength": 32,
                    },
                    "content": {
                        "type": "string",
                        "description": "兼容旧调用；新调用请使用 memory_text。",
                        "minLength": 1,
                        "maxLength": 1000,
                    },
                    "canonical_fact": {
                        "type": "string",
                        "description": "修正后的中性事实摘要。",
                        "minLength": 1,
                        "maxLength": 1000,
                    },
                    "memory_text": {
                        "type": "string",
                        "description": (
                            "以当前角色自己的第一人称语气、价值观和思考方式写成"
                            "的更正后完整记忆。"
                        ),
                        "minLength": 1,
                        "maxLength": 1000,
                    },
                    "importance": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "default": 0.7,
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "default": 0.9,
                    },
                },
                "required": [
                    "memory_id",
                ],
                "additionalProperties": False,
            },
            handler=_update_my_memory,
            risk=ToolRisk.MUTATING,
        )
    )
    registry.register(
        ToolSpec(
            name="read_recent_chat",
            description=(
                "读取当前群或当前私聊最近的客观聊天记录；只能访问当前会话，"
                "不能指定 QQ 号、群号、表名或其他会话。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 20,
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=_read_recent_chat,
        )
    )
    registry.register(
        ToolSpec(
            name="search_current_chat",
            description=(
                "在当前群或当前私聊 90 天内的客观聊天记录中搜索文字；"
                "查询范围由服务端固定为当前会话。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 200,
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 20,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=_search_current_chat,
        )
    )
    register_qweather_tools(
        registry,
        project_root=(project_root or Path.cwd()).resolve(),
        logger=logger,
    )


def _empty_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


def _get_current_time(context: ToolContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    now = datetime.now().astimezone()
    return {
        "iso": now.isoformat(timespec="seconds"),
        "date": now.date().isoformat(),
        "time": now.time().replace(microsecond=0).isoformat(),
        "utc_offset": now.strftime("%z"),
        "timezone": str(now.tzinfo),
    }


def _calculate_expression(
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    expression = str(arguments["expression"])
    tree = ast.parse(expression, mode="eval")
    value = _evaluate_node(tree.body, depth=0)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("calculation produced a non-finite result")
    if abs(value) > _MAX_ABSOLUTE_NUMBER:
        raise ValueError("calculation result is too large")
    return {"expression": expression, "result": value}


def _evaluate_node(node: ast.AST, *, depth: int) -> int | float:
    if depth > 32:
        raise ValueError("calculation expression is too deeply nested")
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("calculation accepts numbers only")
        if abs(value) > _MAX_ABSOLUTE_NUMBER:
            raise ValueError("calculation input is too large")
        return value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
        return _UNARY_OPERATORS[type(node.op)](
            _evaluate_node(node.operand, depth=depth + 1)
        )
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
        left = _evaluate_node(node.left, depth=depth + 1)
        right = _evaluate_node(node.right, depth=depth + 1)
        if isinstance(node.op, ast.Pow):
            if abs(right) > 100 or abs(left) > 10**6:
                raise ValueError("calculation exponent is too large")
        value = _BINARY_OPERATORS[type(node.op)](left, right)
        if abs(value) > _MAX_ABSOLUTE_NUMBER:
            raise ValueError("calculation result is too large")
        return value
    raise ValueError("calculation contains an unsupported operation")


async def _get_current_user_profile(
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    user_id = str(getattr(context.event, "user_id", "") or "")
    sender = getattr(context.event, "sender", None)
    profile = {
        "user_id": user_id,
        "nickname": str(_value(sender, "nickname") or ""),
        "card": str(_value(sender, "card") or ""),
    }
    method = getattr(context.actions, "get_stranger_info", None)
    if not callable(method) or not user_id:
        profile["source"] = "event"
        return profile
    try:
        response = await method(user_id)
        data = _response_data(response)
        for key in ("user_id", "nickname", "sex", "age"):
            value = _value(data, key)
            if value is not None and value != "":
                profile[key] = str(value) if key == "user_id" else value
        profile["source"] = "adapter"
    except Exception:
        profile["source"] = "event"
        profile["adapter_status"] = "unavailable"
    return profile


async def _get_current_chat_info(
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    key = context.conversation
    output: dict[str, Any] = {
        "protocol": str(key.protocol),
        "kind": key.kind.value,
        "conversation_id": str(key.conversation_id),
        "preset": str(key.preset),
    }
    group_id = getattr(context.event, "group_id", None)
    if group_id is None:
        output["source"] = "event"
        return output
    method = getattr(context.actions, "get_group_info", None)
    if not callable(method):
        output["adapter_status"] = "unsupported"
        return output
    try:
        response = await method(str(group_id))
        data = _response_data(response)
        for name in ("group_id", "group_name", "member_count", "max_member_count"):
            value = _value(data, name)
            if value is not None:
                output[name] = str(value) if name == "group_id" else value
        output["source"] = "adapter"
    except Exception:
        output["adapter_status"] = "unavailable"
    return output


async def _read_recent_chat(
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    return await _current_chat_messages(
        context,
        query="",
        limit=max(1, min(100, int(arguments.get("limit", 20)))),
    )


async def _search_current_chat(
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    query = str(arguments.get("query") or "").strip()
    if not query:
        raise ToolExecutionError(
            "invalid_chat_query",
            "搜索文字不能为空。",
        )
    return await _current_chat_messages(
        context,
        query=query,
        limit=max(1, min(100, int(arguments.get("limit", 20)))),
    )


async def _current_chat_messages(
    context: ToolContext,
    *,
    query: str,
    limit: int,
) -> dict[str, Any]:
    method = getattr(context.memory, "query_recent_chat", None)
    if not callable(method):
        raise ToolExecutionError(
            "chat_history_unavailable",
            "当前聊天记录后端不可用。",
        )
    kind = str(getattr(context.conversation.kind, "value", "") or "")
    records = await asyncio.to_thread(
        method,
        protocol=str(context.conversation.protocol),
        self_id=str(context.conversation.self_id),
        conversation_kind=kind,
        conversation_id=str(context.conversation.conversation_id),
        query=query,
        limit=limit,
        max_characters=8000,
    )
    return {
        "scope": "current_chat",
        "conversation_kind": kind,
        "query": query,
        "count": len(records),
        "messages": [
            {
                "id": str(getattr(record, "id", "")),
                "direction": str(getattr(record, "direction", "incoming")),
                "sender_name": str(getattr(record, "sender_name", "")),
                "sender_person_id": str(
                    getattr(record, "sender_canonical_id", "") or ""
                ),
                "text": str(getattr(record, "content", "")),
                "occurred_at": int(getattr(record, "occurred_at", 0)),
                "message_type": str(
                    getattr(record, "message_type", "text")
                ),
            }
            for record in records
        ],
    }


async def _list_my_memories(
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    limit = max(1, min(10, int(arguments.get("limit", 5))))
    scope, scope_kwargs = _memory_scope_kwargs(context, arguments)
    scoped_list = getattr(context.memory, "list_scoped_memories", None)
    if callable(scoped_list):
        records = await asyncio.to_thread(
            scoped_list,
            scope=scope,
            canonical_user_id=context.canonical_user_id,
            preset=context.conversation.preset,
            limit=limit,
            **scope_kwargs,
        )
    elif scope == "person":
        records = await asyncio.to_thread(
            context.memory.list_memories,
            canonical_user_id=context.canonical_user_id,
            preset=context.conversation.preset,
            limit=limit,
        )
    else:
        raise ToolExecutionError(
            "group_memory_unavailable",
            "当前记忆后端暂不支持群记忆。",
        )
    return {
        "preset": context.conversation.preset,
        "scope": scope,
        "count": len(records),
        "memories": [
            {
                "id": str(record.fact_id),
                "scope": str(getattr(record, "scope", scope)),
                "content": str(record.content),
                "weight": float(record.weight),
                "canonical_fact": str(
                    getattr(record, "canonical_fact", "")
                ),
                "confidence": float(
                    getattr(record, "confidence", 1.0)
                ),
                "source_count": int(
                    getattr(record, "source_count", 0)
                ),
            }
            for record in records
        ],
    }


async def _create_my_memory(
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    content = _memory_content(
        arguments.get("memory_text", arguments.get("content"))
    )
    canonical_fact = _memory_content(
        arguments.get("canonical_fact", content)
    )
    importance = _memory_score(arguments.get("importance", 0.7), 0.7)
    confidence = _memory_score(arguments.get("confidence", 0.9), 0.9)
    style_fields = (
        {
            "canonical_fact": canonical_fact,
            "importance": importance,
            "confidence": confidence,
        }
        if "canonical_fact" in arguments or "memory_text" in arguments
        else {}
    )
    scope, scope_kwargs = _memory_scope_kwargs(context, arguments)
    create_scoped = getattr(context.memory, "create_scoped_memory", None)
    if callable(create_scoped):
        result = await asyncio.to_thread(
            create_scoped,
            scope=scope,
            canonical_user_id=context.canonical_user_id,
            preset=context.conversation.preset,
            content=content,
            **style_fields,
            **scope_kwargs,
        )
    elif scope == "person":
        result = await asyncio.to_thread(
            context.memory.create_memory,
            canonical_user_id=context.canonical_user_id,
            preset=context.conversation.preset,
            content=content,
            **style_fields,
        )
    else:
        raise ToolExecutionError(
            "group_memory_unavailable",
            "当前记忆后端暂不支持群记忆。",
        )
    await _attach_tool_memory_evidence(
        context,
        result,
        scope=scope,
        excerpt=content,
        operation="create",
    )
    return _memory_write_payload(context, result, scope=scope)


async def _update_my_memory(
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    memory_id = str(arguments.get("memory_id") or "").strip()
    if not memory_id.isdigit() or int(memory_id) <= 0:
        raise ToolExecutionError(
            "invalid_memory_id",
            "记忆 ID 必须是 list_my_memories 返回的正整数。",
        )
    content = _memory_content(
        arguments.get("memory_text", arguments.get("content"))
    )
    canonical_fact = _memory_content(
        arguments.get("canonical_fact", content)
    )
    importance = _memory_score(arguments.get("importance", 0.7), 0.7)
    confidence = _memory_score(arguments.get("confidence", 0.9), 0.9)
    style_fields = (
        {
            "canonical_fact": canonical_fact,
            "importance": importance,
            "confidence": confidence,
        }
        if "canonical_fact" in arguments or "memory_text" in arguments
        else {}
    )
    scope, scope_kwargs = _memory_scope_kwargs(context, arguments)
    try:
        update_scoped = getattr(context.memory, "update_scoped_memory", None)
        if callable(update_scoped):
            result = await asyncio.to_thread(
                update_scoped,
                scope=scope,
                canonical_user_id=context.canonical_user_id,
                preset=context.conversation.preset,
                memory_id=memory_id,
                content=content,
                **style_fields,
                **scope_kwargs,
            )
        elif scope == "person":
            result = await asyncio.to_thread(
                context.memory.update_memory,
                canonical_user_id=context.canonical_user_id,
                preset=context.conversation.preset,
                memory_id=memory_id,
                content=content,
                **style_fields,
            )
        else:
            raise ToolExecutionError(
                "group_memory_unavailable",
                "当前记忆后端暂不支持群记忆。",
            )
    except MemoryConflictError as exc:
        raise ToolExecutionError(
            "memory_conflict",
            "当前角色下已经存在内容相同的另一条记忆。",
        ) from exc
    if result is None:
        raise ToolExecutionError(
            "memory_not_found",
            "当前 scope、当前对话和当前角色下找不到这条记忆。",
        )
    await _attach_tool_memory_evidence(
        context,
        result,
        scope=scope,
        excerpt=content,
        operation="update",
    )
    return _memory_write_payload(context, result, scope=scope)


async def _attach_tool_memory_evidence(
    context: ToolContext,
    result: MemoryWriteResult,
    *,
    scope: str,
    excerpt: str,
    operation: str,
) -> None:
    method = getattr(context.memory, "add_scoped_memory_evidence", None)
    if not callable(method):
        return
    event = context.event
    try:
        occurred_at = int(getattr(event, "time", 0) or 0) or None
    except (TypeError, ValueError):
        occurred_at = None
    await asyncio.to_thread(
        method,
        scope=scope,
        canonical_user_id=context.canonical_user_id,
        preset=context.conversation.preset,
        memory_id=result.fact_id,
        protocol=str(context.conversation.protocol),
        self_id=str(context.conversation.self_id),
        conversation_kind=str(
            getattr(context.conversation.kind, "value", "")
        ),
        conversation_id=str(context.conversation.conversation_id),
        message_id=str(getattr(event, "message_id", "") or ""),
        excerpt=excerpt,
        observed_at=occurred_at,
        metadata={"source": "agent_tool", "operation": operation},
    )


def _memory_scope_kwargs(
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> tuple[str, dict[str, str]]:
    scope = str(arguments.get("scope") or "person").strip().casefold()
    if scope not in {"person", "group"}:
        raise ToolExecutionError(
            "invalid_memory_scope",
            "记忆 scope 只能是 person 或 group。",
        )
    if scope == "person":
        return scope, {}
    kind = str(getattr(context.conversation.kind, "value", "") or "")
    if kind != "group":
        raise ToolExecutionError(
            "group_memory_requires_group_chat",
            "私聊中不能创建、查看或修改群记忆。",
        )
    return scope, {
        "protocol": str(context.conversation.protocol),
        "self_id": str(context.conversation.self_id),
        "group_id": str(context.conversation.conversation_id),
    }


def _memory_content(value: Any) -> str:
    content = str(value if value is not None else "").strip()
    if not content:
        raise ToolExecutionError(
            "invalid_memory_content",
            "记忆内容不能为空。",
        )
    return content


def _memory_score(value: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return float(default)


def _memory_write_payload(
    context: ToolContext,
    result: MemoryWriteResult,
    *,
    scope: str,
) -> dict[str, Any]:
    return {
        "preset": context.conversation.preset,
        "scope": str(getattr(result, "scope", scope)),
        "status": str(result.outcome),
        "memory": {
            "id": str(result.fact_id),
            "content": str(result.content),
            "weight": float(result.weight),
        },
    }


def _response_data(response: Any) -> Any:
    if isinstance(response, Mapping):
        return response.get("data", response)
    return getattr(response, "data", response)


def _value(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)
