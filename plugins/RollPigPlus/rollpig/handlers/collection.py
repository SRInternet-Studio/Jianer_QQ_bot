"""猪圈收集与统计命令（JianerCore 原生实现）。"""

from __future__ import annotations

import datetime
import time

from jianer import common, segments
from jianer.plugins.builtin.alconna import Command

from bot import plugin_state

from ...host import logger
from ..card_renderer import render_weekly_pig_image
from ..catalog_renderer import render_catalog_image
from ..config import plugin_config
from ..helpers import (
    command_tail,
    get_event_user_name,
    guard_store_errors,
    has_extra_text,
    image_message,
    log_perf,
    reply,
    reply_to,
)
from ..resource_manager import PIG_LIST, find_image_file, get_pig_by_id
from ..roll_flow import build_pigsty_growth_summary
from ..runtime import rollpig_today
from ..store import store

_REMINDER = str(plugin_state.get_runtime().get("reminder", ""))


# 6. 我的猪圈


@Command(f"{_REMINDER}我的猪圈").handle()
@Command(f"{_REMINDER}我的小猪").handle()
async def cmd_pigsty(event, actions, user_message=None):
    if has_extra_text(event, "我的猪圈", "我的小猪"):
        return False
    user_id = str(getattr(event, "user_id", ""))
    try:
        draw_state = await store.get_draw_state(user_id)
    except Exception as error:
        if await guard_store_errors(actions, event, error):
            return True
        raise

    total_pigs = len(PIG_LIST)
    message_id = getattr(event, "message_id", None)
    if total_pigs <= 0:
        await reply(actions, event, reply_to(message_id, "猪图鉴为空，请先检查资源文件。"))
        return True
    if not draw_state.pig_ids:
        await reply(actions, event, reply_to(message_id, "你的猪圈空空如也！"))
        return True

    summary = build_pigsty_growth_summary(
        get_event_user_name(event),
        draw_state,
        total_pigs,
    )
    await reply(actions, event, reply_to(message_id, summary))
    return True


# 6.5 图片版小猪图鉴


@Command(f"{_REMINDER}小猪图鉴").handle()
@Command(f"{_REMINDER}小猪图鉴 <page>").handle()
@Command(f"{_REMINDER}猪猪图鉴").handle()
@Command(f"{_REMINDER}猪猪图鉴 <page>").handle()
@Command(f"{_REMINDER}完整图鉴").handle()
@Command(f"{_REMINDER}完整图鉴 <page>").handle()
async def cmd_catalog(event, actions, user_message=None, page: str = ""):
    message_id = getattr(event, "message_id", None)
    if not plugin_config.rollpig_catalog_enabled:
        await reply(actions, event, reply_to(message_id, "图片版小猪图鉴当前未启用。"))
        return True

    raw_arg = command_tail(event, "小猪图鉴", "猪猪图鉴", "完整图鉴") or str(page).strip()
    page_number = 1
    if raw_arg:
        try:
            page_number = max(1, int(raw_arg.split()[0]))
        except ValueError:
            await reply(
                actions,
                event,
                reply_to(message_id, "页码需要是数字，例如：小猪图鉴 2"),
            )
            return True

    user_id = str(getattr(event, "user_id", ""))
    if not PIG_LIST:
        await reply(actions, event, reply_to(message_id, "猪图鉴为空，请先检查资源文件。"))
        return True

    command_started_at = time.perf_counter()
    try:
        snapshot_started_at = time.perf_counter()
        snapshot = await store.get_catalog_snapshot(user_id, days=14)
        snapshot_ready_at = time.perf_counter()
    except Exception as error:
        if await guard_store_errors(actions, event, error):
            return True
        raise

    if not snapshot.draw_state.pig_ids:
        await reply(
            actions,
            event,
            reply_to(message_id, "你的猪圈空空如也！发送「今日小猪」开始收集。"),
        )
        return True

    try:
        pic = await render_catalog_image(
            user_name=get_event_user_name(event),
            snapshot=snapshot,
            page=page_number,
        )
        render_ready_at = time.perf_counter()
    except Exception as error:
        logger.error(f"小猪图鉴渲染失败: user={user_id} page={page_number} error={error}")
        await reply(actions, event, reply_to(message_id, "小猪图鉴生成失败，请稍后再试。"))
        return True

    log_perf(
        f"rollpig catalog command ready: user={user_id} page={page_number} "
        f"snapshot={snapshot_ready_at - snapshot_started_at:.2f}s "
        f"render={render_ready_at - snapshot_ready_at:.2f}s "
        f"total_before_send={render_ready_at - command_started_at:.2f}s "
        f"bytes={len(pic)}"
    )
    await reply(actions, event, reply_to(message_id, image_message(pic)))
    return True


# 6.6 小猪投稿入口


@Command(f"{_REMINDER}小猪投稿").handle()
@Command(f"{_REMINDER}投稿小猪").handle()
async def cmd_submit_pig(event, actions, user_message=None):
    if has_extra_text(event, "小猪投稿", "投稿小猪"):
        return False
    message = (
        "🐷 RollPig 小猪投稿\n"
        "将你的小猪创意送进猪圈吧！\n\n"
        "支持投稿：\n"
        "• 小猪创意\n"
        "• 完整小猪\n"
        "• EX 等级差分\n\n"
        "投稿地址：\n"
        "https://pig.felislab.cc/submit\n"
        "投稿完成后，记得保存投稿编号和私密查询码，方便随时查询审核进度。"
    )
    await reply(
        actions, event, reply_to(getattr(event, "message_id", None), message)
    )
    return True


# 7. 本周小猪


@Command(f"{_REMINDER}本周小猪").handle()
async def cmd_week(event, actions, user_message=None):
    if has_extra_text(event, "本周小猪"):
        return False
    user_id = str(getattr(event, "user_id", ""))
    message_id = getattr(event, "message_id", None)
    today = rollpig_today()

    images_to_merge = []
    try:
        for index in range(7):
            day = today - datetime.timedelta(days=(6 - index))
            pig = get_pig_by_id(await store.get_pig_by_date(user_id, day.isoformat()))
            if not pig:
                continue
            image_file = find_image_file(pig["id"])
            if image_file:
                images_to_merge.append(image_file)
    except Exception as error:
        if await guard_store_errors(actions, event, error):
            return True
        raise

    if not images_to_merge:
        await reply(actions, event, reply_to(message_id, "你这周还没抽过猪呢！"))
        return True

    try:
        image_data = await render_weekly_pig_image(images_to_merge)
    except Exception as error:
        logger.error(f"本周小猪长图生成失败: user={user_id}, error={error}")
        await reply(actions, event, reply_to(message_id, "生成图片失败。"))
        return True

    message = common.Message(
        segments.Text(f"你这周变了 {len(images_to_merge)} 次猪！"),
        *image_message(image_data).contents,
    )
    await reply(actions, event, reply_to(message_id, message))
    return True


__all__ = ["cmd_catalog", "cmd_pigsty", "cmd_submit_pig", "cmd_week"]
