"""Model-callable platform API tools.

Three tools are exposed:

* ``send_message`` -- high level, protocol-agnostic send that builds a message
  from text / At / reply / image segments and reuses the adapter's ``send``.
* ``call_platform_api`` -- raw passthrough for arbitrary OneBot actions,
  Milky endpoints, or Lark OAPI requests (privileged).
* ``platform_command`` -- natural command parser backed by Alconna that maps
  common operations onto the adapter, with a raw ``action {json}`` fallback.

Everything that mutates remote state requires the acting user to be a group
owner/administrator or a bot administrator.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from arclet.alconna import Alconna, Args, MultiVar
from jianer import common as Manager, segments as Segments

from plugins.JianerAI.tools.contracts import (
    ToolContext,
    ToolExecutionError,
    ToolRisk,
    ToolSpec,
)

SEND_MESSAGE_TOOL_NAME = "send_message"
CALL_PLATFORM_API_TOOL_NAME = "call_platform_api"
PLATFORM_COMMAND_TOOL_NAME = "platform_command"
PLATFORM_API_TOOL_NAMES = frozenset(
    {
        SEND_MESSAGE_TOOL_NAME,
        CALL_PLATFORM_API_TOOL_NAME,
        PLATFORM_COMMAND_TOOL_NAME,
    }
)

_DEFAULT_TIMEOUT_SECONDS = 15.0
_MAX_TEXT_CHARS = 4000
_MAX_ACTION_CHARS = 128


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------


def _actor(context: ToolContext) -> Any:
    return getattr(context, "actor", None)


def _is_privileged(context: ToolContext) -> bool:
    actor = _actor(context)
    if actor is None:
        return True
    return bool(getattr(actor, "is_privileged", False))


def _is_bot_admin(context: ToolContext) -> bool:
    actor = _actor(context)
    if actor is None:
        return True
    return bool(getattr(actor, "is_bot_admin", False))


def _require_privilege(context: ToolContext, action: str) -> None:
    if not _is_privileged(context):
        raise ToolExecutionError(
            "permission_denied",
            f"只有群管理员或机器人管理员才能调用 {action}。",
        )


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def _protocol(actions: Any) -> str:
    return str(getattr(actions, "protocol", "") or "").strip().casefold()


def _numeric_id(value: Any, field: str) -> str:
    text = str(value if value is not None else "").strip()
    if not text or not text.lstrip("-").isdigit():
        raise ToolExecutionError(
            "invalid_target",
            f"{field} 必须是数字 ID，收到：{value!r}",
        )
    return text


class PlatformTransport:
    """Dispatches a raw action/endpoint to the active adapter transport."""

    def __init__(
        self,
        actions: Any,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.actions = actions
        self.protocol = _protocol(actions)
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    async def call(
        self,
        action: str,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        name = str(action or "").strip()
        if not name:
            raise ToolExecutionError("invalid_action", "action 不能为空。")
        if len(name) > _MAX_ACTION_CHARS:
            raise ToolExecutionError("invalid_action", "action 过长。")
        payload = dict(params or {})
        if self.protocol == "onebot":
            return await self._onebot(name, payload)
        if self.protocol == "milky":
            return await self._milky(name, payload)
        if self.protocol in {"feishu", "lark"}:
            return await self._lark(name, payload)
        raise ToolExecutionError(
            "unsupported_protocol",
            f"当前协议 {self.protocol or '未知'} 不支持平台 API 调用。",
        )

    async def _onebot(
        self,
        action: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        custom = getattr(self.actions, "custom", None)
        if custom is None:
            raise ToolExecutionError(
                "unsupported_protocol",
                "当前 OneBot 连接不支持自定义 action。",
            )
        wrapper = getattr(custom, action, None)
        if not callable(wrapper):
            raise ToolExecutionError(
                "unknown_action",
                f"未知的 OneBot action：{action}",
            )
        echo = str(await wrapper(**params))
        reports = _onebot_reports()
        if reports is None:
            return {"echo": echo, "submitted": True}
        try:
            response = await reports.get_async(echo, self.timeout_seconds)
        except TimeoutError:
            return {
                "echo": echo,
                "submitted": True,
                "timeout": True,
                "message": f"等待 {action} 响应超时。",
            }
        return _as_dict(response)

    async def _milky(
        self,
        endpoint: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        connection = getattr(self.actions, "connection", None)
        sender = getattr(connection, "http_send", None)
        if callable(sender):
            result = await asyncio.to_thread(
                sender,
                endpoint,
                params,
                timeout_seconds=self.timeout_seconds,
                attempts=3,
            )
            return _as_dict(result)
        custom = getattr(self.actions, "custom", None)
        if custom is None:
            raise ToolExecutionError(
                "unsupported_protocol",
                "当前 Milky 连接不支持自定义 endpoint。",
            )
        wrapper = getattr(custom, endpoint, None)
        if not callable(wrapper):
            raise ToolExecutionError(
                "unknown_action",
                f"未知的 Milky endpoint：{endpoint}",
            )
        echo = str(await wrapper(**params))
        reports = _milky_reports()
        if reports is None:
            return {"echo": echo, "submitted": True}
        try:
            response = await asyncio.to_thread(
                reports.get,
                echo,
                self.timeout_seconds,
            )
        except TimeoutError:
            return {
                "echo": echo,
                "submitted": True,
                "timeout": True,
                "message": f"等待 {endpoint} 响应超时。",
            }
        return _as_dict(response)

    async def _lark(
        self,
        action: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        client = getattr(self.actions, "client", None)
        request = getattr(client, "_request", None)
        if not callable(request):
            raise ToolExecutionError(
                "unsupported_protocol",
                "当前飞书连接不支持原始 OAPI 调用。",
            )
        method = str(params.pop("method", "POST") or "POST").upper()
        path = str(params.pop("path", "") or action).strip()
        query = params.pop("params", None)
        body = params.pop("json", None)
        if body is None:
            body = params.pop("body", None)
        if body is None and params:
            body = params
        if not path.startswith("/"):
            raise ToolExecutionError(
                "invalid_action",
                "Lark OAPI 需要以 / 开头的接口路径，例如 "
                "/open-apis/im/v1/messages。",
            )
        try:
            result = await asyncio.to_thread(
                request,
                method,
                path,
                params=query if isinstance(query, Mapping) else None,
                json_body=body if isinstance(body, Mapping) else None,
            )
        except Exception as exc:  # noqa: BLE001 - surface the API error to the model
            return {
                "status": "failed",
                "message": str(exc)[:500],
            }
        return _as_dict(result)


def _onebot_reports() -> Any | None:
    try:
        from jianer.LecAdapters.OneBotLib.Manager import reports
    except Exception:
        return None
    return reports


def _milky_reports() -> Any | None:
    try:
        from jianer.LecAdapters.MilkyLib.Manager import reports
    except Exception:
        return None
    return reports


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    data = getattr(value, "data", None)
    if isinstance(data, Mapping):
        return {str(key): item for key, item in data.items()}
    if hasattr(value, "__dict__"):
        return {
            str(key): item
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return {"data": value}


# ---------------------------------------------------------------------------
# High level send
# ---------------------------------------------------------------------------


def _current_target(context: ToolContext) -> dict[str, str]:
    conversation = context.conversation
    if conversation.kind.value == "group":
        return {"group_id": str(conversation.conversation_id)}
    return {"user_id": str(conversation.conversation_id)}


def _resolve_target(
    context: ToolContext,
    group_id: Any,
    user_id: Any,
) -> dict[str, str]:
    current = _current_target(context)
    if group_id is None and user_id is None:
        return current
    if not _is_bot_admin(context):
        raise ToolExecutionError(
            "cross_conversation_denied",
            "只有机器人管理员才能向其他会话发送消息。",
        )
    if group_id is not None:
        return {"group_id": _numeric_id(group_id, "group_id")}
    return {"user_id": _numeric_id(user_id, "user_id")}


def _build_message(arguments: Mapping[str, Any]) -> Any:
    text = str(arguments.get("text") or "").strip()
    if not text:
        raise ToolExecutionError("invalid_text", "消息内容不能为空。")
    if len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS]
    segments: list[Any] = []
    reply_to = arguments.get("reply_to_message_id")
    if reply_to:
        segments.append(Segments.Reply(str(reply_to)))
    raw_mentions = arguments.get("at_user_ids")
    mentions: Sequence[Any] = (
        raw_mentions
        if isinstance(raw_mentions, Sequence)
        and not isinstance(raw_mentions, (str, bytes))
        else ()
    )
    for user_id in mentions:
        value = str(user_id).strip()
        if value:
            segments.append(Segments.At(value))
    for url in arguments.get("image_urls") or ():
        value = str(url).strip()
        if value:
            segments.append(Segments.Image(value))
    segments.append(Segments.Text(text))
    return Manager.Message(*segments)


async def _send_message(
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    message = _build_message(arguments)
    target = _resolve_target(
        context,
        arguments.get("group_id"),
        arguments.get("user_id"),
    )
    sender = getattr(context.actions, "send", None)
    if not callable(sender):
        raise ToolExecutionError(
            "unsupported_protocol",
            "当前连接不支持发送消息。",
        )
    result = await sender(message=message, **target)
    data = getattr(result, "data", None)
    message_id = str(getattr(data, "message_id", "") or "")
    return {
        "sent": True,
        "target": target,
        "message_id": message_id,
    }


async def _call_platform_api(
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    _require_privilege(context, "call_platform_api")
    action = str(arguments.get("action") or "").strip()
    params = arguments.get("params") or {}
    if not isinstance(params, Mapping):
        raise ToolExecutionError("invalid_params", "params 必须是 JSON 对象。")
    transport = PlatformTransport(context.actions)
    result = await transport.call(action, params)
    return {
        "protocol": transport.protocol,
        "action": action,
        "result": result,
    }


# ---------------------------------------------------------------------------
# Alconna command parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    action: str
    params: dict[str, Any]


_COMMANDS: tuple[tuple[Alconna, str], ...] = (
    (
        Alconna("发送群消息", Args["group_id", str], Args["text", MultiVar(str)]),
        "send_group_message",
    ),
    (
        Alconna(
            "发送私聊消息",
            Args["user_id", str],
            Args["text", MultiVar(str)],
        ),
        "send_private_message",
    ),
    (
        Alconna("撤回消息", Args["message_id", str]),
        "delete_msg",
    ),
    (
        Alconna(
            "禁言成员",
            Args["group_id", str],
            Args["user_id", str],
            Args["duration", int, 60],
        ),
        "set_group_ban",
    ),
    (
        Alconna("查询群成员", Args["group_id", str], Args["user_id", str]),
        "get_group_member_info",
    ),
    (
        Alconna("查询群信息", Args["group_id", str]),
        "get_group_info",
    ),
    (
        Alconna("查询登录信息"),
        "get_login_info",
    ),
)


def parse_platform_command(text: str) -> ParsedCommand | None:
    """Parse a natural command into an action + params pair."""

    value = str(text or "").strip()
    if not value:
        return None
    for command, action in _COMMANDS:
        result = command.parse(value)
        if not result.matched:
            continue
        params = dict(result.main_args or {})
        return ParsedCommand(action=action, params=_normalize_params(params))
    head, _, tail = value.partition(" ")
    tail = tail.strip()
    if tail.startswith("{"):
        try:
            params = json.loads(tail)
        except json.JSONDecodeError:
            return None
        if isinstance(params, Mapping):
            return ParsedCommand(action=head, params=dict(params))
    return None


def _normalize_params(params: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in params.items():
        if key == "duration":
            output["duration"] = int(value)
        elif key == "text" and isinstance(value, (tuple, list)):
            output["text"] = " ".join(str(item) for item in value)
        else:
            output[key] = value
    return output


async def _platform_command(
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    _require_privilege(context, "platform_command")
    command = str(arguments.get("command") or "").strip()
    if not command:
        raise ToolExecutionError("invalid_command", "command 不能为空。")
    parsed = parse_platform_command(command)
    if parsed is None:
        raise ToolExecutionError(
            "unparsed_command",
            "无法解析该命令；可改用 call_platform_api 直接传 action 与 params。",
        )
    transport = PlatformTransport(context.actions)
    result = await transport.call(parsed.action, parsed.params)
    return {
        "command": command,
        "action": parsed.action,
        "params": parsed.params,
        "protocol": transport.protocol,
        "result": result,
    }


# ---------------------------------------------------------------------------
# Tool specs
# ---------------------------------------------------------------------------


def send_message_tool() -> ToolSpec:
    return ToolSpec(
        name=SEND_MESSAGE_TOOL_NAME,
        description=(
            "向当前会话发送一条消息，可以 At 指定用户、引用消息或附带图片。"
            "给群里其他人发消息时优先使用这个工具。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "消息正文。",
                    "minLength": 1,
                    "maxLength": _MAX_TEXT_CHARS,
                },
                "at_user_ids": {
                    "type": "array",
                    "description": "需要 At 的用户 ID 列表。",
                    "items": {"type": "string"},
                    "maxItems": 20,
                },
                "reply_to_message_id": {
                    "type": "string",
                    "description": "要引用的消息 ID。",
                },
                "image_urls": {
                    "type": "array",
                    "description": "要发送的图片链接列表。",
                    "items": {"type": "string"},
                    "maxItems": 5,
                },
                "group_id": {
                    "type": "string",
                    "description": "目标群 ID，仅机器人管理员可指定其他会话。",
                },
                "user_id": {
                    "type": "string",
                    "description": "目标用户 ID，仅机器人管理员可指定其他会话。",
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        handler=_send_message,
        risk=ToolRisk.MUTATING,
        timeout_seconds=20.0,
        max_output_chars=4000,
    )


def call_platform_api_tool() -> ToolSpec:
    return ToolSpec(
        name=CALL_PLATFORM_API_TOOL_NAME,
        description=(
            "直接调用当前协议的原始接口（需要群管理员或机器人管理员）。"
            "OneBot 传 action 与 params；Milky 传 endpoint 与 params；"
            "Lark 传 /open-apis/... 路径，可用 method/params/json 指定请求细节。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "OneBot action、Milky endpoint 或 Lark OAPI 路径。",
                    "minLength": 1,
                    "maxLength": _MAX_ACTION_CHARS,
                },
                "params": {
                    "type": "object",
                    "description": "传给接口的参数对象。",
                    "additionalProperties": True,
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        handler=_call_platform_api,
        risk=ToolRisk.MUTATING,
        required_privilege=True,
        timeout_seconds=20.0,
        max_output_chars=8000,
    )


def platform_command_tool() -> ToolSpec:
    return ToolSpec(
        name=PLATFORM_COMMAND_TOOL_NAME,
        description=(
            "用自然语言命令调用常用平台能力（需要群管理员或机器人管理员），"
            "例如：发送群消息 12345 你好；撤回消息 678；"
            "禁言成员 12345 678 60；查询群成员 12345 678。"
            "无法解析时会尝试 action {json} 形式的原始调用。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的平台命令。",
                    "minLength": 1,
                    "maxLength": 1000,
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        handler=_platform_command,
        risk=ToolRisk.MUTATING,
        required_privilege=True,
        timeout_seconds=20.0,
        max_output_chars=8000,
    )


def register_platform_api_tools(registry: Any) -> tuple[ToolSpec, ...]:
    specs = (
        send_message_tool(),
        call_platform_api_tool(),
        platform_command_tool(),
    )
    for spec in specs:
        registry.register(spec)
    return specs


__all__ = [
    "CALL_PLATFORM_API_TOOL_NAME",
    "PLATFORM_API_TOOL_NAMES",
    "PLATFORM_COMMAND_TOOL_NAME",
    "SEND_MESSAGE_TOOL_NAME",
    "ParsedCommand",
    "PlatformTransport",
    "call_platform_api_tool",
    "parse_platform_command",
    "platform_command_tool",
    "register_platform_api_tools",
    "send_message_tool",
]
