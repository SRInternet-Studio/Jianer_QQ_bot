"""烤箱续火（补货投票）命令与通知处理（JianerCore 原生实现）。"""

from __future__ import annotations

import asyncio
import random

from jianer.plugins.builtin.alconna import Command

from bot import plugin_state

from ..helpers import (
    get_event_group_id,
    get_event_user_name,
    has_extra_text,
    reply,
    reply_to,
)
from ...host import Bot, bot_for, logger
from ..roast_refill import (
    RoastRefillReactionError,
    add_refill_reaction,
    build_refill_created_message,
    extract_message_id,
    find_group_roast_refill,
    format_existing_refill,
    get_refill_eligible_users,
    pick_refill_reaction_hint,
    pick_refill_unsupported_text,
    process_refill_notice,
    reconcile_refill_request,
)
from ..runtime import resolve_roast_charge_max, rollpig_date_str
from ..store import store
from ..store.cloud import CloudRoastRefillUnsupportedError
from ..store.models import (
    GroupRoastRefillPrepareResult,
    GroupRoastRefillRequest,
    ROAST_REFILL_VOTE_WEIGHT_POLICY,
    legacy_roast_refill_threshold,
    roast_refill_threshold,
)
from ..texts import (
    ROAST_REFILL_INACTIVE_INITIATOR_TEXTS,
    ROAST_REFILL_INSUFFICIENT_ACTIVE_TEXTS,
)

_REMINDER = str(plugin_state.get_runtime().get("reminder", ""))

# 短暂重试消息绑定，应对网络抖动。
ROAST_REFILL_BIND_RETRY_DELAYS = (0.25, 0.75)


def _can_start_refill(user_id: str, eligible_user_ids: set[str] | frozenset[str]) -> bool:
    """仅允许本群当日有效活跃用户创建新的补货申请。"""

    return str(user_id) in eligible_user_ids


async def _fail_unusable_request(request_id: str, message_id: str, reason: str) -> None:
    try:
        await store.fail_group_roast_refill(request_id, message_id, reason)
    except Exception as error:
        logger.warning(f"rollpig 烤箱续火失败状态写入异常: request={request_id} error={error}")


async def _bind_refill_message_with_retry(
    request_id: str, message_id: str
) -> GroupRoastRefillRequest | None:
    """重试绑定补货消息 ID。"""

    total_attempts = len(ROAST_REFILL_BIND_RETRY_DELAYS) + 1
    for attempt in range(total_attempts):
        try:
            return await store.bind_group_roast_refill_message(request_id, message_id)
        except Exception as error:
            if attempt >= len(ROAST_REFILL_BIND_RETRY_DELAYS):
                logger.error(
                    "rollpig 烤箱续火消息绑定重试耗尽: "
                    f"request={request_id} message={message_id} error={error}"
                )
                return None
            delay = ROAST_REFILL_BIND_RETRY_DELAYS[attempt]
            logger.warning(
                "rollpig 烤箱续火消息绑定临时失败，准备重试: "
                f"request={request_id} message={message_id} "
                f"attempt={attempt + 1}/{total_attempts} error={error}"
            )
            await asyncio.sleep(delay)
    return None


async def _handle_post_send_probe_error(
    bot: Bot, request: GroupRoastRefillRequest, error: RoastRefillReactionError
) -> bool:
    """处理发消息后的适配器异常，返回是否终止投票。"""

    if not error.message_missing and not error.capability_unsupported:
        logger.warning(
            "rollpig 烤箱续火发起后验票临时失败，申请保持有效: "
            f"request={request.request_id} error={error}"
        )
        return False

    reason = "message_missing" if error.message_missing else "reaction_unsupported"
    await _fail_unusable_request(request.request_id, request.message_id, reason)
    text = (
        "补货投票消息已经失效，本轮申请已停止，请重新发起。"
        if error.message_missing
        else pick_refill_unsupported_text()
    )
    logger.warning(
        f"rollpig 烤箱续火投票无法继续: request={request.request_id} reason={reason} error={error}"
    )
    await bot.send_group_msg(group_id=int(request.group_id), message=text)
    return True


async def _reconcile_after_bind(bot: Bot, request: GroupRoastRefillRequest) -> bool:
    """绑定消息后补验票。"""

    try:
        result = await reconcile_refill_request(bot, request)
    except RoastRefillReactionError as error:
        return await _handle_post_send_probe_error(bot, request, error)
    return result.status != "pending"


def _preparation_matches_members(
    preparation: GroupRoastRefillPrepareResult,
    eligible_user_ids: set[str] | frozenset[str],
) -> bool:
    """校验补货门槛快照与当前活跃成员是否一致。"""

    request = preparation.request
    if request is None:
        return False
    active_count = len(eligible_user_ids)
    expected_thresholds = {
        roast_refill_threshold(active_count, request.success_count_before),
        legacy_roast_refill_threshold(active_count, request.success_count_before),
    }
    if (
        request.active_count_snapshot != active_count
        or (request.required_ratio, request.required_votes) not in expected_thresholds
    ):
        return False
    returned_active = {str(user_id) for user_id in preparation.active_user_ids if user_id}
    return not returned_active or returned_active == eligible_user_ids


async def _describe_existing_refill(
    bot: Bot, request: GroupRoastRefillRequest
) -> str | None:
    """重查并尝试补结算已有投票；返回 None 表示成功文案已经发到群里。"""

    if not request.message_id:
        return "补货申请正在生成，请稍后再查看票数。"

    delivery_bot = bot
    if request.delivery_bot_id != str(bot.self_id):
        from ...host import get_bot

        try:
            delivery_bot = get_bot(request.delivery_bot_id)
        except KeyError:
            return "现有补货申请仍在有效期内，但负责投递的 Bot 暂时离线，票数稍后再查。"

    try:
        result = await reconcile_refill_request(delivery_bot, request)
    except RoastRefillReactionError as error:
        if error.message_missing or error.capability_unsupported:
            reason = "message_missing" if error.message_missing else "reaction_unsupported"
            await _fail_unusable_request(request.request_id, request.message_id, reason)
            if error.message_missing:
                return "原补货投票消息已经失效，本轮申请已停止，请重新发起。"
            return pick_refill_unsupported_text()
        logger.warning(
            f"rollpig 烤箱续火恢复验票失败: request={request.request_id} error={error}"
        )
        return "暂时没能读取当前票数，申请仍然有效，请稍后再试。"

    if result.completed:
        return None
    if result.status == "pending":
        return format_existing_refill(
            request,
            supporters=len(result.valid_voter_ids),
            votes=result.effective_votes,
        )
    if result.status == "unbound":
        return "补货申请正在生成，请稍后再查看票数。"
    if result.status == "group_disabled":
        return "本群 RollPig 当前已关闭，补货申请暂不结算。"
    if result.status == "succeeded":
        return "本轮烤箱续火已经完成。"
    if result.status in {"expired", "failed"}:
        return "上一轮补货申请已经结束，请重新发起。"
    return "补货申请状态刚刚发生变化，请稍后再试。"


@Command(f"{_REMINDER}烤箱续火").handle()
@Command(f"{_REMINDER}烤箱补货").handle()
@Command(f"{_REMINDER}重置烤猪次数").handle()
@Command(f"{_REMINDER}恢复烧烤配额").handle()
@Command(f"{_REMINDER}申请烤箱补给").handle()
@Command(f"{_REMINDER}重置烧烤次数").handle()
async def cmd_roast_refill(event, actions, user_message=None):
    if has_extra_text(
        event,
        "烤箱续火",
        "烤箱补货",
        "重置烤猪次数",
        "恢复烧烤配额",
        "申请烤箱补给",
        "重置烧烤次数",
    ):
        return False
    message_id = getattr(event, "message_id", None)
    group_id = get_event_group_id(event)
    if not group_id:
        await reply(actions, event, reply_to(message_id, "烤箱续火只能在群聊中发起。"))
        return True

    bot = bot_for(actions, getattr(event, "self_id", None))
    date_str = rollpig_date_str()

    # ================================ 跨日旧申请恢复 ================================ #

    try:
        existing = await find_group_roast_refill(
            group_id, reference_ts=getattr(event, "time", None)
        )
    except CloudRoastRefillUnsupportedError:
        await reply(
            actions,
            event,
            reply_to(
                message_id,
                "当前 Cloud 版本尚未支持烤箱续火，请先升级 RollPig Cloud。其他功能不受影响。",
            ),
        )
        return True

    if existing is not None:
        text = await _describe_existing_refill(bot, existing)
        if text is None:
            return True
        await reply(actions, event, reply_to(message_id, text))
        return True

    # ================================ 当前群成员门槛 ================================ #

    try:
        eligibility = await get_refill_eligible_users(bot, group_id, date_str)
    except RoastRefillReactionError as error:
        logger.warning(f"rollpig 烤箱续火群成员核对失败: group={group_id} error={error}")
        await reply(
            actions, event, reply_to(message_id, "暂时无法核对本群活跃小猪，请稍后再试。")
        )
        return True

    eligible_user_ids = eligibility.eligible_user_ids
    if len(eligible_user_ids) < 3:
        await reply(
            actions,
            event,
            reply_to(
                message_id, random.choice(ROAST_REFILL_INSUFFICIENT_ACTIVE_TEXTS)
            ),
        )
        return True
    if not _can_start_refill(str(getattr(event, "user_id", "")), eligible_user_ids):
        await reply(
            actions,
            event,
            reply_to(message_id, random.choice(ROAST_REFILL_INACTIVE_INITIATOR_TEXTS)),
        )
        return True

    # ================================ 原子创建与发送 ================================ #

    try:
        preparation = await store.prepare_group_roast_refill(
            group_id=group_id,
            initiator_id=str(getattr(event, "user_id", "")),
            initiator_name=get_event_user_name(event),
            delivery_bot_id=str(getattr(event, "self_id", "")),
            eligible_user_ids=sorted(eligible_user_ids),
            date_str=date_str,
        )
    except CloudRoastRefillUnsupportedError:
        await reply(
            actions,
            event,
            reply_to(
                message_id,
                "当前 Cloud 版本尚未支持烤箱续火，请先升级 RollPig Cloud。其他功能不受影响。",
            ),
        )
        return True

    if preparation.status == "insufficient_active":
        await reply(
            actions,
            event,
            reply_to(message_id, random.choice(ROAST_REFILL_INSUFFICIENT_ACTIVE_TEXTS)),
        )
        return True
    if preparation.request is None:
        await reply(
            actions, event, reply_to(message_id, "补货申请没有成功建立，请稍后再试。")
        )
        return True

    request = preparation.request
    if preparation.status == "existing":
        text = await _describe_existing_refill(bot, request)
        if text is None:
            return True
        await reply(actions, event, reply_to(message_id, text))
        return True

    if not _preparation_matches_members(preparation, eligible_user_ids):
        await _fail_unusable_request(
            request.request_id, request.message_id, "member_snapshot_mismatch"
        )
        await reply(
            actions,
            event,
            reply_to(
                message_id,
                "今日活跃记录与当前群成员不一致，为避免误补次数，本轮未开始；记录会在次日自动刷新。",
            ),
        )
        return True

    try:
        send_result = await bot.send_group_msg(
            group_id=group_id,
            message=build_refill_created_message(
                request,
                resolve_roast_charge_max(),
                manager_double_vote=(
                    preparation.vote_weight_policy == ROAST_REFILL_VOTE_WEIGHT_POLICY
                ),
            ),
        )
    except Exception as error:
        await _fail_unusable_request(request.request_id, "", "send_failed")
        logger.warning(
            f"rollpig 烤箱续火投票消息发送失败: request={request.request_id} error={error}"
        )
        await reply(
            actions,
            event,
            reply_to(message_id, "补货投票消息发送失败，本轮没有扣改任何次数。"),
        )
        return True

    sent_message_id = extract_message_id(send_result)
    if not sent_message_id:
        await _fail_unusable_request(request.request_id, "", "message_id_missing")
        await reply(
            actions, event, reply_to(message_id, pick_refill_unsupported_text())
        )
        return True
    bound = await _bind_refill_message_with_retry(request.request_id, sent_message_id)
    if bound is None:
        await _fail_unusable_request(
            request.request_id, sent_message_id, "bind_failed"
        )
        await reply(
            actions, event, reply_to(message_id, "投票消息状态绑定失败，本轮申请已停止。")
        )
        return True

    reaction_added = await add_refill_reaction(bot, sent_message_id)
    # 绑定重试期间的 reaction Notice 无法匹配未绑定申请；绑定成功后立即走
    # 完整验票，既完成能力探测，也补结算这个窗口内已经到齐的票。
    if await _reconcile_after_bind(bot, bound):
        return True
    if not reaction_added:
        await bot.send_group_msg(
            group_id=group_id, message=pick_refill_reaction_hint()
        )
    return True


async def handle_notice(event, actions) -> bool:
    """通知事件入口：续标识投票的旁路验票。"""

    notice_type = str(getattr(event, "notice_type", "") or "")
    if notice_type not in {"reaction", "notify"}:
        return False
    payload = _notice_payload(event)
    if payload is None:
        return False

    bot = bot_for(actions, getattr(event, "self_id", None))
    try:
        await process_refill_notice(bot, payload)
    except CloudRoastRefillUnsupportedError:
        return False
    except Exception as error:
        # Notice 属于旁路事件，任何异常都不能影响其他插件的事件分发。
        logger.warning(f"rollpig 烤箱续火 Notice 处理失败: error={error}")
    return True


def _notice_payload(event) -> object | None:
    """把 JianerCore 的 reaction/notify 事件映射成续标识投票负载。"""

    code = getattr(event, "code", None)
    likes = getattr(event, "likes", None)
    if not likes and code is not None:
        likes = [
            {
                "emoji_id": str(code),
                "count": getattr(event, "count", None) or 1,
                "user_id": getattr(event, "operator_id", None),
            }
        ]
    if not isinstance(likes, list):
        return None
    return _NoticeLike(
        notice_type="group_msg_emoji_like",
        group_id=getattr(event, "group_id", None),
        message_id=getattr(event, "message_id", None),
        user_id=getattr(event, "user_id", None),
        time=getattr(event, "time", None),
        likes=likes,
    )


class _NoticeLike:
    """续标识投票负载；``process_refill_notice`` 只读取这些字段。"""

    def __init__(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


__all__ = ["cmd_roast_refill", "handle_notice"]
