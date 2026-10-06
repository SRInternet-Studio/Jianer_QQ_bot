"""Static model-callable OneBot V11 public API definitions.

The catalog follows https://11.onebot.dev/api/public.html. Hidden APIs and
implementation-specific actions are intentionally not part of this surface.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def _field(
    type_name: str,
    description: str,
    *,
    default: Any = ...,
    enum: tuple[Any, ...] = (),
    minimum: int | None = None,
    maximum: int | None = None,
    properties: dict[str, Any] | None = None,
    additional_properties: bool | None = None,
    items: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output: dict[str, Any] = {"type": type_name, "description": description}
    if default is not ...:
        output["default"] = default
    if enum:
        output["enum"] = list(enum)
    if minimum is not None:
        output["minimum"] = minimum
    if maximum is not None:
        output["maximum"] = maximum
    if properties is not None:
        output["properties"] = deepcopy(properties)
    if additional_properties is not None:
        output["additionalProperties"] = additional_properties
    if items is not None:
        output["items"] = deepcopy(items)
    return output


def _message() -> dict[str, Any]:
    segment_properties = {
        "type": {
            "type": "string",
            "enum": [
                "text",
                "face",
                "image",
                "record",
                "video",
                "at",
                "rps",
                "dice",
                "shake",
                "poke",
                "anonymous",
                "share",
                "contact",
                "location",
                "music",
                "reply",
                "node",
                "xml",
                "json",
            ],
        },
        "data": {"type": "object", "additionalProperties": True},
    }
    nested_message = {
        "anyOf": [
            {"type": "string"},
            {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": deepcopy(segment_properties),
                    "required": ["type", "data"],
                    "additionalProperties": False,
                },
            },
        ]
    }
    segments = [
        _segment("text", {"text": _string("文本内容。")}, ("text",)),
        _segment("face", {"id": _string("QQ 表情 ID。")}, ("id",)),
        _segment(
            "image",
            {
                "file": _string("图片文件名或文件 URI。"),
                "type": _string("图片类型。", enum=("flash",)),
                "url": _string("图片 URL。"),
                "cache": _integer("是否使用缓存（0 否，1 是）。", enum=(0, 1), default=1),
                "proxy": _integer("是否使用代理（0 否，1 是）。", enum=(0, 1), default=1),
                "timeout": _field("number", "下载超时秒数。", minimum=0),
            },
            ("file",),
        ),
        _segment(
            "record",
            {
                "file": _string("语音文件名或文件 URI。"),
                "magic": _integer("是否变声（0 否，1 是）。", enum=(0, 1), default=0),
                "url": _string("语音 URL。"),
                "cache": _integer("是否使用缓存（0 否，1 是）。", enum=(0, 1), default=1),
                "proxy": _integer("是否使用代理（0 否，1 是）。", enum=(0, 1), default=1),
                "timeout": _field("number", "下载超时秒数。", minimum=0),
            },
            ("file",),
        ),
        _segment(
            "video",
            {
                "file": _string("视频文件名或文件 URI。"),
                "url": _string("视频 URL。"),
                "cache": _integer("是否使用缓存（0 否，1 是）。", enum=(0, 1), default=1),
                "proxy": _integer("是否使用代理（0 否，1 是）。", enum=(0, 1), default=1),
                "timeout": _field("number", "下载超时秒数。", minimum=0),
            },
            ("file",),
        ),
        _segment(
            "at",
            {"qq": {"anyOf": [_integer("被 At 的 QQ 号。", minimum=1), _string("使用 all At 全体成员。", enum=("all",))]}},
            ("qq",),
        ),
        _segment("rps", {}, ()),
        _segment("dice", {}, ()),
        _segment("shake", {}, ()),
        _segment(
            "poke",
            {
                "type": _string("戳一戳类型。"),
                "id": _string("戳一戳 ID。"),
                "name": _string("戳一戳名称。"),
            },
            ("type", "id"),
        ),
        _segment(
            "anonymous",
            {"ignore": _integer("无法匿名时是否继续发送（0 否，1 是）。", enum=(0, 1), default=0)},
            (),
        ),
        _segment(
            "share",
            {
                "url": _string("分享链接。"),
                "title": _string("分享标题。"),
                "content": _string("分享描述。"),
                "image": _string("预览图片 URL。"),
            },
            ("url", "title"),
        ),
        _segment(
            "contact",
            {
                "type": _string("联系人类型。", enum=("qq", "group")),
                "id": _integer("推荐的 QQ 号或群号。", minimum=1),
            },
            ("type", "id"),
        ),
        _segment(
            "location",
            {
                "lat": _field("number", "纬度。"),
                "lon": _field("number", "经度。"),
                "title": _string("位置标题。"),
                "content": _string("位置说明。"),
            },
            ("lat", "lon"),
        ),
        _segment(
            "music",
            {
                "type": _string("音乐平台。", enum=("qq", "163", "xm")),
                "id": _string("歌曲 ID。"),
            },
            ("type", "id"),
        ),
        _segment(
            "music",
            {
                "type": _string("自定义音乐类型。", enum=("custom",)),
                "url": _string("点击后跳转的 URL。"),
                "audio": _string("音乐 URL。"),
                "title": _string("音乐标题。"),
                "content": _string("音乐描述。"),
                "image": _string("预览图片 URL。"),
            },
            ("type", "url", "audio", "title"),
        ),
        _segment("reply", {"id": _integer("被回复的消息 ID。")}, ("id",)),
        _segment("node", {"id": _string("已有合并转发节点 ID。")}, ("id",)),
        _segment(
            "node",
            {
                "user_id": _integer("发送者 QQ 号。", minimum=1),
                "nickname": _string("发送者昵称。"),
                "content": nested_message,
            },
            ("user_id", "nickname", "content"),
        ),
        _segment("xml", {"data": _string("XML 内容。")}, ("data",)),
        _segment("json", {"data": _string("JSON 内容。")}, ("data",)),
    ]
    return {
        "anyOf": [
            {"type": "string"},
            {
                "type": "array",
                "items": {"anyOf": segments},
            },
        ]
    }


def _segment(
    segment_type: str,
    data_properties: dict[str, Any],
    data_required: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": [segment_type]},
            "data": {
                "type": "object",
                "properties": deepcopy(data_properties),
                "required": list(data_required),
                "additionalProperties": False,
            },
        },
        "required": ["type", "data"],
        "additionalProperties": False,
    }


def _api(
    name: str,
    description: str,
    properties: dict[str, Any] | None = None,
    required: tuple[str, ...] = (),
    *,
    one_of_required: tuple[str, ...] = (),
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": deepcopy(properties or {}),
        "required": list(required),
        "additionalProperties": False,
    }
    if one_of_required:
        schema["anyOf"] = [
            {
                "type": "object",
                "properties": deepcopy(properties or {}),
                "required": [name],
            }
            for name in one_of_required
        ]
    return {"name": name, "description": description, "input_schema": schema}


_integer = lambda description, **kwargs: _field(  # noqa: E731
    "integer", description, **kwargs
)
_string = lambda description, **kwargs: _field(  # noqa: E731
    "string", description, **kwargs
)
_boolean = lambda description, **kwargs: _field(  # noqa: E731
    "boolean", description, **kwargs
)

_anonymous = _field(
    "object",
    "群消息上报中的 anonymous 对象。",
    properties={
        "id": _string("匿名用户 ID。"),
        "name": _string("匿名用户名。"),
        "flag": _string("匿名用户 flag。"),
    },
    additional_properties=False,
)
_message_field = _message()
_request_flag = _string("从对应请求事件中取得的 flag。")
_group_id = lambda: _integer("群号。", minimum=1)  # noqa: E731
_user_id = lambda: _integer("QQ 用户号。", minimum=1)  # noqa: E731

ONEBOT_API_CATALOG = (
    _api(
        "send_private_msg",
        "发送私聊消息。",
        {
            "user_id": _user_id(),
            "message": _message_field,
            "auto_escape": _boolean("字符串消息是否作为纯文本发送。", default=False),
        },
        ("user_id", "message"),
    ),
    _api(
        "send_group_msg",
        "发送群消息。",
        {
            "group_id": _group_id(),
            "message": _message_field,
            "auto_escape": _boolean("字符串消息是否作为纯文本发送。", default=False),
        },
        ("group_id", "message"),
    ),
    _api(
        "send_msg",
        "按 message_type 或目标 ID 发送私聊/群消息。",
        {
            "message_type": _string("消息类型。", enum=("private", "group")),
            "user_id": _user_id(),
            "group_id": _group_id(),
            "message": _message_field,
            "auto_escape": _boolean("字符串消息是否作为纯文本发送。", default=False),
        },
        ("message",),
        one_of_required=("user_id", "group_id"),
    ),
    _api(
        "delete_msg",
        "撤回指定消息。",
        {"message_id": _integer("消息 ID。")},
        ("message_id",),
    ),
    _api(
        "get_msg",
        "获取指定消息。",
        {"message_id": _integer("消息 ID。")},
        ("message_id",),
    ),
    _api(
        "get_forward_msg",
        "获取合并转发消息内容。",
        {"id": _string("合并转发 ID。")},
        ("id",),
    ),
    _api(
        "send_like",
        "给好友发送赞。",
        {
            "user_id": _user_id(),
            "times": _integer("点赞次数，每日最多 10 次。", default=1, minimum=1, maximum=10),
        },
        ("user_id",),
    ),
    _api(
        "set_group_kick",
        "将成员移出群。",
        {
            "group_id": _group_id(),
            "user_id": _user_id(),
            "reject_add_request": _boolean("是否拒绝该成员再次加群。", default=False),
        },
        ("group_id", "user_id"),
    ),
    _api(
        "set_group_ban",
        "设置或取消群成员禁言。",
        {
            "group_id": _group_id(),
            "user_id": _user_id(),
            "duration": _integer("禁言秒数，0 表示取消禁言。", default=1800, minimum=0),
        },
        ("group_id", "user_id"),
    ),
    _api(
        "set_group_anonymous_ban",
        "禁言匿名群成员。anonymous、anonymous_flag、flag 三者必须且只能提供一个。",
        {
            "group_id": _group_id(),
            "anonymous": _anonymous,
            "anonymous_flag": _string("匿名用户 flag。"),
            "flag": _string("anonymous_flag 的兼容别名。"),
            "duration": _integer("禁言秒数。", default=1800, minimum=0),
        },
        ("group_id",),
        one_of_required=("anonymous", "anonymous_flag", "flag"),
    ),
    _api(
        "set_group_whole_ban",
        "设置或取消群全员禁言。",
        {"group_id": _group_id(), "enable": _boolean("是否启用全员禁言。", default=True)},
        ("group_id",),
    ),
    _api(
        "set_group_admin",
        "设置或取消群管理员。",
        {
            "group_id": _group_id(),
            "user_id": _user_id(),
            "enable": _boolean("true 设置管理员，false 取消。", default=True),
        },
        ("group_id", "user_id"),
    ),
    _api(
        "set_group_anonymous",
        "开启或关闭群匿名聊天。",
        {"group_id": _group_id(), "enable": _boolean("是否允许匿名聊天。", default=True)},
        ("group_id",),
    ),
    _api(
        "set_group_card",
        "设置或清空群名片。",
        {
            "group_id": _group_id(),
            "user_id": _user_id(),
            "card": _string("群名片；空字符串表示清空。", default=""),
        },
        ("group_id", "user_id"),
    ),
    _api(
        "set_group_name",
        "设置群名称。",
        {"group_id": _group_id(), "group_name": _string("新群名称。")},
        ("group_id", "group_name"),
    ),
    _api(
        "set_group_leave",
        "退出群聊；机器人是群主时可通过 is_dismiss 解散群。",
        {"group_id": _group_id(), "is_dismiss": _boolean("是否解散群。", default=False)},
        ("group_id",),
    ),
    _api(
        "set_group_special_title",
        "设置或清空群成员专属头衔。",
        {
            "group_id": _group_id(),
            "user_id": _user_id(),
            "special_title": _string("头衔；空字符串表示清空。", default=""),
            "duration": _integer("有效秒数，-1 表示永久。", default=-1, minimum=-1),
        },
        ("group_id", "user_id"),
    ),
    _api(
        "set_friend_add_request",
        "处理好友添加请求。",
        {
            "flag": _request_flag,
            "approve": _boolean("是否同意请求。", default=True),
            "remark": _string("同意后设置的好友备注。", default=""),
        },
        ("flag",),
    ),
    _api(
        "set_group_add_request",
        "处理加群请求或邀请。",
        {
            "flag": _request_flag,
            "sub_type": _string("请求类型。", enum=("add", "invite")),
            "type": _string("sub_type 的兼容别名。", enum=("add", "invite")),
            "approve": _boolean("是否同意请求/邀请。", default=True),
            "reason": _string("拒绝理由。", default=""),
        },
        ("flag",),
        one_of_required=("sub_type", "type"),
    ),
    _api("get_login_info", "获取登录号信息。"),
    _api(
        "get_stranger_info",
        "获取陌生人信息。",
        {"user_id": _user_id(), "no_cache": _boolean("是否绕过缓存。", default=False)},
        ("user_id",),
    ),
    _api("get_friend_list", "获取好友列表。"),
    _api(
        "get_group_info",
        "获取群信息。",
        {"group_id": _group_id(), "no_cache": _boolean("是否绕过缓存。", default=False)},
        ("group_id",),
    ),
    _api("get_group_list", "获取群列表。"),
    _api(
        "get_group_member_info",
        "获取群成员信息。",
        {
            "group_id": _group_id(),
            "user_id": _user_id(),
            "no_cache": _boolean("是否绕过缓存。", default=False),
        },
        ("group_id", "user_id"),
    ),
    _api(
        "get_group_member_list",
        "获取群成员列表。",
        {"group_id": _group_id()},
        ("group_id",),
    ),
    _api(
        "get_group_honor_info",
        "获取指定类型的群荣誉信息。",
        {
            "group_id": _group_id(),
            "type": _string(
                "群荣誉类型。",
                enum=("talkative", "performer", "legend", "strong_newbie", "emotion", "all"),
            ),
        },
        ("group_id", "type"),
    ),
    _api(
        "get_cookies",
        "获取指定域名的 Cookies。",
        {"domain": _string("目标域名。", default="")},
    ),
    _api("get_csrf_token", "获取 CSRF Token。"),
    _api(
        "get_credentials",
        "获取 QQ 相关接口凭证。",
        {"domain": _string("目标域名。", default="")},
    ),
    _api(
        "get_record",
        "获取并转换语音文件。",
        {
            "file": _string("消息中的语音文件名。"),
            "out_format": _string(
                "输出音频格式。",
                enum=("mp3", "amr", "wma", "m4a", "spx", "ogg", "wav", "flac"),
            ),
        },
        ("file", "out_format"),
    ),
    _api(
        "get_image",
        "获取图片文件。",
        {"file": _string("消息中的图片文件名。")},
        ("file",),
    ),
    _api("can_send_image", "检查当前账号是否可以发送图片。"),
    _api("can_send_record", "检查当前账号是否可以发送语音。"),
    _api("get_status", "获取 OneBot 运行状态。"),
    _api("get_version_info", "获取 OneBot 实现版本信息。"),
    _api(
        "set_restart",
        "重启 OneBot 实现。",
        {"delay": _integer("延迟毫秒数。", default=0, minimum=0)},
    ),
    _api("clean_cache", "清理 OneBot 实现缓存。"),
)

ONEBOT_API_ENDPOINTS = frozenset(str(api["name"]) for api in ONEBOT_API_CATALOG)
ONEBOT_API_TOOL_NAMES = frozenset(f"onebot_{name}" for name in ONEBOT_API_ENDPOINTS)

__all__ = ["ONEBOT_API_CATALOG", "ONEBOT_API_ENDPOINTS", "ONEBOT_API_TOOL_NAMES"]
