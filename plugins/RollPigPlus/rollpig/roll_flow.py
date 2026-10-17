from __future__ import annotations

import random
from dataclasses import dataclass, replace

from ..host import logger

from .resource_manager import pig_resource_manager
from .store import store
from .store.models import (
    MAX_EXPERT_LEVEL,
    DailyRollResult,
    DailyRollSnapshot,
    DrawState,
    expert_level_from_copies,
)
from .texts import (
    DAILY_ROLL_DUPLICATE_LEVEL_UP_TEXTS,
    DAILY_ROLL_DUPLICATE_SAME_LEVEL_TEXTS,
    DAILY_ROLL_NEW_PIG_TEXTS,
    DAILY_ROLL_VARIANT_LEVEL_UP_TEXTS,
)


DUPLICATE_PITY_WEIGHT_STEP = 0.5
DUPLICATE_PITY_WEIGHT_CAP = 4.0
RECORDED_PIG_RESOURCE_MISSING_TEXT = (
    "你的今日小猪已经抽出来了，但当前 Bot 的小猪资源暂时缺失，请稍后再试。"
)


@dataclass(frozen=True)
class DailyPigResolution:
    """今日形态解析结果；命令层用它决定发卡片、提示成长，或提示资源缺失。"""

    pig: dict | None
    roll_result: DailyRollResult | None = None
    growth_text: str = ""
    missing_resources: bool = False
    recorded_pig_missing: bool = False
    ex_level: int | None = None

    @property
    def was_auto_created(self) -> bool:
        """本次命令是否顺手创建了今日抽猪记录。"""

        return bool(self.roll_result and self.roll_result.created)


# ================================ 今日小猪成长流程 ================================ #
# 集中处理“今日小猪”与图鉴成长相关的纯业务规则。


def resolve_variant_unlocks(
    pig_id: str,
    previous_level: int,
    current_level: int,
) -> tuple[tuple[int, ...], frozenset[str]]:
    """返回本次真正解锁的差分等级与精确字段，供即时提示和历史快照共用。"""

    candidate_levels = pig_resource_manager.newly_unlocked_variant_levels(
        pig_id,
        previous_level,
        current_level,
    )
    levels: list[int] = []
    fields: set[str] = set()
    for level in candidate_levels:
        level_fields = pig_resource_manager.variant_snapshot_fields(pig_id, level)
        if not level_fields:
            continue
        levels.append(level)
        fields.update(level_fields)
    return tuple(levels), frozenset(fields)


def build_completed_daily_roll_snapshot(
    result: DailyRollResult,
    pig_data: dict,
) -> DailyRollSnapshot | None:
    """用当前已激活资源补全首次抽取快照，不对旧记录或当前进度做历史倒推。"""

    snapshot = result.snapshot
    if snapshot is None or not snapshot.outcome_available:
        return None

    previous_level = (
        int(snapshot.previous_expert_level)
        if snapshot.previous_expert_level is not None
        else expert_level_from_copies(snapshot.previous_copies or 0)
    )
    current_level = (
        int(snapshot.expert_level_after_roll)
        if snapshot.expert_level_after_roll is not None
        else expert_level_from_copies(snapshot.copies_after_roll or 0)
    )
    pig_id = str(pig_data.get("id") or snapshot.pig_id)
    unlocked_levels, unlocked_fields = resolve_variant_unlocks(
        pig_id,
        previous_level,
        current_level,
    )
    appearance = pig_resource_manager.resolve_pig_appearance(pig_data, current_level)
    resolved_level = appearance.applied_level
    resolved_image = appearance.image_path
    if (
        resolved_image is not None
        and resolved_image != appearance.base_image_path
        and not pig_resource_manager.image_file_is_decodable(resolved_image)
    ):
        # 差分图片在同步后被替换或损坏时，普通卡会整卡回退基础资源；
        # 历史快照必须记录相同结果，不能继续宣称新立绘或新介绍已生效。
        resolved_level = 0
        resolved_image = appearance.base_image_path
        unlocked_levels = ()
        unlocked_fields = frozenset()
    return replace(
        snapshot,
        resource_version=appearance.resource_version or "builtin",
        resolved_variant_level=resolved_level,
        resolved_image_name=resolved_image.name if resolved_image else "",
        unlocked_variant_levels=unlocked_levels,
        unlocked_variant_fields=unlocked_fields,
    )


async def _complete_daily_roll_snapshot(
    user_id: str,
    result: DailyRollResult,
    pig_data: dict,
) -> DailyRollResult:
    """补全失败只损失昨日成长细节，不能阻断今日抽猪与预约激活。"""

    base_snapshot = result.snapshot
    if base_snapshot is None or base_snapshot.resource_version:
        return result
    try:
        # 资源解析、图片首帧检查和 Store 写入都属于可选补全；任一环节异常
        # 都必须与今日抽猪主流程隔离。
        snapshot = build_completed_daily_roll_snapshot(result, pig_data)
        if snapshot is None:
            return result
        await store.complete_daily_roll_snapshot(user_id, snapshot)
    except Exception as error:
        logger.warning(
            "rollpig 每日抽取资源快照补全失败，已保留基础抽取结果: "
            f"date={base_snapshot.date_str} user={user_id} "
            f"pig_id={base_snapshot.pig_id} error={error}"
        )
        return result
    return replace(result, snapshot=snapshot)


async def pick_daily_roll_candidate(user_id: str) -> dict:
    """按用户当前图鉴状态选择今日候选猪；连续重复越多，新猪权重越高。"""
    pig_list = pig_resource_manager.pig_list
    draw_state = await store.get_draw_state(user_id)
    owned_pig_ids = set(draw_state.pig_ids)
    duplicate_streak = max(0, int(draw_state.duplicate_streak or 0))
    new_pig_bonus = min(duplicate_streak * DUPLICATE_PITY_WEIGHT_STEP, DUPLICATE_PITY_WEIGHT_CAP)

    weights = []
    for pig in pig_list:
        pig_id = str(pig.get("id", ""))
        is_unowned = pig_id and pig_id not in owned_pig_ids
        weights.append(1.0 + new_pig_bonus if is_unowned else 1.0)

    # random.choices 比手写累计权重更不容易写出边界错误；pig_list 为空时调用方已拦截。
    return random.choices(pig_list, weights=weights, k=1)[0]


async def ensure_yesterday_pig(user_id: str, date_str: str) -> bool:
    """补抽缺失的昨日记录；候选由 Bot 选择，后端原子决定最终结果。"""
    if await store.get_daily_roll(user_id, date_str=date_str):
        return False
    if not pig_resource_manager.pig_list:
        raise RuntimeError("小猪资源暂时不可用，无法补签")
    candidate = await pick_daily_roll_candidate(user_id)
    result = await store.get_or_create_daily_roll(
        user_id, candidate["id"], date_str=date_str, makeup=True,
    )
    pig = pig_resource_manager.pig_map.get(result.pig_id)
    if pig is not None:
        await _complete_daily_roll_snapshot(user_id, result, pig)
    return result.created


def build_roll_growth_text(result: DailyRollResult, pig_data: dict) -> str:
    """生成今日首次抽猪后的成长提示；重复查看当天结果时不刷提示也不刷等级。"""
    if not result.created:
        return ""

    pig_name = pig_data.get("name", "未知小猪")
    current_level = (
        result.expert_level
        if result.expert_level is not None
        else expert_level_from_copies(result.copies)
    )
    if result.is_new_pig:
        return random.choice(DAILY_ROLL_NEW_PIG_TEXTS).format(pig=pig_name, level=current_level)

    previous_level = (
        result.previous_expert_level
        if result.previous_expert_level is not None
        else expert_level_from_copies(result.previous_copies)
    )
    if previous_level == current_level:
        return random.choice(DAILY_ROLL_DUPLICATE_SAME_LEVEL_TEXTS).format(
            pig=pig_name,
            level=current_level,
        )

    unlocked_levels, precise_fields = resolve_variant_unlocks(
        str(pig_data.get("id") or ""),
        previous_level,
        current_level,
    )
    if unlocked_levels:
        # 一次升级可能跨过多个稀疏档位；合并本次真实变化后只发送一条完整提示，
        # 避免先说普通升级、再追加“解锁图片/文案”的重复表达。
        changed_fields = frozenset({"image"} if "image" in precise_fields else set())
        if precise_fields & {"description", "analysis"}:
            changed_fields = changed_fields | {"text"}
        pool_key = {
            frozenset({"image"}): "image",
            frozenset({"text"}): "text",
            frozenset({"image", "text"}): "image_text",
        }.get(changed_fields)
        if pool_key is not None:
            return random.choice(DAILY_ROLL_VARIANT_LEVEL_UP_TEXTS[pool_key]).format(
                pig=pig_name,
                old_level=previous_level,
                new_level=current_level,
            )

    return random.choice(DAILY_ROLL_DUPLICATE_LEVEL_UP_TEXTS).format(
        pig=pig_name,
        old_level=previous_level,
        new_level=current_level,
    )


def _current_daily_card_expert_level(result: DailyRollResult) -> int:
    """返回今日卡片应展示的最终等级，同时保留抽取快照的历史语义。"""

    level = (
        result.expert_level
        if result.expert_level is not None
        else expert_level_from_copies(result.copies)
    )
    feed = result.snapshot.daily_feed_result if result.snapshot is not None else None
    # get 与 get-or-create 之间可能被另一 Bot 抢先完成抽取和加餐；此时响应中的
    # expert_level 仍是抽取时快照，当前卡片应采用同一只猪已结算的加餐等级。
    if feed is not None and feed.status == "fed" and feed.pig_id == result.pig_id:
        level = max(level, int(feed.new_level))
    return min(max(level, 0), MAX_EXPERT_LEVEL)


async def resolve_daily_pig(
    user_id: str,
    group_id: str = "",
    *,
    include_progress: bool = False,
) -> DailyPigResolution:
    """
    取得用户今日形态；没有记录时自动抽取并更新图鉴进度。

    今日小猪与今日烤猪都依赖这条流程。集中处理后可以保证：
    1. 重复查看不会重复增加图鉴进度；
    2. 群内可见记录统一写入；
    3. 资源缺失时由命令层按各自口吻提示。
    """

    pig_id = await store.get_daily_roll(user_id)
    current_pig = pig_resource_manager.pig_map.get(pig_id) if pig_id else None
    if current_pig:
        if group_id:
            await store.mark_group_roll_seen(user_id, current_pig["id"], group_id)
        ex_level: int | None = None
        if include_progress:
            draw_state = await store.get_draw_state(user_id)
            ex_level = draw_state.expert_level_of(current_pig["id"])
        return DailyPigResolution(pig=current_pig, ex_level=ex_level)

    if pig_id:
        # 已保存的 ID 缺失时绝不能用随机候选替代，否则展示结果会与账本永久不一致。
        # 具体 ID 只进入管理员日志；命令层用独立标记解释为“已抽出但本机缺资源”。
        logger.warning(f"RollPig 今日形态资源缺失: user={user_id} pig_id={pig_id}")
        return DailyPigResolution(
            pig=None,
            missing_resources=True,
            recorded_pig_missing=True,
        )

    if not pig_resource_manager.pig_list:
        return DailyPigResolution(pig=None, missing_resources=True)

    proposed_pig = await pick_daily_roll_candidate(user_id)
    roll_result = await store.get_or_create_daily_roll(
        user_id,
        proposed_pig["id"],
        group_id=group_id,
    )
    pig = pig_resource_manager.pig_map.get(roll_result.pig_id)
    if pig is None:
        # 多 Bot 并发时 Cloud 可能返回另一实例抢先写入的猪。当前实例尚未同步
        # 该资源时必须停止展示与快照补全，不能拿本地候选猪冒充 Cloud 赢家。
        logger.warning(
            "RollPig Cloud 抽取结果资源缺失: "
            f"user={user_id} pig_id={roll_result.pig_id} "
            f"proposed_pig_id={proposed_pig['id']}"
        )
        return DailyPigResolution(
            pig=None,
            missing_resources=True,
            recorded_pig_missing=True,
        )
    roll_result = await _complete_daily_roll_snapshot(user_id, roll_result, pig)
    return DailyPigResolution(
        pig=pig,
        roll_result=roll_result,
        growth_text=build_roll_growth_text(roll_result, pig) if include_progress else "",
        ex_level=_current_daily_card_expert_level(roll_result) if include_progress else None,
    )


def build_pigsty_growth_summary(user_name: str, draw_state: DrawState, total_pigs: int) -> str:
    """生成文本版猪圈摘要；图片版图鉴由“小猪图鉴”命令独立提供。"""
    user_count = len(draw_state.pig_ids)
    percent = int((user_count / total_pigs) * 100) if total_pigs > 0 else 0

    ranked_progress = sorted(
        draw_state.progress.items(),
        key=lambda item: (-item[1].copies, item[1].first_obtained_at or "", item[0]),
    )
    favorite_line = "🐷 本命猪：暂无"
    top_repeat_line = "⭐ 高等级小猪：暂无重复猪，猪圈还很清新"
    max_level = 0
    maxed_count = 0
    if ranked_progress:
        levels = [progress.expert_level for _, progress in ranked_progress]
        max_level = max(levels)
        maxed_count = sum(1 for level in levels if level >= MAX_EXPERT_LEVEL)

        favorite_id, favorite_progress = ranked_progress[0]
        favorite = pig_resource_manager.pig_map.get(favorite_id)
        favorite_name = favorite.get("name", favorite_id) if favorite else favorite_id
        favorite_level = favorite_progress.expert_level
        favorite_line = f"🐷 本命猪：【{favorite_name}】EX Lv.{favorite_level}（累计 {favorite_progress.copies} 次）"

        # 加餐不增加 copies；高等级榜独立按 EX 排序，本命猪仍按真实抽取次数。
        repeat_items = sorted(
            ((pig_id, progress) for pig_id, progress in ranked_progress if progress.expert_level >= 1),
            key=lambda item: (
                -item[1].expert_level, -item[1].copies,
                item[1].first_obtained_at or "", item[0],
            ),
        )[:5]
        if repeat_items:
            parts = []
            for pig_id, progress in repeat_items:
                pig = pig_resource_manager.pig_map.get(pig_id)
                pig_name = pig.get("name", pig_id) if pig else pig_id
                parts.append(f"【{pig_name}】EX Lv.{progress.expert_level}")
            top_repeat_line = "⭐ 高等级小猪：" + "、".join(parts)

    if draw_state.duplicate_streak > 0:
        streak_line = f"🔥 连续重复：{draw_state.duplicate_streak} 次（新猪气息正在靠近）"
    else:
        streak_line = "🔥 连续重复：0 次（下一只从平常心开始）"

    footer_line = "发送「今日小猪」开始收集。" if user_count <= 0 else "发送「小猪图鉴」查看图片版完整图鉴。"

    return (
        f"【我的猪圈统计】\n"
        f"👑 猪圈主人：{user_name}\n"
        f"📦 已收集：{user_count} / {total_pigs} 只\n"
        f"📈 收藏率：{percent}%\n"
        f"🏅 最高等级：EX Lv. {max_level}｜满级 {maxed_count} 只\n"
        f"{favorite_line}\n"
        f"{top_repeat_line}\n"
        f"{streak_line}\n"
        f"━━━━━━━━━━━━━━\n"
        f"{footer_line}\n"
        "💡 有新的小猪创意？发送「小猪投稿」把它送进猪圈。"
    )
