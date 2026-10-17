"""烤猪与烤群友命令（JianerCore 原生实现）。"""

from __future__ import annotations

import random

from jianer import segments
from jianer.plugins.builtin.alconna import Command

from bot import plugin_state

from ..data_manager import LocalStoreUnavailableError
from ..helpers import (
    GroupMemberLookupError,
    event_plain_text,
    event_replied_message_id,
    get_event_group_id,
    get_event_user_name,
    get_group_member_display_name,
    get_group_roll_candidates,
    finish_roast_outcome,
    guard_store_errors,
    has_extra_text,
    reply,
    reply_to,
    resolve_roast_target,
    send_rendered_pig,
)
from ...host import Bot, bot_for, logger
from ..reservation_delivery import register_owned_reservation
from ..reservation_flow import deliver_newly_ready_reservations
from ..resource_manager import get_pig_by_id
from ..roast_flow import (
    RoastFoodMissingError,
    build_member_roast_outcome,
    build_self_roast_data,
    detect_force_roast_mode,
    format_roast_outcome_log,
    format_cooldown_message,
    pick_force_limit_text,
    pick_food_pig,
    pick_member_target_block_text,
    pick_random_target_block_text,
    pick_reservation_prepare_text,
    pick_self_roast_block_text,
)
from ..roast_refill import extract_message_id
from ..roll_flow import RECORDED_PIG_RESOURCE_MISSING_TEXT, resolve_daily_pig
from ..runtime import resolve_roast_charge_max, resolve_roast_cooldown_seconds
from ..store import store
from ..store.cloud import CloudReservationUnsupportedError, CloudStoreError
from ..store.models import RoastEvent
from ..texts import (
    AUTO_ROLL_ROAST_TEXTS,
    PROTECTION_BLOCK_TEXTS,
    PROTECTION_BREAK_TEXTS,
    RANDOM_ROAST_INTRO_TEXTS,
    ROAST_BOT_TEXTS,
    UNROLLED_FORCE_ROAST_BACKFIRE_TEXTS,
    UNROLLED_ROAST_BACKFIRE_TEXTS,
    UNROLLED_ROAST_WARNING_TEXTS,
)

_REMINDER = str(plugin_state.get_runtime().get("reminder", ""))

_JOIN_PHRASES = {"加入", "加入预约", "加入烤猪"}


# ================================ 预约通知加入 ================================ #


def _register_preparation_owner(preparation, current_bot_id: str) -> None:
    """若已有预约归当前 Bot 负责，则恢复轮询。"""

    reservation = preparation.reservation
    if reservation and reservation.delivery_bot_id == str(current_bot_id):
        register_owned_reservation(str(current_bot_id))


def _is_reservation_reply(event) -> bool:
    """只接收回复当前 Bot 的完整短语，避免抢占普通聊天和其他 Bot。"""

    replied = event_replied_message_id(event)
    if not replied:
        return False
    if event_plain_text(event) not in _JOIN_PHRASES:
        return False
    self_id = str(getattr(event, "self_id", "") or "")
    message = getattr(event, "message", None)
    for content in getattr(message, "contents", []) or []:
        if isinstance(content, (segments.Text, segments.Reply)):
            continue
        if isinstance(content, segments.At) and str(content.qq) == self_id:
            continue
        return False
    return True


async def _send_reservation_notice(
    actions, event, preparation, *, attacker_name: str, target_name: str, prefix: str = ""
) -> None:
    """发送后绑定实际消息 ID；关联失败不能将已成功的预约误报为失败。"""

    message = reply_to(
        getattr(event, "message_id", None),
        prefix
        + pick_reservation_prepare_text(
            preparation, attacker_name=attacker_name, target_name=target_name
        ),
    )
    receipt = await reply(actions, event, message)
    if (
        preparation.reservation
        and preparation.status in {"reservation_created", "reservation_joined", "already_joined"}
    ):
        message_id = extract_message_id(
            getattr(receipt, "message_id", None) if receipt is not None else None
        )
        if message_id:
            try:
                bound = await store.bind_roast_reservation_message(
                    reservation_id=preparation.reservation.reservation_id,
                    bot_id=str(getattr(event, "self_id", "")),
                    group_id=get_event_group_id(event),
                    message_id=str(message_id),
                    date_str=preparation.reservation.date_str,
                )
                if not bound:
                    logger.warning("预约通知关联未保存，仍可通过烤群友 @目标加入")
            except CloudReservationUnsupportedError:
                pass
            except (CloudStoreError, LocalStoreUnavailableError) as error:
                logger.warning(f"预约已完成，但通知关联保存失败: {error}")


async def handle_message_hook(event, actions) -> bool:
    """``on_message`` 钩子：回复预约通知加入 + 黏连写法补充。"""

    from .fallback import handle_glued_command

    if _is_reservation_reply(event):
        return await _handle_reservation_join(event, actions)
    return await handle_glued_command(event, actions)


async def _handle_reservation_join(event, actions) -> bool:
    """回复预约通知加入。"""

    group_id = get_event_group_id(event)
    if group_id and not _group_enabled(group_id):
        return False

    name = get_event_user_name(event)
    try:
        preparation = await store.join_roast_reservation_by_message(
            bot_id=str(getattr(event, "self_id", "")),
            group_id=group_id,
            message_id=str(event_replied_message_id(event)),
            attacker_id=str(getattr(event, "user_id", "")),
            attacker_name=name,
        )
    except CloudReservationUnsupportedError:
        return False
    except Exception as error:
        if await guard_store_errors(actions, event, error):
            return True
        raise

    if preparation.status == "message_not_found":
        return False
    _register_preparation_owner(preparation, str(getattr(event, "self_id", "")))
    await _send_reservation_notice(
        actions,
        event,
        preparation,
        attacker_name=name,
        target_name=(
            preparation.reservation.target_name if preparation.reservation else ""
        ),
    )
    return True


def _group_enabled(group_id: str) -> bool:
    from ..runtime import is_group_rollpig_enabled

    return is_group_rollpig_enabled(group_id)


# ================================ 未抽猪攻击者限制 ================================ #


def _classify_roast_target(attacker_id: str, target_id: str | None, bot_id: str) -> str:
    """分类目标类型：missing / self / bot / member。"""

    if not target_id:
        return "missing"
    if target_id == attacker_id:
        return "self"
    if target_id == bot_id:
        return "bot"
    return "member"


async def _finish_unrolled_attacker_attempt(
    actions, event, attacker_name: str, force_mode
) -> None:
    """处理未抽猪玩家的烤群友惩罚（首次警告，二次反噬）。"""

    message_id = getattr(event, "message_id", None)
    try:
        attempt = await store.record_unrolled_roast_attempt(
            str(getattr(event, "user_id", ""))
        )
    except CloudReservationUnsupportedError:
        await reply(
            actions,
            event,
            reply_to(message_id, random.choice(UNROLLED_ROAST_WARNING_TEXTS)),
        )
        return

    if attempt.count <= 1:
        await reply(
            actions,
            event,
            reply_to(message_id, random.choice(UNROLLED_ROAST_WARNING_TEXTS)),
        )
        return

    try:
        food_pig = pick_food_pig().copy()
    except RoastFoodMissingError as error:
        await reply(actions, event, reply_to(message_id, str(error)))
        return
    pool = (
        UNROLLED_FORCE_ROAST_BACKFIRE_TEXTS
        if force_mode in {"normal", "super"}
        else UNROLLED_ROAST_BACKFIRE_TEXTS
    )
    food_pig["analysis"] = random.choice(pool).format(
        attacker=attacker_name, food=food_pig.get("name", "熟食")
    )
    await send_rendered_pig(actions, event, food_pig, cache_final_card=False)


async def _load_attacker_pig_or_none(actions, event, attacker_name: str, force_mode):
    """获取发起者今日小猪；未抽猪或资源缺失时结束流程并返回 None。"""

    attacker_pig_id = await store.get_daily_roll(str(getattr(event, "user_id", "")))
    if not attacker_pig_id:
        await _finish_unrolled_attacker_attempt(actions, event, attacker_name, force_mode)
        return None
    attacker_pig = get_pig_by_id(attacker_pig_id)
    if not attacker_pig:
        await reply(
            actions,
            event,
            reply_to(getattr(event, "message_id", None), RECORDED_PIG_RESOURCE_MISSING_TEXT),
        )
        return None
    return attacker_pig


# ================================ 5. 今日烤猪 ================================ #


@Command(f"{_REMINDER}今日烤猪").handle()
async def cmd_roast(event, actions, user_message=None):
    if has_extra_text(event, "今日烤猪"):
        return False
    user_id = str(getattr(event, "user_id", ""))
    group_id = get_event_group_id(event)
    attacker_name = get_event_user_name(event)
    message_id = getattr(event, "message_id", None)
    try:
        resolution = await resolve_daily_pig(user_id, group_id)
    except Exception as error:
        if await guard_store_errors(actions, event, error):
            return True
        raise

    original_pig = resolution.pig
    if resolution.recorded_pig_missing:
        await reply(
            actions,
            event,
            reply_to(message_id, RECORDED_PIG_RESOURCE_MISSING_TEXT),
        )
        return True
    if resolution.missing_resources or not original_pig:
        await reply(actions, event, reply_to(message_id, "猪圈埋房了（数据缺失）"))
        return True

    auto_roll_hint = ""
    if resolution.was_auto_created:
        auto_roll_hint = (
            random.choice(AUTO_ROLL_ROAST_TEXTS).format(name=original_pig["name"]) + "\n"
        )

    block_text = pick_self_roast_block_text(original_pig)
    if block_text:
        await reply(actions, event, reply_to(message_id, block_text))
        if resolution.was_auto_created:
            await deliver_newly_ready_reservations(str(getattr(event, "self_id", "")))
        return True

    try:
        roasted_pig_data, food_name = await build_self_roast_data(original_pig)
    except RoastFoodMissingError as error:
        await reply(actions, event, reply_to(message_id, str(error)))
        return True

    if group_id:
        await store.append_roast_event(
            RoastEvent(
                event_type="self_roast",
                attacker_id=user_id,
                target_id=user_id,
                attacker_name=attacker_name,
                target_name=attacker_name,
                food=food_name,
                group_id=group_id,
            )
        )
    await send_rendered_pig(
        actions,
        event,
        roasted_pig_data,
        extra_text=auto_roll_hint,
        cache_final_card=False,
        after_send=(
            (lambda: deliver_newly_ready_reservations(str(getattr(event, "self_id", ""))))
            if resolution.was_auto_created
            else None
        ),
    )
    return True


# ================================ 5.5 烤群友 ================================ #


@Command(f"{_REMINDER}烤群友").handle()
@Command(f"{_REMINDER}烤群友 <target>").handle()
@Command(f"{_REMINDER}加急生火").handle()
@Command(f"{_REMINDER}加急生火 <target>").handle()
async def cmd_roast_member(event, actions, user_message=None, target: str = ""):
    attacker_id = str(getattr(event, "user_id", ""))
    attacker_name = get_event_user_name(event)
    group_id = get_event_group_id(event)
    message_id = getattr(event, "message_id", None)
    if not group_id:
        await reply(actions, event, reply_to(message_id, "烤群友只能在群聊里进行。"))
        return True

    plain = event_plain_text(event)
    force_mode = detect_force_roast_mode(plain, attacker_id)
    if force_mode == "super_denied":
        await reply(
            actions,
            event,
            reply_to(message_id, "口令【强行点火】仅 superuser 可用。"),
        )
        return True

    bot = bot_for(actions, getattr(event, "self_id", None))
    roast_target = await resolve_roast_target(bot, event)
    target_id = roast_target.target_id
    target_name = roast_target.target_name
    target_kind = _classify_roast_target(
        attacker_id, target_id, str(getattr(event, "self_id", ""))
    )

    if target_kind == "missing":
        await reply(actions, event, reply_to(message_id, "请 At 或回复你要烤的群友！"))
        return True
    if target_kind == "self":
        await reply(
            actions, event, reply_to(message_id, "对自己好一点，别自焚。请发送「今日烤猪」。")
        )
        return True

    # 目标是 Bot 自身 → 特殊反噬，不消耗 CD，纯文本回复。
    if target_kind == "bot":
        try:
            food_name = pick_food_pig()["name"]
        except RoastFoodMissingError:
            food_name = "美食"
        bot_text = random.choice(ROAST_BOT_TEXTS).format(
            attacker=attacker_name, food=food_name
        )
        logger.info(
            f"[烤群友→Bot] 特殊反噬 | 凶手={attacker_name}({attacker_id}) 变成={food_name}"
        )
        await store.append_roast_event(
            RoastEvent(
                event_type="bot_backfire",
                attacker_id=attacker_id,
                target_id=target_id,
                attacker_name=attacker_name,
                target_name=target_name,
                food=food_name,
                group_id=group_id,
            )
        )
        await reply(actions, event, reply_to(message_id, bot_text))
        return True

    if not roast_target.is_group_member:
        await reply(
            actions,
            event,
            reply_to(message_id, "暂时无法确认对方仍在本群，请核对成员后再试。"),
        )
        return True

    attacker_pig = await _load_attacker_pig_or_none(actions, event, attacker_name, force_mode)
    if not attacker_pig:
        return True
    await store.mark_group_roll_seen(attacker_id, attacker_pig["id"], group_id)

    # ================================ 目标状态与预约准备 ================================ #
    preparation = None
    try:
        preparation = await store.prepare_roast_reservation(
            attacker_id=attacker_id,
            attacker_name=attacker_name,
            attacker_pig_id=attacker_pig["id"],
            target_id=target_id,
            target_name=target_name,
            group_id=group_id,
            delivery_bot_id=str(getattr(event, "self_id", "")),
            force_mode=force_mode,
            cooldown_seconds=resolve_roast_cooldown_seconds(),
            max_charges=resolve_roast_charge_max(),
        )
    except CloudReservationUnsupportedError:
        target_pig_id = await store.get_daily_roll(target_id)
        if not target_pig_id:
            await reply(
                actions,
                event,
                reply_to(message_id, f"【{target_name}】今天还没抽猪，没法下嘴！"),
            )
            return True
        target_pig = get_pig_by_id(target_pig_id)
        if not target_pig:
            await reply(
                actions,
                event,
                reply_to(message_id, "目标的小猪记录存在，但本机资源暂时缺失，请稍后再试。"),
            )
            return True
        if await store.is_protected(group_id, target_id):
            if force_mode in {"normal", "super"}:
                break_text = random.choice(PROTECTION_BREAK_TEXTS).format(target=target_name)
                await reply(actions, event, reply_to(message_id, break_text))
            else:
                await reply(
                    actions,
                    event,
                    reply_to(
                        message_id,
                        random.choice(PROTECTION_BLOCK_TEXTS).format(target=target_name),
                    ),
                )
                return True
    else:
        # Cloud 已创建预约但响应丢失时，重试会返回 already_joined。
        _register_preparation_owner(preparation, str(getattr(event, "self_id", "")))
        if preparation.status != "target_ready":
            if preparation.status == "protected":
                await reply(
                    actions,
                    event,
                    reply_to(
                        message_id,
                        random.choice(PROTECTION_BLOCK_TEXTS).format(target=target_name),
                    ),
                )
                return True
            if preparation.status == "cooldown_denied" and preparation.cooldown:
                await reply(
                    actions,
                    event,
                    reply_to(
                        message_id,
                        format_cooldown_message(preparation.cooldown.remaining_seconds),
                    ),
                )
                return True
            if preparation.status == "force_denied":
                await reply(
                    actions,
                    event,
                    reply_to(
                        message_id,
                        pick_force_limit_text(attacker_name, target_name),
                    ),
                )
                return True
            prefix = ""
            if preparation.protection_broken:
                prefix = (
                    random.choice(PROTECTION_BREAK_TEXTS).format(target=target_name) + "\n"
                )
            await _send_reservation_notice(
                actions,
                event,
                preparation,
                attacker_name=attacker_name,
                target_name=target_name,
                prefix=prefix,
            )
            return True
        target_pig = get_pig_by_id(preparation.target_pig_id)

    if preparation is not None and preparation.protection_broken:
        break_text = random.choice(PROTECTION_BREAK_TEXTS).format(target=target_name)
        logger.info(
            f"[烤群友] 保护被突破 | 凶手={attacker_name}({attacker_id}) 目标={target_name}({target_id})"
        )
        await reply(actions, event, reply_to(message_id, break_text))

    if not target_pig:
        await reply(
            actions, event, reply_to(message_id, "目标的小猪资源缺失，暂时无法开火。")
        )
        return True
    await store.mark_group_roll_seen(target_id, target_pig["id"], group_id)

    block_text = pick_member_target_block_text(target_name, target_pig)
    if block_text:
        await reply(actions, event, reply_to(message_id, block_text))
        return True

    # 模式化限制/计数
    if force_mode == "normal":
        if not await store.consume_force_usage(attacker_id):
            await reply(
                actions,
                event,
                reply_to(message_id, pick_force_limit_text(attacker_name, target_name)),
            )
            return True
    elif force_mode is None:
        cooldown_result = await store.consume_roast_cooldown(
            attacker_id,
            cooldown_seconds=resolve_roast_cooldown_seconds(),
            max_charges=resolve_roast_charge_max(),
        )
        if not cooldown_result.allowed:
            await reply(
                actions,
                event,
                reply_to(
                    message_id, format_cooldown_message(cooldown_result.remaining_seconds)
                ),
            )
            return True
    # super 模式：无限制，不消耗后门次数，不走 CD

    try:
        outcome = await build_member_roast_outcome(
            attacker_pig=attacker_pig,
            target_pig=target_pig,
            attacker_name=attacker_name,
            target_name=target_name,
            force_mode=force_mode,
        )
    except RoastFoodMissingError as error:
        await reply(actions, event, reply_to(message_id, str(error)))
        return True

    logger.info(
        format_roast_outcome_log(
            "烤群友",
            attacker_name=attacker_name,
            attacker_id=attacker_id,
            target_name=target_name,
            target_id=target_id,
            outcome=outcome,
            force_mode=force_mode,
        )
    )

    await finish_roast_outcome(
        actions,
        event,
        outcome,
        attacker_id=attacker_id,
        attacker_name=attacker_name,
        target_id=target_id,
        target_name=target_name,
        group_id=group_id,
        daily_feed_eligible=(force_mode is None),
    )
    return True


# ================================ 5.6 随机烤群友 ================================ #


async def _load_random_roast_context(actions, bot: Bot, event, attacker_name: str):
    """先确认群内存在可选目标，再检查攻击者并记录未抽猪违规。"""

    attacker_id = str(getattr(event, "user_id", ""))
    bot_id = str(getattr(event, "self_id", ""))
    message_id = getattr(event, "message_id", None)
    try:
        candidates = await get_group_roll_candidates(
            bot, get_event_group_id(event), {attacker_id, bot_id}
        )
    except GroupMemberLookupError:
        await reply(
            actions,
            event,
            reply_to(message_id, "暂时无法读取当前群成员，随机烤猪没有执行。"),
        )
        return None, []
    if not candidates:
        await reply(
            actions, event, reply_to(message_id, "今天还没有别人抽猪，没有可以烤的目标！")
        )
        return None, []
    attacker_pig = await _load_attacker_pig_or_none(actions, event, attacker_name, None)
    return attacker_pig, candidates


@Command(f"{_REMINDER}随机烤群友").handle()
@Command(f"{_REMINDER}随机烤猪").handle()
@Command(f"{_REMINDER}抽个群友烤了").handle()
async def cmd_random_roast(event, actions, user_message=None):
    if has_extra_text(event, "随机烤群友", "随机烤猪", "抽个群友烤了"):
        return False
    attacker_id = str(getattr(event, "user_id", ""))
    attacker_name = get_event_user_name(event)
    group_id = get_event_group_id(event)
    message_id = getattr(event, "message_id", None)
    if not group_id:
        await reply(actions, event, reply_to(message_id, "随机烤猪只能在群聊里进行。"))
        return True

    bot = bot_for(actions, getattr(event, "self_id", None))
    attacker_pig, candidates = await _load_random_roast_context(
        actions, bot, event, attacker_name
    )
    if not attacker_pig:
        return True
    await store.mark_group_roll_seen(attacker_id, attacker_pig["id"], group_id)

    target_id = random.choice(candidates)
    target_name = await get_group_member_display_name(bot, group_id, target_id)

    target_pig = get_pig_by_id(await store.get_daily_roll(target_id))
    if not target_pig:
        await reply(
            actions,
            event,
            reply_to(
                message_id,
                f"系统随机选中了【{target_name}】，但对方的猪数据异常。",
            ),
        )
        return True
    await store.mark_group_roll_seen(target_id, target_pig["id"], group_id)

    if await store.is_protected(group_id, target_id):
        protection_text = random.choice(PROTECTION_BLOCK_TEXTS).format(target=target_name)
        await reply(
            actions,
            event,
            reply_to(
                message_id, f"系统随机选中了【{target_name}】——\n{protection_text}"
            ),
        )
        return True

    block_text = pick_random_target_block_text(target_name, target_pig)
    if block_text:
        await reply(actions, event, reply_to(message_id, block_text))
        return True

    # 只有可实际进入烧烤判定的目标才消耗次数。
    cooldown_result = await store.consume_roast_cooldown(
        attacker_id,
        cooldown_seconds=resolve_roast_cooldown_seconds(),
        max_charges=resolve_roast_charge_max(),
    )
    if not cooldown_result.allowed:
        await reply(
            actions,
            event,
            reply_to(
                message_id, format_cooldown_message(cooldown_result.remaining_seconds)
            ),
        )
        return True

    intro = random.choice(RANDOM_ROAST_INTRO_TEXTS).format(target=target_name) + "\n"
    try:
        outcome = await build_member_roast_outcome(
            attacker_pig=attacker_pig,
            target_pig=target_pig,
            attacker_name=attacker_name,
            target_name=target_name,
            intro_text=intro,
        )
    except RoastFoodMissingError as error:
        await reply(actions, event, reply_to(message_id, str(error)))
        return True

    logger.info(
        format_roast_outcome_log(
            "随机烤群友",
            attacker_name=attacker_name,
            attacker_id=attacker_id,
            target_name=target_name,
            target_id=target_id,
            outcome=outcome,
        )
    )

    await finish_roast_outcome(
        actions,
        event,
        outcome,
        attacker_id=attacker_id,
        attacker_name=attacker_name,
        target_id=target_id,
        target_name=target_name,
        group_id=group_id,
        daily_feed_eligible=True,
    )
    return True


__all__ = [
    "cmd_random_roast",
    "cmd_roast",
    "cmd_roast_member",
    "handle_message_hook",
]
