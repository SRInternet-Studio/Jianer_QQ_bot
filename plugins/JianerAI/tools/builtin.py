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

from plugins.JianerAI.tools.contracts import ToolContext, ToolSpec
from plugins.JianerAI.tools.github_repository import github_repository_tool
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
            description="列出当前发言人在当前角色预设下保存的长期记忆。",
            input_schema={
                "type": "object",
                "properties": {
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


async def _list_my_memories(
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    limit = max(1, min(10, int(arguments.get("limit", 5))))
    records = await asyncio.to_thread(
        context.memory.list_memories,
        canonical_user_id=context.canonical_user_id,
        preset=context.conversation.preset,
        limit=limit,
    )
    return {
        "preset": context.conversation.preset,
        "count": len(records),
        "memories": [
            {
                "id": str(record.fact_id),
                "content": str(record.content),
                "weight": float(record.weight),
            }
            for record in records
        ],
    }


def _response_data(response: Any) -> Any:
    if isinstance(response, Mapping):
        return response.get("data", response)
    return getattr(response, "data", response)


def _value(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)
