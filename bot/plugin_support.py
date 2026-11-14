"""Shared UniMessage and protocol helpers for native JianerCore plugins."""

from __future__ import annotations

from typing import Any

from jianer import common as Manager
from jianer.adapters.contracts import Capability
from jianer.plugins.builtin.alconna import Target, UniMessage

from Tools.tools import get_user_nickname

from . import plugin_state
from . import utils

QQ_PROTOCOLS = frozenset({"onebot", "milky"})
UNSUPPORTED_GROUP_ADMIN = "该功能当前协议不支持群管理操作。"
UNSUPPORTED_GROUP_LIST = "该功能当前协议不支持获取群列表。"


def protocol_name(*sources: Any) -> str:
    for source in sources:
        value = getattr(source, "protocol", None)
        if value:
            return str(value).casefold()
    config = plugin_state.get_config()
    return str(getattr(config, "protocol", "")).casefold()


def is_qq_protocol(*sources: Any) -> bool:
    return protocol_name(*sources) in QQ_PROTOCOLS


def is_feishu_protocol(*sources: Any) -> bool:
    return protocol_name(*sources) == "feishu"


def supports_group_admin(actions: Any) -> bool:
    return is_qq_protocol(actions)


def supports_group_list(actions: Any) -> bool:
    return is_qq_protocol(actions)


def supports_native_forward(actions: Any) -> bool:
    capabilities = getattr(actions, "capabilities", ()) or ()
    if capabilities:
        return Capability.NATIVE_GROUP_FORWARD in capabilities
    return is_qq_protocol(actions)


def target_for(event: Any) -> Target:
    return Target.from_event(event)


def runtime() -> dict[str, Any]:
    return plugin_state.get_runtime()


def root_user() -> str | None:
    users = runtime().get("root_users") or ()
    for user in users:
        value = str(user).strip()
        if value:
            return value
    return None


def admin_ids() -> set[str]:
    values = runtime().get("admins") or ()
    return {str(item) for item in values}


def super_ids() -> set[str]:
    values = runtime().get("supers") or ()
    return {str(item) for item in values}


def is_admin(event: Any) -> bool:
    return str(getattr(event, "user_id", "")) in admin_ids()


def is_super(event: Any) -> bool:
    return str(getattr(event, "user_id", "")) in super_ids()


def has_emoji(text: str) -> bool:
    return utils.has_emoji(text)


async def send_message(
    event: Any,
    actions: Any,
    payload: Any,
    *,
    reply: bool = False,
    mention: str | int | None = None,
) -> Any:
    message = UniMessage()
    message_id = getattr(event, "message_id", None)
    if reply and message_id is not None:
        message += UniMessage.reply(str(message_id))
    if mention is not None:
        message += UniMessage.at(str(mention))
    message.append(payload)
    return await message.send(event=event, actions=actions)


async def send_text(
    event: Any,
    actions: Any,
    text: str,
    *,
    reply: bool = False,
) -> Any:
    return await send_message(
        event,
        actions,
        UniMessage.text(str(text)),
        reply=reply,
    )


async def send_root_message(payload: Any, actions: Any = None) -> Any | None:
    root = root_user()
    if root is None:
        logger = plugin_state.get_logger()
        if logger is not None:
            logger.warning("ROOT_User is empty; skip root notification")
        return None
    return await UniMessage(payload).send(
        target=Target.private(root),
        actions=actions,
    )


async def send_root_text(text: str, actions: Any = None) -> Any | None:
    return await send_root_message(UniMessage.text(str(text)), actions=actions)


async def nickname(uid: Any, actions: Any, sender: Any = None) -> str:
    return await get_user_nickname(uid, Manager, actions, sender=sender)


async def group_list(actions: Any) -> list[dict[str, Any]]:
    if not supports_group_list(actions):
        return []
    try:
        echo = await actions.custom.get_group_list()
        result = Manager.Ret.fetch(echo)
        raw = getattr(getattr(result, "data", None), "raw", None)
        return list(raw or ())
    except Exception:
        return []


async def set_group_whole_ban(actions: Any, group_id: Any, enable: bool) -> bool:
    if not supports_group_admin(actions):
        return False
    await actions.custom.set_group_whole_ban(
        group_id=group_id,
        enable=bool(enable),
    )
    return True


async def leave_group(actions: Any, group_id: Any) -> bool:
    if not supports_group_admin(actions):
        return False
    await actions.custom.set_group_leave(
        group_id=group_id,
        is_dismiss=True,
    )
    return True


async def poke(actions: Any, event: Any) -> bool:
    if not supports_group_admin(actions):
        return False
    if getattr(event, "group_id", None) is not None:
        await actions.custom.group_poke(
            group_id=event.group_id,
            user_id=event.user_id,
        )
    else:
        await actions.custom.friend_poke(user_id=event.user_id)
    return True


def config_others() -> dict[str, Any]:
    config = plugin_state.get_config()
    return dict(getattr(config, "others", {}) or {})


def help_mode_default() -> str:
    return str(config_others().get("help_mode_default", "图片"))


def protocol_object(*sources: Any) -> str:
    return protocol_name(*sources)


__all__ = [
    "QQ_PROTOCOLS",
    "UNSUPPORTED_GROUP_ADMIN",
    "UNSUPPORTED_GROUP_LIST",
    "admin_ids",
    "config_others",
    "group_list",
    "has_emoji",
    "help_mode_default",
    "is_admin",
    "is_feishu_protocol",
    "is_qq_protocol",
    "is_super",
    "leave_group",
    "nickname",
    "poke",
    "protocol_name",
    "root_user",
    "runtime",
    "send_message",
    "send_root_message",
    "send_root_text",
    "send_text",
    "set_group_whole_ban",
    "super_ids",
    "supports_group_admin",
    "supports_group_list",
    "supports_native_forward",
    "target_for",
]
