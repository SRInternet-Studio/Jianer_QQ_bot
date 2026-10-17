from __future__ import annotations

import asyncio
import datetime
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..host import Bot, Message, MessageSegment
from ..host import ActionFailed
from ..host import logger

from .runtime import ROLLPIG_TIMEZONE, is_group_rollpig_enabled, resolve_roast_charge_max
from .store import store
from .store.models import (
    ROAST_REFILL_MIN_DISTINCT_VOTERS,
    GroupRoastRefillCompleteResult,
    GroupRoastRefillRequest,
)
from .texts import (
    ROAST_REFILL_CREATED_TEXTS,
    ROAST_REFILL_EXISTING_TEXTS,
    ROAST_REFILL_REACTION_HINT_TEXTS,
    ROAST_REFILL_SUCCESS_TEXTS,
    ROAST_REFILL_UNSUPPORTED_TEXTS,
)


ROAST_REFILL_EMOJI_ID = "424"
ROAST_REFILL_EMOJI_TYPE = 1
ROAST_REFILL_FETCH_PAGE_SIZE = 100
ROAST_REFILL_FETCH_MAX_PAGES = 20
ROAST_REFILL_IMAGE_PATH = Path(__file__).parent / "resource" / "refill.jpg"


@dataclass(frozen=True)
class RefillEligibility:
    """保存本轮日活、在群有效用户及其中的群管理账号。"""

    active_user_ids: frozenset[str]
    eligible_user_ids: frozenset[str]
    manager_user_ids: frozenset[str]


class RoastRefillReactionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        message_missing: bool = False,
        capability_unsupported: bool = False,
    ):
        super().__init__(message)
        self.message_missing = message_missing
        self.capability_unsupported = capability_unsupported


def _reaction_error_info(error: object) -> dict[str, object] | None:
    """只提取 OneBot ActionFailed 或 API 响应中的结构化错误字段。"""

    if isinstance(error, ActionFailed):
        return dict(error.info)
    if isinstance(error, dict):
        return dict(error)
    return None


def _reaction_error_flags(error: object) -> tuple[bool, bool]:
    """仅凭结构化 OneBot 信号判断终态；未知异常必须保持投票可重试。"""

    info = _reaction_error_info(error)
    if info is None:
        return False, False
    lowered = " ".join(
        str(info.get(key) or "")
        for key in ("message", "wording", "msg", "errMsg", "error")
    ).lower()
    message_missing = any(
        token in lowered
        for token in (
            "消息不存在",
            "msg not found",
            "message not found",
            "message does not exist",
        )
    )
    capability_unsupported = any(
        token in lowered
        for token in (
            "unsupported api",
            "api not found",
            "unknown api",
            "unknown action",
            "action not found",
            "api is not supported",
            "action is not supported",
            "api not implemented",
            "action not implemented",
            "接口不存在",
            "动作不存在",
            "不支持该接口",
            "不支持此接口",
            "未实现该接口",
        )
    )
    # 部分 OneBot 实现只返回“方法不存在”类 retcode 和动作名，没有稳定 wording。
    # 仅在两者同时出现时判为能力缺失，避免代理层的普通 404 被误杀为不支持。
    try:
        retcode = int(info.get("retcode"))
    except (TypeError, ValueError):
        retcode = 0
    if retcode in {-32601, 404, 1404} and "fetch_emoji_like" in lowered:
        capability_unsupported = True
    return message_missing, capability_unsupported


def extract_message_id(send_result: Any) -> str:
    """兼容 OneBot 直接整数和标准 `{message_id}` 返回。"""

    if isinstance(send_result, dict):
        value = send_result.get("message_id")
    else:
        value = send_result
    return str(value) if value not in {None, ""} else ""


async def add_refill_reaction(bot: Bot, message_id: str) -> bool:
    """给投票消息贴 QQ「续标识」；失败只影响引导，不直接判整场失败。"""

    try:
        await bot.call_api(
            "set_msg_emoji_like",
            message_id=message_id,
            emoji_id=ROAST_REFILL_EMOJI_ID,
            set=True,
        )
        return True
    except Exception as error:
        logger.warning(f"rollpig 烤箱续火自动添加续标识失败: message={message_id} error={error}")
        return False


def _reaction_payload(value: Any) -> dict:
    if not isinstance(value, dict):
        raise RoastRefillReactionError("fetch_emoji_like 返回格式无效")
    nested = value.get("data")
    return nested if isinstance(nested, dict) else value


def _is_truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes"}


async def fetch_refill_reactors(bot: Bot, message_id: str) -> set[str]:
    """分页读取续标识用户列表；QQ raw count 从不作为业务真值。"""

    user_ids: set[str] = set()
    cookie = ""
    seen_cookies: set[str] = set()
    for _ in range(ROAST_REFILL_FETCH_MAX_PAGES):
        try:
            response = await bot.call_api(
                "fetch_emoji_like",
                message_id=message_id,
                emojiId=ROAST_REFILL_EMOJI_ID,
                emojiType=ROAST_REFILL_EMOJI_TYPE,
                count=ROAST_REFILL_FETCH_PAGE_SIZE,
                cookie=cookie,
            )
        except Exception as error:
            error_message = str(error)
            missing, unsupported = _reaction_error_flags(error)
            raise RoastRefillReactionError(
                error_message,
                message_missing=missing,
                capability_unsupported=unsupported,
            ) from error

        payload = _reaction_payload(response)
        result_code = payload.get("result")
        if result_code not in {None, 0, "0"}:
            error_message = str(payload.get("errMsg") or payload.get("message") or result_code)
            missing, unsupported = _reaction_error_flags(payload)
            raise RoastRefillReactionError(
                error_message,
                message_missing=missing,
                capability_unsupported=unsupported,
            )
        likes = payload.get("emojiLikesList", [])
        if not isinstance(likes, list):
            raise RoastRefillReactionError("fetch_emoji_like 缺少 emojiLikesList")
        for item in likes:
            if not isinstance(item, dict):
                continue
            user_id = item.get("tinyId") or item.get("tiny_id") or item.get("user_id")
            if user_id:
                user_ids.add(str(user_id))

        if _is_truthy(payload.get("isLastPage")):
            break
        next_cookie = str(payload.get("cookie") or "")
        if not next_cookie or next_cookie == cookie or next_cookie in seen_cookies:
            break
        seen_cookies.add(next_cookie)
        cookie = next_cookie
    return user_ids


async def fetch_refill_group_members(bot: Bot, group_id: str) -> dict[str, str]:
    """读取当前群成员及角色，供退群过滤和管理员双票共同使用。"""

    try:
        response = await bot.call_api("get_group_member_list", group_id=int(group_id))
    except Exception as error:
        raise RoastRefillReactionError(f"群成员名单读取失败: {error}") from error

    if isinstance(response, dict) and isinstance(response.get("data"), list):
        members = response["data"]
    else:
        members = response
    if not isinstance(members, list):
        raise RoastRefillReactionError("get_group_member_list 返回格式无效")
    member_roles: dict[str, str] = {}
    for item in members:
        if not isinstance(item, dict):
            continue
        user_id = item.get("user_id") or item.get("userId") or item.get("uin")
        if user_id:
            member_roles[str(user_id)] = str(item.get("role") or "member").lower()
    return member_roles


# ================================ 跨日查找与成员资格 ================================ #

def _refill_lookup_dates(reference_ts: float | None = None) -> tuple[str, str]:
    """返回事件所处业务日及前一日；十分钟投票最多只可能跨过一个零点。"""

    try:
        reference = (
            datetime.datetime.now(ROLLPIG_TIMEZONE)
            if reference_ts is None
            else datetime.datetime.fromtimestamp(float(reference_ts), tz=ROLLPIG_TIMEZONE)
        )
    except (TypeError, ValueError, OSError, OverflowError):
        reference = datetime.datetime.now(ROLLPIG_TIMEZONE)
    current = reference.date()
    return current.isoformat(), (current - datetime.timedelta(days=1)).isoformat()


async def find_group_roast_refill(
    group_id: str,
    *,
    message_id: str = "",
    reference_ts: float | None = None,
) -> GroupRoastRefillRequest | None:
    """查找当前或跨零点的上一业务日投票，可按消息 ID 精确匹配。"""

    normalized_message_id = str(message_id or "")
    for date_str in _refill_lookup_dates(reference_ts):
        request = await store.get_group_roast_refill(str(group_id), date_str=date_str)
        if request is None:
            continue
        if not normalized_message_id or request.message_id == normalized_message_id:
            return request
    return None


async def get_refill_eligible_users(
    bot: Bot,
    group_id: str,
    date_str: str,
) -> RefillEligibility:
    """返回登记日活、仍在群内的真人账号及其中的当前群管理。"""

    group_member_roles, active_user_ids = await asyncio.gather(
        fetch_refill_group_members(bot, group_id),
        store.get_group_active_user_ids(group_id, date_str),
    )
    normalized_active = {str(user_id) for user_id in active_user_ids if user_id}
    eligible_user_ids = (normalized_active & set(group_member_roles)) - {str(bot.self_id)}
    manager_user_ids = {
        user_id
        for user_id in eligible_user_ids
        if group_member_roles.get(user_id) in {"owner", "admin"}
    }
    return RefillEligibility(
        active_user_ids=frozenset(normalized_active),
        eligible_user_ids=frozenset(eligible_user_ids),
        manager_user_ids=frozenset(manager_user_ids),
    )


# ================================ 文案与票数 ================================ #

def format_refill_created(
    request: GroupRoastRefillRequest,
    max_charges: int,
    *,
    manager_double_vote: bool = True,
) -> str:
    return random.choice(ROAST_REFILL_CREATED_TEXTS).format(
        initiator=request.initiator_name or request.initiator_id,
        active_count=request.active_count_snapshot,
        required_votes=request.required_votes,
        success_summary=(
            f"\n今日已续火 {request.success_count_before} 次。"
            if request.success_count_before > 0
            else ""
        ),
        max_charges=max_charges,
        vote_rule=(
            "管理及以上按 2 头计"
            if manager_double_vote
            else "每人按 1 头计"
        ),
    )


def build_refill_created_message(
    request: GroupRoastRefillRequest,
    max_charges: int,
    *,
    manager_double_vote: bool = True,
) -> Message:
    """按图片在前、申请文案在后的顺序构造消息；图片内嵌以兼容跨容器 OneBot。"""

    return MessageSegment.image(ROAST_REFILL_IMAGE_PATH.read_bytes()) + MessageSegment.text(
        format_refill_created(
            request,
            max_charges,
            manager_double_vote=manager_double_vote,
        )
    )


def _remaining_minutes(request: GroupRoastRefillRequest) -> int:
    try:
        expires_at = datetime.datetime.fromisoformat(request.expires_at)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
        remaining = max(0.0, (expires_at - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
    except ValueError:
        return 0
    return max(1, int((remaining + 59) // 60)) if remaining > 0 else 0


def _refill_requirement_hint(request: GroupRoastRefillRequest, supporters: int, votes: int) -> str:
    """同时描述剩余加权票数和两人联署约束，避免双票进度产生歧义。"""

    missing_votes = max(0, request.required_votes - votes)
    missing_supporters = max(0, ROAST_REFILL_MIN_DISTINCT_VOTERS - supporters)
    if missing_votes and missing_supporters:
        return f"，还差 {missing_votes} 头，且至少再来 {missing_supporters} 人"
    if missing_votes:
        return f"，还差 {missing_votes} 头"
    if missing_supporters:
        return f"，至少再来 {missing_supporters} 人"
    return ""


def format_existing_refill(
    request: GroupRoastRefillRequest,
    supporters: int,
    votes: int,
) -> str:
    if votes >= request.required_votes and supporters >= ROAST_REFILL_MIN_DISTINCT_VOTERS:
        return "续火支持已经达标，正在结算。"
    return random.choice(ROAST_REFILL_EXISTING_TEXTS).format(
        current=votes,
        required=request.required_votes,
        requirement_hint=_refill_requirement_hint(request, supporters, votes),
        minutes=_remaining_minutes(request),
    )


def format_refill_success(
    request: GroupRoastRefillRequest,
    votes: int,
    benefited: int,
    max_charges: int,
) -> str:
    return random.choice(ROAST_REFILL_SUCCESS_TEXTS).format(
        votes=votes,
        benefited=benefited,
        success_count=request.success_count_before + 1,
        max_charges=max_charges,
    )


def pick_refill_unsupported_text() -> str:
    return random.choice(ROAST_REFILL_UNSUPPORTED_TEXTS)


def pick_refill_reaction_hint() -> str:
    return random.choice(ROAST_REFILL_REACTION_HINT_TEXTS)


# ================================ Notice 结算 ================================ #

def is_refill_notice(event: Any) -> bool:
    if getattr(event, "notice_type", "") != "group_msg_emoji_like":
        return False
    likes = getattr(event, "likes", None)
    if not isinstance(likes, list):
        return False
    return any(
        isinstance(item, dict) and str(item.get("emoji_id") or "") == ROAST_REFILL_EMOJI_ID
        for item in likes
    )


async def reconcile_refill_request(
    bot: Bot,
    request: GroupRoastRefillRequest,
    *,
    fast_below_threshold: bool = False,
) -> GroupRoastRefillCompleteResult:
    """重放一次验票与结算；供 Notice 和命令恢复路径共同调用。"""

    if not is_group_rollpig_enabled(request.group_id):
        # Notice 不经过命令 matcher 的群开关守卫。关闭期间保留 voting，重新
        # 开启后仍可由新 reaction 或命令恢复，不在后台擅自补充次数。
        return GroupRoastRefillCompleteResult(False, "group_disabled", request)

    if not request.message_id:
        # prepare 与消息发送/绑定之间存在很短的并发窗口。旁路请求只能等待，
        # 绝不能把另一名用户正在创建的共享申请标记为失败。
        return GroupRoastRefillCompleteResult(False, "unbound", request)

    raw_voters = await fetch_refill_reactors(bot, request.message_id)
    raw_supporter_count = len(raw_voters - {str(bot.self_id)})
    if fast_below_threshold and (
        raw_supporter_count < ROAST_REFILL_MIN_DISTINCT_VOTERS
        or raw_supporter_count * 2 < request.required_votes
    ):
        # 每人最多计两票；即使全部是管理员也不可能达标时，才跳过成员名单查询。
        return GroupRoastRefillCompleteResult(False, "pending", request)

    eligibility = await get_refill_eligible_users(
        bot,
        request.group_id,
        request.date_str,
    )
    valid_voter_ids = raw_voters & eligibility.eligible_user_ids
    manager_voter_ids = valid_voter_ids & eligibility.manager_user_ids
    excluded_user_ids = (
        eligibility.active_user_ids - eligibility.eligible_user_ids
    ) | {str(bot.self_id)}
    max_charges = resolve_roast_charge_max()
    # 群成员与 reaction 查询期间开关可能变化；在实际修改配额前再确认一次。
    if not is_group_rollpig_enabled(request.group_id):
        return GroupRoastRefillCompleteResult(False, "group_disabled", request)
    result = await store.complete_group_roast_refill(
        request_id=request.request_id,
        message_id=request.message_id,
        voter_ids=sorted(valid_voter_ids),
        manager_voter_ids=sorted(manager_voter_ids),
        # Cloud/本地 Store 会再次按日活求交集；这里额外排除已退群用户，
        # 保证他们既不计票，也不会得到全局烧烤次数补充。
        excluded_user_ids=sorted(excluded_user_ids),
        max_charges=max_charges,
    )
    if (
        result.completed
        and result.request is not None
        and is_group_rollpig_enabled(result.request.group_id)
    ):
        await bot.send_group_msg(
            group_id=int(result.request.group_id),
            message=format_refill_success(
                result.request,
                votes=result.effective_votes,
                benefited=len(result.benefited_user_ids),
                max_charges=max_charges,
            ),
        )
    return result


async def process_refill_notice(bot: Bot, event: Any) -> None:
    """Notice 只唤醒校验；最终名单、资格与一次性状态迁移都重新从后端确认。"""

    if not is_refill_notice(event):
        return
    group_id = str(getattr(event, "group_id", "") or "")
    message_id = str(getattr(event, "message_id", "") or "")
    if not group_id or not message_id:
        return

    request = await find_group_roast_refill(
        group_id,
        message_id=message_id,
        reference_ts=getattr(event, "time", None),
    )
    if (
        request is None
        or request.message_id != message_id
        or request.delivery_bot_id != str(bot.self_id)
    ):
        return
    try:
        # reaction 原始人数尚未达到门槛时必然无法通过，跳过昂贵的群成员与日活读取。
        await reconcile_refill_request(bot, request, fast_below_threshold=True)
    except RoastRefillReactionError as error:
        logger.warning(f"rollpig 烤箱续火验票失败: request={request.request_id} error={error}")
        if error.message_missing or error.capability_unsupported:
            reason = "message_missing" if error.message_missing else "reaction_unsupported"
            await store.fail_group_roast_refill(request.request_id, message_id, reason)
