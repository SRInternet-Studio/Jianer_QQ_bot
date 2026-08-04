"""Shared command utilities replacing NoneBot dependency injection."""

from dataclasses import dataclass
from typing import Any

from jianer import segments

from .. import adapter
from ..core.clients.exceptions import UserNotBindError
from ..core.database.qq import User, get_user, update_user
from ..core.merge.models import ServiceName
from ..runtime import runtime


AUTHORIZE_ERROR = (
    "您尚未授权 BOT 访问您的落雪查分器数据，请先使用「lxbind」指令进行绑定。"
)
@dataclass(frozen=True)
class UserResolution:
    user: User | None
    error: str | None = None


def mentioned_query_user_id(event: Any) -> int | None:
    """Return the last non-bot QQ mention while retaining the original message."""

    self_id = str(getattr(event, "self_id", ""))
    mentioned = None
    for item in getattr(event, "message", ()) or ():
        if not isinstance(item, segments.At):
            continue
        value = str(item.qq)
        if value.lower() == "all" or value == self_id:
            continue
        try:
            mentioned = int(value)
        except (TypeError, ValueError):
            continue
    return mentioned


_mentioned_query_user_id = mentioned_query_user_id


def parse_qq_id(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text or len(text) < 5 or len(text) > 12 or not text.isascii():
        return None
    if not text.isdigit() or text.startswith("0"):
        return None
    return int(text)


def _public_divingfish_user(user: User) -> User:
    """Create a non-persistent query view that cannot expose LXNS credentials."""

    copier = getattr(user, "model_copy", None)
    if not callable(copier):
        return user
    return copier(
        update={
            "friend_code": None,
            "access_token": None,
            "refresh_token": None,
            "service": ServiceName.DIVINGFISH,
        }
    )


async def public_divingfish_user(qqid: int) -> User:
    """Load a public-only Waterfish view without creating a database binding."""

    try:
        stored = await get_user(int(qqid))
    except UserNotBindError:
        stored = User(qqid=int(qqid))
    return _public_divingfish_user(stored)


async def resolve_user(
    event: Any,
    actions: Any,
    *,
    auto_create: bool = True,
    check_auth: bool = False,
    silent_auth_error: bool = False,
    allow_mention: bool = True,
) -> UserResolution:
    mentioned = mentioned_query_user_id(event) if allow_mention else None
    user_id = mentioned or getattr(event, "user_id", None)
    try:
        numeric_id = int(user_id)
    except (TypeError, ValueError):
        return UserResolution(None, "无法识别 QQ 用户 ID。")

    try:
        user = await get_user(numeric_id)
    except UserNotBindError:
        user = await update_user(numeric_id) if auto_create else None

    sender_id = str(getattr(event, "user_id", ""))
    if (
        mentioned is not None
        and str(mentioned) != sender_id
        and user is not None
    ):
        user = _public_divingfish_user(user)

    if (
        check_auth
        and user is not None
        and getattr(user, "service", None) == ServiceName.LXNS
        and user.access_token is None
        and user.refresh_token is None
    ):
        if silent_auth_error:
            return UserResolution(None)
        return UserResolution(None, AUTHORIZE_ERROR)
    return UserResolution(user)


async def require_user(
    event: Any,
    actions: Any,
    *,
    check_auth: bool = False,
    silent_auth_error: bool = False,
    allow_mention: bool = True,
) -> User | None:
    resolution = await resolve_user(
        event,
        actions,
        check_auth=check_auth,
        silent_auth_error=silent_auth_error,
        allow_mention=allow_mention,
    )
    if resolution.error:
        await adapter.send(event, actions, resolution.error)
    return resolution.user


async def require_data(event: Any, actions: Any) -> bool:
    return await runtime.ensure_data(event, actions)


async def require_resources(event: Any, actions: Any) -> bool:
    return await runtime.ensure_resources(event, actions)


def command_argument(event: Any, command: str, parsed: str = "") -> str:
    raw = str(getattr(event, "msg_str", "") or "").strip()
    if raw[: len(command)].lower() == command.lower():
        value = raw[len(command) :].strip()
    else:
        value = str(parsed or "").strip()
    return value


def is_private(event: Any) -> bool:
    return getattr(event, "group_id", None) is None


def is_group(event: Any) -> bool:
    return getattr(event, "group_id", None) is not None


async def reject_unless_superuser(event: Any, actions: Any) -> bool:
    if adapter.is_superuser(event):
        return False
    await adapter.send(event, actions, "权限不足，仅 BOT 超级用户可执行此命令。")
    return True


async def reject_unless_group_admin(event: Any, actions: Any) -> bool:
    if await adapter.is_group_admin(event, actions):
        return False
    await adapter.send(event, actions, "权限不足，仅群主、群管理员或 BOT 超级用户可执行。")
    return True
