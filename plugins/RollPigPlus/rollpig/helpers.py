"""Shared helpers for the RollPigPlus commands (JianerCore native)."""

from __future__ import annotations

import base64
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from jianer import common, segments
from jianer.plugins.builtin.alconna import Receipt, Target

from bot import plugin_state

from ..host import Bot, logger
from .card_renderer import render_pig_card_image
from .data_manager import LocalStoreUnavailableError
from .resource_manager import pig_resource_manager
from .runtime import is_group_rollpig_enabled, rollpig_date_str
from .store import store
from .store.cloud import CloudStoreError
from .store.models import DailyFeedResult, RoastEvent
from .texts import DAILY_FEED_ROAST_TEXTS


# ================================ 事件解析 ================================ #


def event_user_id(event: Any) -> str:
    return str(getattr(event, "user_id", "") or "")


def event_self_id(event: Any) -> str:
    return str(getattr(event, "self_id", "") or "")


def get_event_group_id(event: Any) -> str:
    """群号；私聊或未知事件返回空字符串。"""

    group_id = getattr(event, "group_id", None)
    return str(group_id) if group_id not in (None, "") else ""


def get_event_user_name(event: Any) -> str:
    """按群名片、昵称、用户 ID 的优先级取用户展示名。"""

    sender = getattr(event, "sender", None)
    if sender is not None:
        card = getattr(sender, "card", "") or ""
        nickname = getattr(sender, "nickname", "") or ""
        if card or nickname:
            return str(card or nickname)
    return event_user_id(event)


def event_message(event: Any) -> common.Message:
    message = getattr(event, "message", None)
    if isinstance(message, common.Message):
        return message
    return common.Message()


def event_plain_text(event: Any) -> str:
    text = getattr(event, "msg_str", None)
    if text is None:
        text = str(event_message(event))
    return str(text or "").strip()


def event_at_targets(event: Any) -> list[str]:
    return [
        str(content.qq)
        for content in event_message(event).contents
        if isinstance(content, segments.At)
    ]


def event_replied_message_id(event: Any) -> str | None:
    for content in event_message(event).contents:
        if isinstance(content, segments.Reply):
            return str(content.id)
    return None


def _text_only(event: Any) -> str:
    return "".join(
        str(content.text)
        for content in event_message(event).contents
        if isinstance(content, segments.Text)
    ).strip()


def command_tail(event: Any, *names: str) -> str:
    """命令名之后的剩余纯文本（已去掉宿主 reminder）。

    Alconna 只按前缀匹配命令，参数边界由命令自己判定：这样 ``随机小猪3``
    这类黏连写法与 ``随机小猪 3`` 都能工作，同时无参数命令可以拒绝多余文本。
    """

    text = _text_only(event)
    reminder = str(plugin_state.get_runtime().get("reminder") or "")
    if reminder and text.startswith(reminder):
        text = text[len(reminder) :].lstrip()
    for name in names:
        if text.startswith(name):
            return text[len(name) :].strip()
    return ""


def has_extra_text(event: Any, *names: str) -> bool:
    return bool(command_tail(event, *names))


def is_superuser_user(user_id: Any) -> bool:
    runtime = plugin_state.get_runtime()
    values: set[str] = set()
    for key in ("root_users", "super_users", "manage_users", "supers"):
        entries = runtime.get(key) or ()
        if isinstance(entries, (str, bytes)):
            entries = (entries,)
        for entry in entries:
            text = str(entry).strip()
            if text:
                values.add(text)
                values.add(text.rsplit(":", 1)[-1])
    return str(user_id) in values


# ================================ 发送 ================================ #


async def reply(actions: Any, event: Any, message: Any) -> Receipt | None:
    """Send ``message`` back to the conversation the event came from."""

    if message is None:
        return None
    try:
        return await Target.from_event(event).send(message, actions)
    except ValueError:
        return None


def reply_to(message_id: Any, message: Any) -> common.Message:
    body = message if isinstance(message, common.Message) else common.Message(message)
    if message_id is None:
        return common.Message(*body.contents)
    return common.Message(segments.Reply(str(message_id)), *body.contents)


def image_message(
    payload: bytes | str, extra_text: str = "", trailing_text: str = ""
) -> common.Message:
    message = common.Message()
    if extra_text:
        message.contents.append(segments.Text(extra_text.rstrip("\r\n")))
    if isinstance(payload, (bytes, bytearray, memoryview)):
        encoded = base64.b64encode(bytes(payload)).decode("ascii")
        message.contents.append(segments.Image.build(f"base64://{encoded}"))
    else:
        message.contents.append(segments.Image.build(str(payload)))
    if trailing_text:
        message.contents.append(segments.Text(trailing_text.lstrip("\r\n")))
    return message


def log_perf(message: str) -> None:
    try:
        logger.info(message)
    except Exception:
        logger.warning(message)


# ================================ 群开关与账本错误 ================================ #


def group_enabled(event: Any) -> bool:
    """未接入群开关时默认放行；私聊始终放行。"""

    group_id = get_event_group_id(event)
    if not group_id:
        return True
    return is_group_rollpig_enabled(group_id)


async def guard_store_errors(actions: Any, event: Any, error: Exception) -> bool:
    """把已知账本异常转成用户提示；返回 False 表示需要继续抛出。"""

    if isinstance(error, LocalStoreUnavailableError):
        logger.error(f"rollpig local store unavailable: {error}")
        await reply(
            actions,
            event,
            reply_to(
                getattr(event, "message_id", None),
                "猪圈本地账本读取失败，已进入写保护，请联系管理员恢复数据。",
            ),
        )
        return True
    if isinstance(error, CloudStoreError):
        logger.warning(f"rollpig cloud store unavailable: {error}")
        await reply(
            actions,
            event,
            reply_to(
                getattr(event, "message_id", None),
                "猪圈云账本暂时离线，请稍后再试。",
            ),
        )
        return True
    return False


# ================================ 烤群友目标 ================================ #


@dataclass(frozen=True)
class RoastTarget:
    """烤群友目标解析结果；target_id 为空表示用户没有指定目标。"""

    target_id: str | None
    target_name: str
    is_group_member: bool = False


class GroupMemberLookupError(RuntimeError):
    """当前群成员名单无法可靠读取。"""


async def _resolve_reply_sender(bot: Bot, message_id: str) -> tuple[str, str]:
    """读取被回复消息的发送者，供「引用 + 烤群友」定位目标。"""

    try:
        response = await bot.get_msg(message_id)
    except Exception as error:
        logger.debug(f"读取被回复消息失败: message={message_id} error={error}")
        return "", ""
    payload = getattr(response, "raw", response)
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if not isinstance(payload, dict):
        return "", ""
    sender = payload.get("sender")
    if not isinstance(sender, dict):
        return "", ""
    user_id = sender.get("user_id") or sender.get("uin") or ""
    name = sender.get("card") or sender.get("nickname") or ""
    return str(user_id or ""), str(name or "")


async def _member_payload(response: Any) -> dict[str, Any]:
    payload = getattr(response, "raw", response)
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    return payload if isinstance(payload, dict) else {}


async def resolve_roast_target(bot: Bot, event: Any) -> RoastTarget:
    """从回复、At 和 @自己 中解析烤群友目标。"""

    target_id: str | None = None
    target_name = "群友"
    is_group_member = False

    replied = event_replied_message_id(event)
    if replied:
        sender_id, sender_name = await _resolve_reply_sender(bot, replied)
        if sender_id:
            target_id = sender_id
            target_name = sender_name or target_name
    if not target_id:
        targets = event_at_targets(event)
        if targets:
            target_id = targets[0]
            target_name = "对方"
    if not target_id and bool(getattr(event, "is_mentioned", False)):
        target_id = event_self_id(event)

    group_id = get_event_group_id(event)
    if target_id and group_id:
        try:
            member = await bot.get_group_member_info(
                group_id=group_id, user_id=int(target_id)
            )
            payload = await _member_payload(member)
            if payload:
                target_name = str(
                    payload.get("card") or payload.get("nickname") or target_name
                )
                is_group_member = True
        except Exception as error:
            logger.debug(
                f"获取群成员信息失败: group={group_id} user={target_id} error={error}"
            )

    return RoastTarget(
        target_id=target_id,
        target_name=target_name,
        is_group_member=is_group_member,
    )


async def get_group_member_display_name(
    bot: Bot, group_id: Any, user_id: str, default: str = "群友"
) -> str:
    """按群名片优先获取成员展示名；接口失败时返回默认值。"""

    try:
        member = await bot.get_group_member_info(
            group_id=int(group_id), user_id=int(user_id)
        )
    except Exception:
        return default
    payload = await _member_payload(member)
    return str(payload.get("card") or payload.get("nickname") or default)


async def get_group_roll_candidates(
    bot: Bot, group_id: Any, exclude_ids: set[str]
) -> list[str]:
    """按当前群成员范围筛候选；名单不可用时拒绝使用可能已过期的记录。"""

    today = rollpig_date_str()
    today_rolls = await store.get_daily_rolls(today)

    try:
        response = await bot.get_group_member_list(group_id=int(group_id))
        payload = getattr(response, "raw", response)
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            members = payload["data"]
        else:
            members = payload
        if not isinstance(members, list):
            raise ValueError("get_group_member_list 返回格式无效")
        member_ids = {
            str(member.get("user_id") or member.get("uin"))
            for member in members
            if isinstance(member, dict) and (member.get("user_id") or member.get("uin"))
        }
        return [uid for uid in today_rolls if uid in member_ids and uid not in exclude_ids]
    except Exception as error:
        logger.debug(f"获取群成员列表失败: group={group_id} error={error}")
        raise GroupMemberLookupError(str(error)) from error


# ================================ 小猪卡片发送 ================================ #


async def send_rendered_pig(
    actions: Any,
    event: Any,
    pig_data: dict,
    extra_text: str = "",
    trailing_text: str = "",
    *,
    cache_final_card: bool = True,
    ex_level: int = 0,
    after_send: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """渲染并发送卡片；固定卡缓存成品，烤猪动态文案只复用 GIF 源帧。"""

    started_at = time.perf_counter()
    pig_id = str(pig_data.get("id", ""))
    appearance = pig_resource_manager.resolve_pig_appearance(pig_data, ex_level)
    render_data = appearance.pig_data
    avatar_file = appearance.image_path
    uses_variant_image = avatar_file is not None and avatar_file != appearance.base_image_path
    name = render_data.get("name", "未知小猪")
    variant_fallback = False
    payload_ready_at = time.perf_counter()
    message_id = getattr(event, "message_id", None)

    try:
        render_started_at = time.perf_counter()
        render_result = await render_pig_card_image(
            render_data,
            avatar_file,
            cache_final_card=cache_final_card,
            require_avatar=uses_variant_image,
        )
        render_finished_at = time.perf_counter()
    except Exception as error:
        if appearance.applied_level <= 0:
            logger.error(f"图片渲染失败: pig_id={pig_id}, renderer=pillow, error={error}")
            await reply(actions, event, reply_to(message_id, "图片生成失败。"))
            return

        # 差分是可选增强；文件损坏时回退基础资源，不能拖垮“今日小猪”本身。
        variant_fallback = True
        logger.warning(
            "rollpig EX 差分渲染失败，已回退基础资源: "
            f"pig_id={pig_id} requested={appearance.requested_level} "
            f"applied={appearance.applied_level} error={error}"
        )
        try:
            render_result = await render_pig_card_image(
                dict(pig_data),
                appearance.base_image_path,
                cache_final_card=cache_final_card,
                require_avatar=False,
            )
            render_finished_at = time.perf_counter()
        except Exception as fallback_error:
            logger.error(
                "图片渲染失败: "
                f"pig_id={pig_id}, variant_error={error}, fallback_error={fallback_error}"
            )
            await reply(actions, event, reply_to(message_id, "图片生成失败。"))
            return

    if variant_fallback:
        extra_text = ""

    body = image_message(render_result.data, extra_text, trailing_text)
    ready_to_send_at = time.perf_counter()

    log_perf(
        f"rollpig card rendered: renderer={render_result.renderer} "
        f"format={render_result.image_format} pig_id={pig_id} name={name} "
        f"image_found={avatar_file is not None} "
        f"payload={payload_ready_at - started_at:.2f}s "
        f"render={render_finished_at - render_started_at:.2f}s "
        f"message={ready_to_send_at - render_finished_at:.2f}s "
        f"total_before_send={ready_to_send_at - started_at:.2f}s "
        f"bytes={len(render_result.data)} "
        f"card_cache={'final-disk' if cache_final_card else 'dynamic'} "
        f"requested_ex={appearance.requested_level} applied_ex={appearance.applied_level} "
        f"variant_fallback={variant_fallback}"
    )

    await reply(actions, event, reply_to(message_id, body))
    if after_send is not None:
        # 首次抽猪需要严格保证“先展示今日卡，再发送预约结算”。
        await after_send()


async def finish_roast_outcome(
    actions: Any,
    event: Any,
    outcome: Any,
    *,
    attacker_id: str,
    attacker_name: str,
    target_id: str,
    target_name: str,
    group_id: str,
    daily_feed_eligible: bool = False,
) -> None:
    """落库并发送烤群友结果。"""

    roast_event = RoastEvent(
        event_type=outcome.event_type,
        attacker_id=attacker_id,
        target_id=target_id,
        attacker_name=attacker_name,
        target_name=target_name,
        food=outcome.food_name,
        group_id=group_id,
        event_id=uuid.uuid4().hex,
    )
    feed_result = await store.append_roast_event(
        roast_event,
        settle_daily_feed=(daily_feed_eligible and outcome.event_type == "success"),
    )
    feed_text = build_daily_feed_roast_text(feed_result)
    if outcome.render_data:
        # 烤猪分析文案随对象变化，缓存最终成品会迅速挤满，只复用 GIF 源帧。
        await send_rendered_pig(
            actions,
            event,
            outcome.render_data,
            extra_text=outcome.extra_text,
            trailing_text=feed_text,
            cache_final_card=False,
        )
        return
    text = outcome.plain_text
    if feed_text:
        text += "\n" + feed_text
    await reply(actions, event, reply_to(getattr(event, "message_id", None), text))


def build_daily_feed_roast_text(result: DailyFeedResult | None) -> str:
    """只为实际成长追加一行加餐结果。"""

    if result is None or result.status != "fed" or result.new_level <= result.previous_level:
        return ""
    pig = pig_resource_manager.pig_map.get(result.pig_id)
    pig_name = str(pig.get("name") or result.pig_id) if pig else result.pig_id
    return random.choice(DAILY_FEED_ROAST_TEXTS).format(
        pig=pig_name or "今日小猪",
        old_level=result.previous_level,
        new_level=result.new_level,
    )


__all__ = [
    "GroupMemberLookupError",
    "RoastTarget",
    "build_daily_feed_roast_text",
    "command_tail",
    "event_at_targets",
    "event_message",
    "event_plain_text",
    "event_replied_message_id",
    "event_self_id",
    "event_user_id",
    "finish_roast_outcome",
    "get_event_group_id",
    "get_event_user_name",
    "get_group_member_display_name",
    "get_group_roll_candidates",
    "group_enabled",
    "has_extra_text",
    "guard_store_errors",
    "image_message",
    "is_superuser_user",
    "log_perf",
    "reply",
    "reply_to",
    "resolve_roast_target",
    "send_rendered_pig",
]
