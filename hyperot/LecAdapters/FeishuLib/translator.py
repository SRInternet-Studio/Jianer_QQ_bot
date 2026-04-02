import json
import re
import time


def _json_loads(content):
    if isinstance(content, dict):
        return content
    if not content:
        return {}
    try:
        return json.loads(content)
    except Exception:
        return {}


def _append_text(target: list, text: str) -> None:
    if text:
        target.append({"type": "text", "data": {"text": text}})


def _parse_text_segments(text: str) -> list[dict]:
    if not isinstance(text, str):
        return [{"type": "text", "data": {"text": str(text or "")}}]
    result = []
    cursor = 0
    for match in re.finditer(r'<at\s+user_id="([^"]+)"[^>]*>(.*?)</at>', text):
        start, end = match.span()
        _append_text(result, text[cursor:start])
        user_id = match.group(1)
        display_name = match.group(2) or ""
        if user_id == "all":
            _append_text(result, f"@{display_name or '所有人'}")
        else:
            result.append({"type": "at", "data": {"qq": str(user_id)}})
        cursor = end
    _append_text(result, text[cursor:])
    if not result:
        _append_text(result, text)
    return result


def feishu_message_to_segments(message: dict, event: dict | None = None) -> list[dict]:
    event = event or {}
    message_type = str(message.get("message_type") or "").lower()
    content = _json_loads(message.get("content"))

    if message_type == "text":
        return _parse_text_segments(content.get("text", ""))

    if message_type == "image":
        image_key = content.get("image_key") or message.get("image_key") or ""
        return [{"type": "image", "data": {"file": str(image_key), "summary": "[Image]"}}]

    if message_type == "file":
        file_key = content.get("file_key") or message.get("file_key") or ""
        file_name = content.get("file_name") or message.get("file_name") or file_key or "文件"
        return [{"type": "text", "data": {"text": f"[文件] {file_name}"}}]

    if message_type == "audio":
        return [{"type": "text", "data": {"text": "[语音]"}}]

    if message_type == "media":
        return [{"type": "text", "data": {"text": "[媒体]"}}]

    if message_type == "video":
        return [{"type": "text", "data": {"text": "[视频]"}}]

    if message_type == "interactive":
        return [{"type": "text", "data": {"text": "[卡片消息]"}}]

    if message_type == "post":
        title = content.get("title") or ""
        return [{"type": "text", "data": {"text": title or "[富文本消息]"}}]

    text = content.get("text")
    if text:
        return _parse_text_segments(text)
    return [{"type": "text", "data": {"text": str(message.get("content") or "[未知消息]")}}]


def build_message_event(payload: dict, bot_identity: str) -> dict | None:
    header = payload.get("header", {})
    event = payload.get("event", {})
    message = event.get("message", {})
    sender = event.get("sender", {})
    sender_id = sender.get("sender_id", {})
    open_id = (
        sender_id.get("open_id")
        or sender_id.get("user_id")
        or sender_id.get("union_id")
        or sender.get("open_id")
        or ""
    )
    chat_type = str(message.get("chat_type") or "p2p").lower()
    is_group = chat_type != "p2p"
    message_segments = feishu_message_to_segments(message, event)
    parent_id = (
        event.get("parent_id")
        or event.get("root_id")
        or message.get("parent_id")
        or message.get("root_id")
    )
    if parent_id:
        message_segments.insert(0, {"type": "reply", "data": {"id": str(parent_id)}})

    sender_name = (
        sender.get("name")
        or sender.get("nickname")
        or sender.get("sender_type")
        or open_id
        or "未知用户"
    )
    sender_data = {
        "user_id": str(open_id),
        "nickname": sender_name,
        "sex": "unknown",
        "age": 0,
    }
    if is_group:
        sender_data.update({
            "card": sender_name,
            "area": "",
            "level": "",
            "role": "member",
            "title": "",
        })

    return {
        "post_type": "message",
        "message_type": "group" if is_group else "private",
        "sub_type": "normal",
        "time": int(int(header.get("create_time") or time.time() * 1000) / 1000),
        "self_id": str(bot_identity),
        "user_id": str(open_id),
        "group_id": str(message.get("chat_id")) if is_group else None,
        "message_id": str(message.get("message_id") or ""),
        "message": message_segments,
        "sender": sender_data,
    }


def build_menu_event(payload: dict, bot_identity: str) -> dict | None:
    header = payload.get("header", {})
    event = payload.get("event", {})
    operator = event.get("operator", {})
    operator_id = operator.get("operator_id", {})
    open_id = operator_id.get("open_id") or operator_id.get("user_id") or operator.get("open_id") or ""
    event_key = event.get("event_key") or event.get("key") or event.get("menu_key") or "菜单"
    return {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "menu",
        "time": int(int(header.get("create_time") or time.time() * 1000) / 1000),
        "self_id": str(bot_identity),
        "user_id": str(open_id),
        "group_id": None,
        "message_id": str(header.get("event_id") or f"menu_{time.time_ns()}"),
        "message": [{"type": "text", "data": {"text": str(event_key)}}],
        "sender": {
            "user_id": str(open_id),
            "nickname": operator.get("name") or str(open_id),
            "sex": "unknown",
            "age": 0,
        },
    }


def build_hyper_event(payload: dict, bot_identity: str) -> dict | None:
    header = payload.get("header", {})
    event_type = (
        header.get("event_type")
        or payload.get("event_type")
        or payload.get("type")
        or ""
    )
    if event_type == "im.message.receive_v1":
        return build_message_event(payload, bot_identity)
    if event_type == "application.bot.menu_v6":
        return build_menu_event(payload, bot_identity)
    return None


def stringify_feishu_message(message_data: dict | str | None) -> str:
    content = _json_loads(message_data.get("content")) if isinstance(message_data, dict) else _json_loads(message_data)
    if isinstance(content, dict):
        if content.get("text"):
            return content.get("text")
        if content.get("title"):
            return content.get("title")
        if content.get("file_name"):
            return f"[文件] {content.get('file_name')}"
        if content.get("image_key"):
            return "[图片]"
    if isinstance(message_data, dict):
        return str(message_data.get("content") or "")
    return str(message_data or "")
