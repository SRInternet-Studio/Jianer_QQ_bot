"""抽猪、找猪与昨日/明日回顾命令（JianerCore 原生实现）。"""

from __future__ import annotations

import random
from typing import Any

from jianer import common, segments
from jianer.plugins.builtin.alconna import Command

from bot import plugin_state

from ...host import logger
from ..pighub_service import build_pighub_image_url, pighub_service
from ..reservation_flow import deliver_newly_ready_reservations
from ..resource_manager import PIG_LIST, sync_rollpig_resources
from ..roll_flow import (
    RECORDED_PIG_RESOURCE_MISSING_TEXT,
    ensure_yesterday_pig,
    resolve_daily_pig,
)
from ..runtime import rollpig_date_str
from ..store.cloud import CloudMakeupUnsupportedError
from ..texts import TOMORROW_TEXTS, YESTERDAY_MAKEUP_TEXTS
from ..yesterday_card_renderer import render_yesterday_recap_card
from ..yesterday_recap import (
    YesterdayPigResourceMissingError,
    build_yesterday_recap,
)
from ..helpers import (
    command_tail,
    get_event_group_id,
    guard_store_errors,
    has_extra_text,
    image_message,
    is_superuser_user,
    reply,
    reply_to,
    send_rendered_pig,
)

_REMINDER = str(plugin_state.get_runtime().get("reminder", ""))


def _build_pighub_image_message(pig: dict, image_url: str, fallback_title: str) -> common.Message:
    """把外部 PigHub 标题封装成纯文本消息，并追加已解析的图片段。"""

    title = str(pig.get("title") or fallback_title)
    return common.Message(
        segments.Text(title),
        segments.Image.build(image_url),
    )


def _pighub_node(pig: dict, image_url: str, fallback_title: str, uin: Any) -> dict:
    return {
        "type": "node",
        "data": {
            "name": "随机小猪Bot",
            "uin": uin,
            # PigHub 标题属于外部数据，必须作为纯文本，不能让其中的 CQ 码变成消息段。
            "content": _build_pighub_image_message(pig, image_url, fallback_title),
        },
    }


# 0. 小猪资源同步（管理员）


@Command(f"{_REMINDER}同步小猪资源").handle()
@Command(f"{_REMINDER}刷新小猪图鉴").handle()
async def cmd_sync_resources(event, actions, user_message=None):
    if has_extra_text(event, "同步小猪资源", "刷新小猪图鉴"):
        return False
    user_id = str(getattr(event, "user_id", ""))
    if not is_superuser_user(user_id):
        await reply(
            actions,
            event,
            reply_to(getattr(event, "message_id", None), "只有超级用户可以同步小猪资源。"),
        )
        return True

    try:
        message = await sync_rollpig_resources(force=True)
    except Exception as error:
        logger.error(f"rollpig 小猪资源手动同步失败: {error}")
        await reply(
            actions,
            event,
            reply_to(
                getattr(event, "message_id", None), f"小猪资源同步失败：{error}"
            ),
        )
        return True

    await reply(
        actions,
        event,
        reply_to(
            getattr(event, "message_id", None),
            "🐷 小猪资源同步结果\n"
            f"{message}\n\n"
            f"🐽 小猪数量：{len(PIG_LIST)}",
        ),
    )
    return True


# 1. 今日小猪


@Command(f"{_REMINDER}今天是什么小猪").handle()
@Command(f"{_REMINDER}今日小猪").handle()
async def cmd_today(event, actions, user_message=None):
    if has_extra_text(event, "今天是什么小猪", "今日小猪"):
        return False
    user_id = str(getattr(event, "user_id", ""))
    group_id = get_event_group_id(event)
    try:
        resolution = await resolve_daily_pig(user_id, group_id, include_progress=True)
    except Exception as error:
        if await guard_store_errors(actions, event, error):
            return True
        raise

    if resolution.recorded_pig_missing:
        await reply(
            actions,
            event,
            reply_to(getattr(event, "message_id", None), RECORDED_PIG_RESOURCE_MISSING_TEXT),
        )
        return True
    if resolution.missing_resources or not resolution.pig:
        await reply(
            actions,
            event,
            reply_to(getattr(event, "message_id", None), "猪圈塌房了（数据缺失）"),
        )
        return True

    await send_rendered_pig(
        actions,
        event,
        resolution.pig,
        extra_text=resolution.growth_text,
        ex_level=resolution.ex_level or 0,
        after_send=(
            (lambda: deliver_newly_ready_reservations(str(getattr(event, "self_id", ""))))
            if resolution.was_auto_created
            else None
        ),
    )
    return True


# 2. 随机小猪


async def _random_pig(actions, event, count: int) -> bool:
    if not await pighub_service.ensure_ready():
        await reply(
            actions, event, reply_to(getattr(event, "message_id", None), "连不上 PigHub，请稍后再试。")
        )
        return True

    selected = pighub_service.sample(count)
    if not selected:
        await reply(
            actions,
            event,
            reply_to(getattr(event, "message_id", None), "PigHub 图片索引为空，请稍后再试。"),
        )
        return True

    pig = selected[0]
    image_url = build_pighub_image_url(pig)
    if not image_url:
        await reply(
            actions,
            event,
            reply_to(
                getattr(event, "message_id", None), "PigHub 返回了异常图片数据，请稍后再试。"
            ),
        )
        return True

    group_id = get_event_group_id(event)
    if count == 1 or not group_id:
        if count > 1:
            message = _build_pighub_image_message(pig, image_url, "随机小猪")
            message += common.Message(segments.Text("\n私聊暂不支持多张连发，先给你一张："))
        else:
            message = _build_pighub_image_message(pig, image_url, "随机小猪")
        await reply(
            actions,
            event,
            reply_to(getattr(event, "message_id", None), message),
        )
        return True

    messages = []
    for item in selected:
        url = build_pighub_image_url(item)
        if not url:
            continue
        messages.append(
            _pighub_node(item, url, "随机小猪", getattr(event, "self_id", None))
        )
    if not messages:
        await reply(
            actions,
            event,
            reply_to(getattr(event, "message_id", None), "PigHub 图片数据异常，请稍后再试。"),
        )
        return True

    try:
        await actions.send_group_forward_msg(
            group_id=group_id,
            message=common.Message(*[_forward_node(entry) for entry in messages]),
        )
    except Exception as error:
        # OneBot 合并转发会让接入端预取远程图片；外部图源抖动时可能超时。
        logger.warning(f"随机小猪合并转发超时: {error}")
        await reply(
            actions,
            event,
            reply_to(
                getattr(event, "message_id", None),
                "PigHub 图片加载或合并转发超时了，请稍后再试。",
            ),
        )
    return True


def _forward_node(entry: dict) -> Any:
    from jianer import segments

    data = entry.get("data") or {}
    content = data.get("content")
    if isinstance(content, common.Message):
        node_content = content
    elif hasattr(content, "to_message"):
        node_content = content.to_message()
    else:
        node_content = common.Message(content)
    return segments.Node(
        str(data.get("uin") or ""),
        str(data.get("name") or ""),
        node_content,
    )


@Command(f"{_REMINDER}随机小猪").handle()
@Command(f"{_REMINDER}随机小猪 <count>").handle()
async def cmd_roll(event, actions, user_message=None, count: str = ""):
    tail = command_tail(event, "随机小猪") or str(count).strip()
    try:
        parsed = int(tail) if tail else 1
    except ValueError:
        parsed = 1
    return await _random_pig(actions, event, max(1, min(parsed, 10)))


# 2.5 找猪


@Command(f"{_REMINDER}找猪").handle()
@Command(f"{_REMINDER}找猪 <keyword>").handle()
@Command(f"{_REMINDER}搜猪").handle()
@Command(f"{_REMINDER}搜猪 <keyword>").handle()
async def cmd_find(event, actions, user_message=None, keyword: str = ""):
    if not await pighub_service.ensure_ready():
        await reply(
            actions, event, reply_to(getattr(event, "message_id", None), "连不上 PigHub，请稍后再试。")
        )
        return True

    keyword = command_tail(event, "找猪", "搜猪") or str(keyword).strip()
    if not keyword:
        await reply(
            actions,
            event,
            reply_to(getattr(event, "message_id", None), "请加上关键词，如：找猪 玩偶"),
        )
        return True

    found_pigs = pighub_service.search(keyword)
    if not found_pigs:
        await reply(
            actions,
            event,
            reply_to(getattr(event, "message_id", None), f"没找到叫「{keyword}」的猪。"),
        )
        return True

    group_id = get_event_group_id(event)
    if group_id:
        messages = []
        for pig in found_pigs[:10]:
            image_url = build_pighub_image_url(pig)
            if not image_url:
                continue
            messages.append(
                _pighub_node(pig, image_url, "未命名小猪", getattr(event, "self_id", None))
            )
        if not messages:
            await reply(
                actions,
                event,
                reply_to(getattr(event, "message_id", None), "搜索结果数据异常，请稍后再试。"),
            )
            return True
        try:
            await actions.send_group_forward_msg(
                group_id=group_id,
                message=common.Message(*[_forward_node(entry) for entry in messages]),
            )
        except Exception as error:
            logger.warning(f"找猪合并转发超时: keyword={keyword}, error={error}")
            await reply(
                actions,
                event,
                reply_to(
                    getattr(event, "message_id", None),
                    "PigHub 图片加载或合并转发超时了，请稍后再试。",
                ),
            )
        return True

    # 私聊降级：展示首条匹配
    pig = found_pigs[0]
    image_url = build_pighub_image_url(pig)
    if not image_url:
        await reply(
            actions,
            event,
            reply_to(getattr(event, "message_id", None), "搜索结果数据异常，请稍后再试。"),
        )
        return True
    message = _build_pighub_image_message(pig, image_url, "未命名小猪")
    if len(found_pigs) > 1:
        message += common.Message(
            segments.Text(f"\n共找到 {len(found_pigs)} 张，私聊仅展示第 1 张。")
        )
    await reply(
        actions, event, reply_to(getattr(event, "message_id", None), message)
    )
    return True


# 3. 明日小猪


@Command(f"{_REMINDER}明日小猪").handle()
async def cmd_tomorrow(event, actions, user_message=None):
    if has_extra_text(event, "明日小猪"):
        return False
    await reply(
        actions,
        event,
        reply_to(getattr(event, "message_id", None), random.choice(TOMORROW_TEXTS)),
    )
    return True


# 4. 昨日小猪


@Command(f"{_REMINDER}昨日小猪").handle()
async def cmd_yesterday(event, actions, user_message=None):
    if has_extra_text(event, "昨日小猪"):
        return False
    user_id = str(getattr(event, "user_id", ""))
    group_id = get_event_group_id(event)
    makeup_created = False
    try:
        target_date = rollpig_date_str(-1)
        recap = await build_yesterday_recap(user_id, group_id=group_id, date_str=target_date)
        if recap is None:
            if not PIG_LIST:
                await reply(
                    actions,
                    event,
                    reply_to(
                        getattr(event, "message_id", None),
                        "小猪资源暂时不可用，无法补签。",
                    ),
                )
                return True
            makeup_created = await ensure_yesterday_pig(user_id, target_date)
            recap = await build_yesterday_recap(
                user_id, group_id=group_id, date_str=target_date
            )
    except CloudMakeupUnsupportedError as error:
        await reply(
            actions, event, reply_to(getattr(event, "message_id", None), str(error))
        )
        return True
    except YesterdayPigResourceMissingError as error:
        logger.warning(f"rollpig 昨日身份无法解析: user={user_id} pig_id={error}")
        await reply(
            actions,
            event,
            reply_to(
                getattr(event, "message_id", None), "昨天那只猪暂时不在当前资源包里。"
            ),
        )
        return True
    except Exception as error:
        if await guard_store_errors(actions, event, error):
            return True
        raise

    if recap is None:
        await reply(
            actions,
            event,
            reply_to(
                getattr(event, "message_id", None), "补签结果暂时无法读取，请稍后再试。"
            ),
        )
        return True

    try:
        render_result = await render_yesterday_recap_card(recap)
    except Exception as error:
        logger.error(
            "rollpig 昨日回顾卡片生成失败: "
            f"user={user_id} pig_id={recap.roll.pig_id} error={error}"
        )
        await reply(
            actions,
            event,
            reply_to(
                getattr(event, "message_id", None), "昨日回顾卡生成失败，请稍后再试。"
            ),
        )
        return True

    logger.info(
        "rollpig yesterday card rendered: "
        f"user={user_id} pig_id={recap.roll.pig_id} "
        f"renderer={render_result.renderer} format={render_result.image_format} "
        f"size={render_result.width}x{render_result.height} "
        f"bytes={len(render_result.data)} image_fallback={render_result.used_fallback_image}"
    )
    message = common.Message()
    if makeup_created:
        message += common.Message(segments.Text(random.choice(YESTERDAY_MAKEUP_TEXTS)))
    message += image_message(render_result.data)
    await reply(
        actions,
        event,
        reply_to(getattr(event, "message_id", None), message),
    )
    return True


__all__ = [
    "cmd_find",
    "cmd_roll",
    "cmd_sync_resources",
    "cmd_today",
    "cmd_tomorrow",
    "cmd_yesterday",
]
