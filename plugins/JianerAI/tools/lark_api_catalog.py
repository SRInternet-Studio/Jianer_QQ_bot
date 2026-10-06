"""Static Lark IM OpenAPI operations exposed to the Agent."""

from __future__ import annotations

from typing import Any


def _string(description: str, **kwargs: Any) -> dict[str, Any]:
    return {"type": "string", "description": description, **kwargs}


def _integer(description: str, **kwargs: Any) -> dict[str, Any]:
    return {"type": "integer", "description": description, **kwargs}


def _boolean(description: str, **kwargs: Any) -> dict[str, Any]:
    return {"type": "boolean", "description": description, **kwargs}


def _api(
    name: str,
    method: str,
    path: str,
    description: str,
    properties: dict[str, Any],
    required: tuple[str, ...] = (),
    *,
    path_parameters: tuple[str, ...] = (),
    query_parameters: tuple[str, ...] = (),
    request_kind: str = "none",
) -> dict[str, Any]:
    return {
        "name": name,
        "method": method,
        "path": path,
        "description": description,
        "path_parameters": path_parameters,
        "query_parameters": query_parameters,
        "request_kind": request_kind,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
    }


LARK_API_CATALOG = (
    _api(
        "lark_send_message",
        "POST",
        "/open-apis/im/v1/messages",
        "向飞书用户或群聊发送文本消息。",
        {
            "receive_id_type": _string(
                "接收对象 ID 类型。",
                enum=["open_id", "user_id", "union_id", "email", "chat_id"],
            ),
            "receive_id": _string("接收对象 ID。"),
            "text": _string("文本消息内容。", minLength=1),
            "uuid": _string("可选的幂等请求 ID。"),
        },
        ("receive_id_type", "receive_id", "text"),
        query_parameters=("receive_id_type",),
        request_kind="send_text",
    ),
    _api(
        "lark_reply_message",
        "POST",
        "/open-apis/im/v1/messages/{message_id}/reply",
        "回复一条飞书消息。",
        {
            "message_id": _string("要回复的平台消息 ID。"),
            "text": _string("回复文本内容。", minLength=1),
            "reply_in_thread": _boolean("是否作为话题回复。"),
            "uuid": _string("可选的幂等请求 ID。"),
        },
        ("message_id", "text"),
        path_parameters=("message_id",),
        request_kind="reply_text",
    ),
    _api(
        "lark_get_message",
        "GET",
        "/open-apis/im/v1/messages/{message_id}",
        "读取一条飞书消息。",
        {
            "message_id": _string("要读取的平台消息 ID。"),
            "user_id_type": _string("用户 ID 类型。"),
            "card_msg_content_type": _string("卡片消息内容格式。"),
            "with_sender_name": _boolean("是否包含发送者名称。"),
        },
        ("message_id",),
        path_parameters=("message_id",),
        query_parameters=("user_id_type", "card_msg_content_type", "with_sender_name"),
    ),
    _api(
        "lark_list_messages",
        "GET",
        "/open-apis/im/v1/messages",
        "读取指定飞书群聊中的消息列表。",
        {
            "container_id": _string("飞书群聊 chat_id。"),
            "start_time": _string("起始 Unix 时间戳（秒）。"),
            "end_time": _string("结束 Unix 时间戳（秒）。"),
            "sort_type": _string("消息排序方式。"),
            "page_size": _integer("每页消息数。", minimum=1, maximum=100),
            "page_token": _string("分页令牌。"),
            "only_thread_root_messages": _boolean("是否只返回话题根消息。"),
            "with_sender_name": _boolean("是否包含发送者名称。"),
            "card_msg_content_type": _string("卡片消息内容格式。"),
        },
        ("container_id",),
        query_parameters=(
            "container_id",
            "start_time",
            "end_time",
            "sort_type",
            "page_size",
            "page_token",
            "only_thread_root_messages",
            "with_sender_name",
            "card_msg_content_type",
        ),
    ),
    _api(
        "lark_delete_message",
        "DELETE",
        "/open-apis/im/v1/messages/{message_id}",
        "撤回一条飞书消息。",
        {"message_id": _string("要撤回的平台消息 ID。")},
        ("message_id",),
        path_parameters=("message_id",),
    ),
    _api(
        "lark_get_read_users",
        "GET",
        "/open-apis/im/v1/messages/{message_id}/read_users",
        "查询已读过指定飞书消息的成员。",
        {
            "message_id": _string("平台消息 ID。"),
            "user_id_type": _string("用户 ID 类型。"),
            "page_size": _integer("每页成员数。", minimum=1, maximum=100),
            "page_token": _string("分页令牌。"),
        },
        ("message_id",),
        path_parameters=("message_id",),
        query_parameters=("user_id_type", "page_size", "page_token"),
    ),
    _api(
        "lark_add_reaction",
        "POST",
        "/open-apis/im/v1/messages/{message_id}/reactions",
        "给一条飞书消息添加表情回应。",
        {
            "message_id": _string("平台消息 ID。"),
            "emoji_type": _string("飞书表情类型。"),
        },
        ("message_id", "emoji_type"),
        path_parameters=("message_id",),
        request_kind="add_reaction",
    ),
    _api(
        "lark_list_reactions",
        "GET",
        "/open-apis/im/v1/messages/{message_id}/reactions",
        "查询一条飞书消息的表情回应。",
        {
            "message_id": _string("平台消息 ID。"),
            "reaction_type": _string("筛选的回应类型。"),
            "page_token": _string("分页令牌。"),
            "page_size": _integer("每页回应数。", minimum=1, maximum=100),
            "user_id_type": _string("用户 ID 类型。"),
        },
        ("message_id",),
        path_parameters=("message_id",),
        query_parameters=("reaction_type", "page_token", "page_size", "user_id_type"),
    ),
    _api(
        "lark_delete_reaction",
        "DELETE",
        "/open-apis/im/v1/messages/{message_id}/reactions/{reaction_id}",
        "删除一条飞书消息上的表情回应。",
        {
            "message_id": _string("平台消息 ID。"),
            "reaction_id": _string("回应 ID。"),
        },
        ("message_id", "reaction_id"),
        path_parameters=("message_id", "reaction_id"),
    ),
)

LARK_API_TOOL_NAMES = frozenset(str(api["name"]) for api in LARK_API_CATALOG)

__all__ = ["LARK_API_CATALOG", "LARK_API_TOOL_NAMES"]
