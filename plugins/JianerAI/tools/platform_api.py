"""Model-callable messaging tools and statically declared platform APIs.

OneBot and Milky use endpoint-specific API catalogs. Lark exposes a fixed set
of message operations. Mutating platform APIs require an authorized actor.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from arclet.alconna import Alconna, Args, MultiVar
from jianer import common as Manager, segments as Segments

from plugins.JianerAI.tools.contracts import (
    ToolContext,
    ToolExecutionError,
    ToolRisk,
    ToolSpec,
)
from plugins.JianerAI.tools.onebot_api_catalog import (
    ONEBOT_API_CATALOG,
    ONEBOT_API_ENDPOINTS,
    ONEBOT_API_TOOL_NAMES,
)
from plugins.JianerAI.tools.lark_api_catalog import (
    LARK_API_CATALOG,
    LARK_API_TOOL_NAMES,
)

SEND_MESSAGE_TOOL_NAME = "send_message"
PLATFORM_COMMAND_TOOL_NAME = "platform_command"
_MILKY_CATALOG_PATH = Path(__file__).with_name("milky_api_catalog.json")
with _MILKY_CATALOG_PATH.open("r", encoding="utf-8") as _catalog_file:
    _MILKY_CATALOG = json.load(_catalog_file)
MILKY_API_CATALOG = tuple(_MILKY_CATALOG["apis"])
MILKY_API_ENDPOINTS = frozenset(str(item["name"]) for item in MILKY_API_CATALOG)
MILKY_API_TOOL_NAMES = frozenset(f"milky_{name}" for name in MILKY_API_ENDPOINTS)
PLATFORM_API_TOOL_NAMES = frozenset(
    {
        SEND_MESSAGE_TOOL_NAME,
        PLATFORM_COMMAND_TOOL_NAME,
        *MILKY_API_TOOL_NAMES,
        *ONEBOT_API_TOOL_NAMES,
        *LARK_API_TOOL_NAMES,
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
            raise ToolExecutionError(
                "static_api_only",
                "Lark OAPI 必须通过对应的静态 lark_* 工具调用。",
            )
        raise ToolExecutionError(
            "unsupported_protocol",
            f"当前协议 {self.protocol or '未知'} 不支持平台 API 调用。",
        )

    async def _onebot(
        self,
        action: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if action not in ONEBOT_API_ENDPOINTS:
            raise ToolExecutionError(
                "unknown_action",
                f"未注册的 OneBot V11 API：{action}",
            )
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
            raise ToolExecutionError(
                "onebot_api_timeout",
                f"等待 OneBot API {action} 的响应超时。",
            )
        result = _as_dict(response)
        status = str(result.get("status") or "")
        retcode = result.get("retcode")
        if status == "async" and retcode == 1:
            return {**result, "completed": False, "submitted": True}
        if status != "ok" or isinstance(retcode, bool) or retcode != 0:
            message = str(
                result.get("msg") or result.get("wording") or "接口返回失败"
            )[:300]
            raise ToolExecutionError(
                "onebot_api_failed",
                f"OneBot API {action} 失败（status={status or 'missing'}, "
                f"retcode={retcode!r}）：{message}",
            )
        return result

    async def _milky(
        self,
        endpoint: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if endpoint not in MILKY_API_ENDPOINTS:
            raise ToolExecutionError(
                "unknown_action",
                f"未注册的 Milky API：{endpoint}",
            )
        connection = getattr(self.actions, "connection", None)
        sender = getattr(connection, "http_send", None)
        if not callable(sender):
            raise ToolExecutionError(
                "unsupported_protocol",
                "当前 Milky 连接不支持 HTTP API 调用。",
            )
        try:
            response = await asyncio.to_thread(
                sender,
                endpoint,
                params,
                timeout_seconds=self.timeout_seconds,
                attempts=3,
            )
        except TimeoutError:
            raise
        except Exception as exc:  # noqa: BLE001 - return a model-safe API error
            raise ToolExecutionError(
                "milky_api_failed",
                f"Milky API {endpoint} 请求失败：{str(exc)[:300]}",
            ) from exc
        result = _as_dict(response)
        status = str(result.get("status") or "")
        retcode = result.get("retcode")
        if status != "ok" or isinstance(retcode, bool) or retcode != 0:
            message = str(result.get("message") or "接口返回失败")[:300]
            raise ToolExecutionError(
                "milky_api_failed",
                f"Milky API {endpoint} 失败（status={status or 'missing'}, "
                f"retcode={retcode!r}）：{message}",
            )
        return result

    async def _lark(
        self,
        api: Mapping[str, Any],
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        client = getattr(self.actions, "client", None)
        request = getattr(client, "_request", None)
        if not callable(request):
            raise ToolExecutionError(
                "unsupported_protocol",
                "当前飞书连接不支持原始 OAPI 调用。",
            )
        method = str(api["method"])
        path = str(api["path"])
        for name in api.get("path_parameters", ()):
            path = path.replace(
                "{" + str(name) + "}",
                quote(str(arguments[name]), safe=""),
            )
        query = {
            str(name): arguments[name]
            for name in api.get("query_parameters", ())
            if name in arguments
        }
        if api["name"] == "lark_send_message":
            body = {
                "msg_type": "text",
                "content": json.dumps({"text": arguments["text"]}, ensure_ascii=False),
            }
            if "uuid" in arguments:
                body["uuid"] = arguments["uuid"]
        elif api["name"] == "lark_reply_message":
            body = {
                "msg_type": "text",
                "content": json.dumps({"text": arguments["text"]}, ensure_ascii=False),
            }
            for name in ("reply_in_thread", "uuid"):
                if name in arguments:
                    body[name] = arguments[name]
        elif api["name"] == "lark_add_reaction":
            body = {"reaction_type": {"emoji_type": arguments["emoji_type"]}}
        else:
            body = None
        if api["name"] == "lark_list_messages":
            query["container_id_type"] = "chat"
        try:
            result = await asyncio.to_thread(
                request,
                method,
                path,
                params=query or None,
                json_body=body,
            )
        except Exception as exc:  # noqa: BLE001 - surface the API error to the model
            raise ToolExecutionError(
                "lark_api_failed",
                f"Lark API {api['name']} 请求失败：{str(exc)[:300]}",
            ) from exc
        response = _as_dict(result)
        code = response.get("code")
        if isinstance(code, bool) or code != 0:
            message = str(response.get("msg") or "接口返回失败")[:300]
            raise ToolExecutionError(
                "lark_api_failed",
                f"Lark API {api['name']} 失败（code={code!r}）：{message}",
            )
        return response


def _onebot_reports() -> Any | None:
    try:
        from jianer.LecAdapters.OneBotLib.Manager import reports
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
    sink = getattr(context, "message_sink", None)
    if sink is not None and callable(getattr(sink, "send", None)):
        target = _resolve_target(
            context,
            arguments.get("group_id"),
            arguments.get("user_id"),
        )
        result = await sink.send(
            str(arguments.get("text") or ""),
            at_user_ids=arguments.get("at_user_ids") or (),
            reply_to_message_id=arguments.get("reply_to_message_id"),
            image_urls=arguments.get("image_urls") or (),
            target=target,
            source="tool",
        )
        message_ids = result if isinstance(result, (list, tuple)) else []
        return {
            "sent": True,
            "target": target,
            "message_ids": [str(item) for item in message_ids if str(item)],
        }
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
        "send_group_msg",
    ),
    (
        Alconna(
            "发送私聊消息",
            Args["user_id", str],
            Args["text", MultiVar(str)],
        ),
        "send_private_msg",
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
            Args["duration", int, 1800],
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
    return None


def _normalize_params(params: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in params.items():
        if key in {"duration", "group_id", "user_id", "message_id"}:
            output[key] = int(value)
        elif key == "text" and isinstance(value, (tuple, list)):
            output["message"] = " ".join(str(item) for item in value)
        else:
            output[key] = value
    return output


async def _platform_command(
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    _require_privilege(context, "platform_command")
    if _protocol(context.actions) != "onebot":
        raise ToolExecutionError(
            "unsupported_protocol",
            "platform_command 仅支持 OneBot V11 的固定命令。",
        )
    command = str(arguments.get("command") or "").strip()
    if not command:
        raise ToolExecutionError("invalid_command", "command 不能为空。")
    parsed = parse_platform_command(command)
    if parsed is None:
        raise ToolExecutionError(
            "unparsed_command",
            "无法解析该命令；请使用对应的静态 OneBot API 工具。",
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


def platform_command_tool() -> ToolSpec:
    return ToolSpec(
        name=PLATFORM_COMMAND_TOOL_NAME,
        description=(
            "用自然语言命令调用常用平台能力（需要群管理员或机器人管理员），"
            "例如：发送群消息 12345 你好；撤回消息 678；"
            "禁言成员 12345 678 60；查询群成员 12345 678。"
            "命令仅映射到固定的 OneBot V11 API。"
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
        supported_protocols=frozenset({"onebot"}),
        timeout_seconds=20.0,
        max_output_chars=8000,
    )


def register_platform_api_tools(registry: Any) -> tuple[ToolSpec, ...]:
    specs = [
        send_message_tool(),
        platform_command_tool(),
    ]
    for api in MILKY_API_CATALOG:
        endpoint = str(api["name"])
        risk = ToolRisk.READ_ONLY if endpoint.startswith("get_") else ToolRisk.MUTATING

        async def invoke_milky_api(
            context: ToolContext,
            arguments: Mapping[str, Any],
            *,
            endpoint: str = endpoint,
        ) -> dict[str, Any]:
            _require_privilege(context, f"milky_{endpoint}")
            transport = PlatformTransport(context.actions)
            result = await transport.call(endpoint, arguments)
            return {
                "protocol": transport.protocol,
                "endpoint": endpoint,
                "result": result,
            }

        specs.append(
            ToolSpec(
                name=f"milky_{endpoint}",
                description=(
                    f"Milky 1.3 API：{api['description']}。"
                    f"固定调用 POST {api['path']}；只接受声明的参数。"
                ),
                input_schema=api["input_schema"],
                handler=invoke_milky_api,
                risk=risk,
                required_privilege=True,
                supported_protocols=frozenset({"milky"}),
                timeout_seconds=20.0,
                max_output_chars=8000,
            )
        )
    for api in LARK_API_CATALOG:
        risk = ToolRisk.READ_ONLY if api["method"] == "GET" else ToolRisk.MUTATING

        async def invoke_lark_api(
            context: ToolContext,
            arguments: Mapping[str, Any],
            *,
            api: Mapping[str, Any] = api,
        ) -> dict[str, Any]:
            _require_privilege(context, str(api["name"]))
            transport = PlatformTransport(context.actions)
            result = await transport._lark(api, arguments)
            return {
                "protocol": transport.protocol,
                "api": api["name"],
                "method": api["method"],
                "path": api["path"],
                "result": result,
            }

        specs.append(
            ToolSpec(
                name=str(api["name"]),
                description=(
                    f"Lark OAPI：{api['description']}"
                    f"固定调用 {api['method']} {api['path']}。"
                ),
                input_schema=api["input_schema"],
                handler=invoke_lark_api,
                risk=risk,
                required_privilege=True,
                supported_protocols=frozenset({"feishu", "lark"}),
                timeout_seconds=20.0,
                max_output_chars=8000,
            )
        )
    for api in ONEBOT_API_CATALOG:
        action = str(api["name"])
        risk = (
            ToolRisk.READ_ONLY
            if action.startswith("get_") or action.startswith("can_send_")
            else ToolRisk.MUTATING
        )

        async def invoke_onebot_api(
            context: ToolContext,
            arguments: Mapping[str, Any],
            *,
            action: str = action,
        ) -> dict[str, Any]:
            _require_privilege(context, f"onebot_{action}")
            _validate_onebot_special_arguments(action, arguments)
            transport = PlatformTransport(context.actions)
            result = await transport.call(action, arguments)
            return {
                "protocol": transport.protocol,
                "action": action,
                "result": result,
            }

        specs.append(
            ToolSpec(
                name=f"onebot_{action}",
                description=(
                    f"OneBot V11 API：{api['description']}"
                    "参数类型和可选值按公开 API 文档固定。"
                ),
                input_schema=api["input_schema"],
                handler=invoke_onebot_api,
                risk=risk,
                required_privilege=True,
                supported_protocols=frozenset({"onebot"}),
                timeout_seconds=35.0,
                max_output_chars=8000,
            )
        )
    for spec in specs:
        registry.register(spec)
    return tuple(specs)


def _validate_onebot_special_arguments(
    action: str,
    arguments: Mapping[str, Any],
) -> None:
    if action == "send_msg":
        message_type = arguments.get("message_type")
        user_id = arguments.get("user_id")
        group_id = arguments.get("group_id")
        if user_id is not None and group_id is not None:
            raise ToolExecutionError(
                "invalid_params",
                "send_msg 的 user_id 和 group_id 只能提供一个。",
            )
        if message_type == "private" and user_id is None:
            raise ToolExecutionError("invalid_params", "private 消息必须提供 user_id。")
        if message_type == "group" and group_id is None:
            raise ToolExecutionError("invalid_params", "group 消息必须提供 group_id。")
        if message_type is None and user_id is None and group_id is None:
            raise ToolExecutionError(
                "invalid_params",
                "send_msg 必须提供 user_id 或 group_id。",
            )
        return
    if action == "set_group_anonymous_ban":
        supplied = sum(
            arguments.get(name) is not None
            for name in ("anonymous", "anonymous_flag", "flag")
        )
        if supplied != 1:
            raise ToolExecutionError(
                "invalid_params",
                "anonymous、anonymous_flag、flag 三者必须且只能提供一个。",
            )
        return
    if action == "set_group_add_request":
        subtype = arguments.get("sub_type")
        alias = arguments.get("type")
        if subtype is None and alias is None:
            raise ToolExecutionError(
                "invalid_params",
                "set_group_add_request 必须提供 sub_type 或 type。",
            )
        if subtype is not None and alias is not None and subtype != alias:
            raise ToolExecutionError(
                "invalid_params",
                "sub_type 和 type 同时提供时必须一致。",
            )


__all__ = [
    "MILKY_API_CATALOG",
    "MILKY_API_ENDPOINTS",
    "MILKY_API_TOOL_NAMES",
    "LARK_API_CATALOG",
    "LARK_API_TOOL_NAMES",
    "ONEBOT_API_CATALOG",
    "ONEBOT_API_ENDPOINTS",
    "ONEBOT_API_TOOL_NAMES",
    "PLATFORM_API_TOOL_NAMES",
    "PLATFORM_COMMAND_TOOL_NAME",
    "SEND_MESSAGE_TOOL_NAME",
    "ParsedCommand",
    "PlatformTransport",
    "parse_platform_command",
    "platform_command_tool",
    "register_platform_api_tools",
    "send_message_tool",
]
