"""Rating, completion, plate, progress, and score-list tables."""

import re

from PIL import Image
from jianer.plugins.builtin.alconna import Command

from .. import adapter
from ..constants import COMBO_PLUS, LEVEL_LIST, PLATE_CN, RANK_PLUS, SYNC_PLUS
from ..core.handler import (
    draw_level_progress,
    draw_level_score_list,
    draw_plate_progress,
    draw_plate_table,
    draw_rating_table,
    draw_rating_table_text,
)
from ..core.image.tools import image_to_base64
from ..core.image.update_table import UpdateTable
from ..core.merge.models import Category
from ..message import MessageSegment
from ..resources import pic_dir
from .common import reject_unless_superuser, require_resources, require_user


RATING_PATTERN = re.compile(r"^([0-9]+\+?)((s+|ap|fc|fs|fdx)\+?)?\s?完成表$", re.IGNORECASE)
TABLE_PATTERN = re.compile(
    r"^([真超檄橙暁晓桃櫻樱紫菫堇白雪輝辉舞霸熊華华爽煌星宙祭祝双宴镜彩])"
    r"([極极将舞神者]舞?)(完成|进度)表?\s?([0-9]+)?$"
)
LEVEL_PATTERN = re.compile(
    r"^([0-9]+\+?)\s?((?:a+|b+|c|d|s+|ap|fc|fs|fdx)\+?)"
    r"\s?([\u4e00-\u9fa5]+)?\s?进度\s?([0-9]+)?$",
    re.IGNORECASE,
)
LEVEL_LIST_PATTERN = re.compile(r"^([0-9]+(?:\.[0-9]+)?\+?)\s?分数列表\s?([0-9]+)?$")
CATEGORY_ALIAS = {
    "已完成": Category.COMPLETED,
    "未完成": Category.UNFINISHED,
    "未开始": Category.NOTPLAYED,
    "未游玩": Category.NOTPLAYED,
}


@Command("更新定数表").handle()
async def handle_update_rating_table(event=None, actions=None):
    if await reject_unless_superuser(event, actions):
        return True
    if not await require_resources(event, actions):
        return True
    await adapter.send(event, actions, "正在更新定数表...")
    update = UpdateTable()
    await update.update_rating_table()
    await update.update_level_15_rating_table()
    await adapter.send(event, actions, "定数表更新完成。", reply=False)
    return True


@Command("更新完成表").handle()
async def handle_update_plate_table(event=None, actions=None):
    if await reject_unless_superuser(event, actions):
        return True
    if not await require_resources(event, actions):
        return True
    await adapter.send(event, actions, "正在更新完成表...")
    update = UpdateTable()
    await update.update_plate_table()
    await update.update_wu_plate_table()
    await adapter.send(event, actions, "完成表更新完成。", reply=False)
    return True


@Command("牌子条件").handle()
async def handle_plate_conditions(event=None, actions=None):
    if not await require_resources(event, actions):
        return True
    await adapter.send(
        event,
        actions,
        MessageSegment.image(image_to_base64(Image.open(pic_dir / "table_condition.jpg"))),
    )
    return True


async def _rating_table(event, actions, rating):
    if rating in LEVEL_LIST[:6]:
        result = "只支持查询lv7-15的定数表。"
    elif rating in LEVEL_LIST[6:]:
        result = draw_rating_table_text(rating)
    else:
        result = "无法识别的定数。"
    await adapter.send(event, actions, result)


async def _rating_completion(event, actions, match):
    user = await require_user(event, actions, check_auth=True)
    if user is None:
        return
    rating, plan = match.group(1), match.group(2)
    if rating in LEVEL_LIST[:6]:
        await adapter.send(event, actions, "只支持查询lv7-15的完成表。")
        return
    if rating not in LEVEL_LIST[6:]:
        await adapter.send(event, actions, "无法识别的定数。")
        return
    if plan and plan.lower() not in COMBO_PLUS:
        await adapter.send(
            event,
            actions,
            "完成表目前仅支持 fc、ap 计划，例如「13fc完成表」。",
        )
        return
    await adapter.send(
        event,
        actions,
        await draw_rating_table(user, rating, bool(plan and plan.lower() in COMBO_PLUS)),
    )


async def _plate_table(event, actions, match):
    user = await require_user(event, actions, check_auth=True)
    if user is None:
        return
    version, plan, mode = match.group(1), match.group(2), match.group(3)
    page = int(match.group(4) or 1)
    version = PLATE_CN.get(version, version)
    if f"{version}{plan}" == "真将":
        await adapter.send(event, actions, "真系没有真将哦。")
        return
    if mode == "完成":
        result = await draw_plate_table(user, version, plan, page)
    else:
        result = await draw_plate_progress(user, version, plan, page)
    await adapter.send(event, actions, result)


async def _level_progress(event, actions, match):
    user = await require_user(event, actions, check_auth=True)
    if user is None:
        return
    level, plan = match.group(1), match.group(2).lower()
    category_name, page = match.group(3), int(match.group(4) or 1)
    if level not in LEVEL_LIST:
        await adapter.send(event, actions, "无此等级。")
        return
    if plan not in RANK_PLUS + COMBO_PLUS + SYNC_PLUS:
        await adapter.send(event, actions, "无此评价等级。")
        return
    if LEVEL_LIST.index(level) < 11 or (plan in RANK_PLUS and RANK_PLUS.index(plan) < 8):
        await adapter.send(event, actions, "兄啊，有点志向好不好。")
        return
    if category_name:
        category = CATEGORY_ALIAS.get(category_name)
        if category is None:
            await adapter.send(event, actions, f"无法指定查询「{category_name}」。")
            return
    else:
        category = Category.DEFAULT
    await adapter.send(
        event,
        actions,
        await draw_level_progress(user, level, plan, category, page),
    )


async def _level_score_list(event, actions, match):
    user = await require_user(event, actions, check_auth=True)
    if user is None:
        return
    rating, page = match.group(1), int(match.group(2) or 1)
    if "." in rating:
        if not re.fullmatch(r"[0-9]+\.[0-9]", rating):
            await adapter.send(event, actions, "输入有误，定数仅有一位小数。")
            return
        rating = round(float(rating), 1)
    elif rating not in LEVEL_LIST:
        await adapter.send(event, actions, "无此等级。")
        return
    await adapter.send(event, actions, await draw_level_score_list(user, rating, page))


async def handle_table_patterns(event, actions):
    text = str(getattr(event, "msg_str", "") or "").strip()
    if not await _matches_any_table(text):
        return False
    if not await require_resources(event, actions):
        return True
    if match := re.search(r"([0-9]+\+?)定数表", text):
        await _rating_table(event, actions, match.group(1))
    elif match := RATING_PATTERN.fullmatch(text):
        await _rating_completion(event, actions, match)
    elif match := TABLE_PATTERN.fullmatch(text):
        await _plate_table(event, actions, match)
    elif match := LEVEL_PATTERN.fullmatch(text):
        await _level_progress(event, actions, match)
    elif match := LEVEL_LIST_PATTERN.fullmatch(text):
        await _level_score_list(event, actions, match)
    return True


async def _matches_any_table(text):
    return bool(re.search(r"([0-9]+\+?)定数表", text)) or any(
        pattern.fullmatch(text)
        for pattern in (
            RATING_PATTERN,
            TABLE_PATTERN,
            LEVEL_PATTERN,
            LEVEL_LIST_PATTERN,
        )
    )
