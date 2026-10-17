from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional

from .resource_manager import pig_resource_manager
from .roast_manager import roast_manager
from .helpers import is_superuser_user
from .texts import (
    BACKFIRE_EATEN_TEXTS,
    BACKFIRE_FOOD_TEXTS,
    BACKFIRE_GENERIC_TEXTS,
    BACKFIRE_HUMAN_TEXTS,
    BACKFIRE_NO_PIG_TEXTS,
    BACKFIRE_SOLD_TEXTS,
    EATEN_PIG_ID,
    ESCAPE_TEXTS,
    FOOD_PIG_IDS,
    FORCE_ROAST_KEYWORDS,
    FORCE_ROAST_LIMIT_TEXTS,
    FORCE_ROAST_PREFIX_TEXTS,
    HUMAN_PIG_ID,
    SOLD_PIG_ID,
    SUPER_FORCE_ROAST_KEYWORD,
    SUPER_FORCE_ROAST_PREFIX_TEXTS,
    TARGET_EATEN_BLOCK_TEXTS,
    TARGET_FOOD_BLOCK_TEXTS,
    TARGET_HUMAN_BLOCK_TEXTS,
    TARGET_SOLD_BLOCK_TEXTS,
    TODAY_ROAST_EATEN_BLOCK_TEXTS,
    TODAY_ROAST_FOOD_BLOCK_TEXTS,
    TODAY_ROAST_HUMAN_BLOCK_TEXTS,
    TODAY_ROAST_SOLD_BLOCK_TEXTS,
    ROAST_RESERVATION_CREATED_TEXTS,
    ROAST_RESERVATION_DUPLICATE_TEXTS,
    ROAST_RESERVATION_FULL_TEXTS,
    ROAST_RESERVATION_JOINED_TEXTS,
)
from .store.models import RoastReservationPrepareResult


@dataclass(frozen=True)
class RoastOutcome:
    """烤群友一次判定后的结果；命令层只负责落库、发图或发纯文本。"""

    event_type: str
    render_data: Optional[dict] = None
    plain_text: str = ""
    extra_text: str = ""
    food_name: str = ""
    backfire_victim_id: str = ""
    backfire_victim_name: str = ""
    special_reason: str = ""


class RoastFoodMissingError(RuntimeError):
    """熟食资源缺失时抛出；由命令层转换成用户可读提示。"""


def pick_reservation_prepare_text(
    result: RoastReservationPrepareResult,
    *,
    attacker_name: str,
    target_name: str,
) -> str:
    """把预约准备状态转换为用户可见文案，handler 不直接拼业务文本。"""

    reservation = result.reservation
    if result.status == "reservation_closed":
        return "这场预约已经结束或过期，不能再加入了。"
    if result.status == "attacker_unrolled":
        return "先发送「今日小猪」领一只猪，再来加入预约。"
    if result.status == "self_target":
        return "这场预约烤的就是你，别往自己的烤架里添柴了。"
    if result.status == "reservation_created":
        return random.choice(ROAST_RESERVATION_CREATED_TEXTS).format(owner=attacker_name, target=target_name)
    if result.status == "reservation_joined":
        return random.choice(ROAST_RESERVATION_JOINED_TEXTS).format(
            participant=attacker_name,
            target=target_name,
            count=reservation.participant_count if reservation else 0,
        )
    if result.status == "already_joined":
        return random.choice(ROAST_RESERVATION_DUPLICATE_TEXTS).format(
            target=target_name,
            count=reservation.participant_count if reservation else 0,
        )
    if result.status == "reservation_full":
        return random.choice(ROAST_RESERVATION_FULL_TEXTS).format(
            target=target_name,
            count=reservation.participant_count if reservation else 0,
        )
    return "预约烤猪暂时没有响应，请稍后再试。"


# ================================ 烤猪形态规则 ================================ #
# “哪些猪能被烤/不能被烤”的基础判定。
# 资源包可以通过 pig_rules.json 扩展特殊形态，因此不要只依赖 texts.py 里的旧常量。


def get_food_pig_ids() -> list[str]:
    """合并内置熟食列表与云端 pig_rules.json；远端缺失时仍保持旧逻辑。"""
    return list(dict.fromkeys([*FOOD_PIG_IDS, *sorted(pig_resource_manager.food_pig_ids)]))


def get_human_pig_ids() -> list[str]:
    """合并内置人类形态与云端规则，允许后续资源包扩展同类特殊形态。"""
    return list(dict.fromkeys([HUMAN_PIG_ID, *sorted(pig_resource_manager.human_pig_ids)]))


def get_eaten_pig_ids() -> list[str]:
    """合并内置“吃掉了”形态与云端规则，避免特殊终态被新增资源绕过。"""
    return list(dict.fromkeys([EATEN_PIG_ID, *sorted(pig_resource_manager.eaten_pig_ids)]))


def get_sold_pig_ids() -> list[str]:
    """合并内置“卖掉了”形态与云端规则，让售罄类特殊形态走独立拦截文案。"""
    return list(dict.fromkeys([SOLD_PIG_ID, *sorted(pig_resource_manager.sold_pig_ids)]))


def is_food_pig(pig_data: Optional[dict]) -> bool:
    return bool(pig_data and pig_data.get("id") in get_food_pig_ids())


def is_human_pig(pig_data: Optional[dict]) -> bool:
    return bool(pig_data and pig_data.get("id") in get_human_pig_ids())


def is_eaten_pig(pig_data: Optional[dict]) -> bool:
    return bool(pig_data and pig_data.get("id") in get_eaten_pig_ids())


def is_sold_pig(pig_data: Optional[dict]) -> bool:
    return bool(pig_data and pig_data.get("id") in get_sold_pig_ids())


def can_backfire_roast(attacker_pig: Optional[dict]) -> bool:
    """判断反噬时攻击者是否还能被做成食物；特殊终态只走文字反噬，不二次加工。"""
    return bool(
        attacker_pig
        and not is_food_pig(attacker_pig)
        and not is_human_pig(attacker_pig)
        and not is_eaten_pig(attacker_pig)
        and not is_sold_pig(attacker_pig)
    )


def pick_food_pig() -> dict:
    """随机取一个熟食模板；资源包规则缺失时显式报错，避免 handler 里散落判空。"""
    food_ids = get_food_pig_ids()
    if not food_ids:
        raise RoastFoodMissingError("熟食规则为空，请检查 pig_rules.json。")

    food_id = random.choice(food_ids)
    food_pig = pig_resource_manager.pig_map.get(food_id)
    if not food_pig:
        raise RoastFoodMissingError("食材配置缺失，请检查 pig.json。")
    return food_pig


# ================================ 后门口令与烤猪文案 ================================ #
# 命令层只负责提取原始文本和用户 ID；本模块负责判断后门模式与生成对应提示。


def detect_force_roast_mode(raw_text: str, user_id: str) -> Optional[str]:
    normalized = raw_text.replace("/", "").replace(" ", "").replace("　", "")
    has_super_cmd = SUPER_FORCE_ROAST_KEYWORD in normalized
    has_force_cmd = any(k in normalized for k in FORCE_ROAST_KEYWORDS)

    if has_super_cmd:
        return "super" if is_superuser_user(user_id) else "super_denied"
    if has_force_cmd:
        return "normal"
    return None


def pick_backfire_text(attacker_name: str, target_name: str, attacker_pig: Optional[dict]) -> str:
    if not attacker_pig:
        pool = BACKFIRE_NO_PIG_TEXTS
        shape = "未抽形态"
    elif is_human_pig(attacker_pig):
        pool = BACKFIRE_HUMAN_TEXTS
        shape = "人类"
    elif is_eaten_pig(attacker_pig):
        pool = BACKFIRE_EATEN_TEXTS
        shape = "吃掉了"
    elif is_sold_pig(attacker_pig):
        pool = BACKFIRE_SOLD_TEXTS
        shape = "卖掉了"
    elif is_food_pig(attacker_pig):
        pool = BACKFIRE_FOOD_TEXTS
        shape = attacker_pig.get("name", "熟食")
    else:
        pool = BACKFIRE_GENERIC_TEXTS
        shape = attacker_pig.get("name", "未知形态")

    return random.choice(pool).format(attacker=attacker_name, target=target_name, shape=shape)


def clarify_backfire_roast_text(roast_text: str, attacker_name: str) -> str:
    """将反噬场景的第二段烧烤文案明确指向攻击者本人。"""
    normalized_text = (roast_text or "").strip()
    if not normalized_text:
        return normalized_text

    if attacker_name and attacker_name in normalized_text:
        return normalized_text

    attacker_label = f"【{attacker_name or '对方'}】"
    subject_replacements = (
        ("曾经你", f"曾经{attacker_label}"),
        ("如今你", f"如今{attacker_label}"),
        ("生前你", f"生前{attacker_label}"),
        ("原本你", f"原本{attacker_label}"),
        ("原来你", f"原来{attacker_label}"),
        ("你本是一只", f"{attacker_label}本是一只"),
        ("你本是", f"{attacker_label}本是"),
        ("你曾经是", f"{attacker_label}曾经是"),
        ("你曾是", f"{attacker_label}曾是"),
        ("你虽然", f"{attacker_label}虽然"),
        ("你从", f"{attacker_label}从"),
        ("看看你", f"看看{attacker_label}"),
        ("可怜的你", f"可怜的{attacker_label}"),
        ("没想到你", f"没想到{attacker_label}"),
    )
    for old_text, new_text in subject_replacements:
        if old_text in normalized_text:
            return normalized_text.replace(old_text, new_text, 1)

    if "你" in normalized_text:
        return normalized_text.replace("你", attacker_label, 1)

    return f"{attacker_label}原本想把别人送上烤架，结果最后被端上桌的却是自己。{normalized_text}"


def pick_escape_text(attacker_name: str, target_name: str, target_pig: Optional[dict]) -> str:
    shape = target_pig.get("name", "未知形态") if target_pig else "未知形态"
    return random.choice(ESCAPE_TEXTS).format(attacker=attacker_name, target=target_name, shape=shape)


def pick_force_prefix_text(target_name: str, is_super_mode: bool) -> str:
    pool = SUPER_FORCE_ROAST_PREFIX_TEXTS if is_super_mode else FORCE_ROAST_PREFIX_TEXTS
    return random.choice(pool).format(target=target_name)


def pick_force_limit_text(operator_name: str, target_name: str) -> str:
    return random.choice(FORCE_ROAST_LIMIT_TEXTS).format(operator=operator_name, target=target_name)


def format_cooldown_message(remaining_seconds: int) -> str:
    remaining = max(0, int(remaining_seconds))
    minutes, seconds = divmod(remaining, 60)
    hours, minutes = divmod(minutes, 60)
    time_str = f"{hours}小时{minutes}分" if hours > 0 else f"{minutes}分{seconds}秒"
    return (
        f"烧烤充能恢复中！还需要 {time_str} 恢复 1 次。\n"
        "等不及了？发送「烤箱续火」，喊群友一起添把火。"
    )


# ================================ 烤猪拦截文案 ================================ #
# “不能被烤”的特殊形态在今日烤猪、指定烤群友、随机烤群友里都会出现。
# 集中在 flow 层能避免 handler 里堆重复 if，也保证后续新增特殊形态时口径一致。


def pick_self_roast_block_text(pig_data: Optional[dict]) -> Optional[str]:
    """返回“今日烤猪”的特殊形态拦截文案；可正常烧烤时返回 None。"""

    if is_human_pig(pig_data):
        return random.choice(TODAY_ROAST_HUMAN_BLOCK_TEXTS)
    if is_eaten_pig(pig_data):
        return random.choice(TODAY_ROAST_EATEN_BLOCK_TEXTS)
    if is_sold_pig(pig_data):
        return random.choice(TODAY_ROAST_SOLD_BLOCK_TEXTS)
    if is_food_pig(pig_data):
        shape = pig_data.get("name", "熟食") if pig_data else "熟食"
        return random.choice(TODAY_ROAST_FOOD_BLOCK_TEXTS).format(shape=shape)
    return None


def pick_member_target_block_text(target_name: str, target_pig: Optional[dict]) -> Optional[str]:
    """返回“烤群友”的目标特殊形态拦截文案；可正常烧烤时返回 None。"""

    if is_human_pig(target_pig):
        return random.choice(TARGET_HUMAN_BLOCK_TEXTS).format(target=target_name)
    if is_eaten_pig(target_pig):
        return random.choice(TARGET_EATEN_BLOCK_TEXTS).format(target=target_name)
    if is_sold_pig(target_pig):
        return random.choice(TARGET_SOLD_BLOCK_TEXTS).format(target=target_name)
    if is_food_pig(target_pig):
        shape = target_pig.get("name", "熟食") if target_pig else "熟食"
        return random.choice(TARGET_FOOD_BLOCK_TEXTS).format(target=target_name, shape=shape)
    return None


def pick_random_target_block_text(target_name: str, target_pig: Optional[dict]) -> Optional[str]:
    """返回“随机烤群友”的目标特殊形态拦截文案；保留随机命令原有的系统提示口吻。"""

    if is_human_pig(target_pig):
        return f"系统随机选中了【{target_name}】，但对方是人类形态，烤架拒绝处理。换一次试试？"
    if is_eaten_pig(target_pig):
        return random.choice(TARGET_EATEN_BLOCK_TEXTS).format(target=target_name)
    if is_sold_pig(target_pig):
        return random.choice(TARGET_SOLD_BLOCK_TEXTS).format(target=target_name)
    if is_food_pig(target_pig):
        shape = target_pig.get("name", "熟食") if target_pig else "熟食"
        return f"系统随机选中了【{target_name}】，但对方已经是【{shape}】了，别鞭尸了。"
    return None


def format_roast_outcome_log(
    scene: str,
    *,
    attacker_name: str,
    attacker_id: str,
    target_name: str,
    target_id: str,
    outcome: RoastOutcome,
    force_mode: Optional[str] = None,
) -> str:
    """按场景格式化烤群友结果日志，避免多个 handler 分支复制同一套状态文案。"""

    if outcome.event_type == "success":
        log_mode = "后门成功" if force_mode in {"normal", "super"} else "成功"
        mode_suffix = f" 模式={force_mode}" if force_mode in {"normal", "super"} else ""
        return (
            f"[{scene}] {log_mode} | 凶手={attacker_name}({attacker_id}) "
            f"目标={target_name}({target_id}){mode_suffix} 结果={outcome.food_name}"
        )
    if outcome.event_type == "escape":
        return f"[{scene}] 逃脱 | 凶手={attacker_name}({attacker_id}) 目标={target_name}({target_id})"
    if outcome.render_data:
        return (
            f"[{scene}] 反噬 | 凶手={attacker_name}({attacker_id}) "
            f"目标={target_name}({target_id}) 凶手变成={outcome.food_name}"
        )
    return f"[{scene}] 反噬(文字) | 凶手={attacker_name}({attacker_id}) 目标={target_name}({target_id})"


# ================================ 烤群友结果构造 ================================ #
# 概率、食材选择、AI 文案生成和反噬文本拼接集中在这里。
# handler 仍然负责 cooldown / protection / store.append_roast_event / 发送消息，避免 P1 阶段
# 把 NoneBot matcher、存储副作用和业务判定一次性搅在一起。


async def build_self_roast_data(original_pig: dict) -> tuple[dict, str]:
    """构造“今日烤猪”的熟食卡片数据，返回渲染用数据和熟食名称。"""
    food_pig = pick_food_pig()
    roast_text = await roast_manager.get_roast_text(original_pig, food_pig)
    roasted_data = food_pig.copy()
    roasted_data["analysis"] = roast_text
    return roasted_data, food_pig["name"]


async def build_success_roast_outcome(
    target_pig: dict,
    *,
    attacker_name: str,
    target_name: str,
    extra_text: str = "",
) -> RoastOutcome:
    """构造成功烤群友结果；普通成功、随机成功和后门成功共用这条路径。"""
    food_pig = pick_food_pig()
    text = await roast_manager.get_roast_text(
        target_pig,
        food_pig,
        operator_name=attacker_name,
        target_name=target_name,
    )
    roasted_data = food_pig.copy()
    roasted_data["analysis"] = text
    return RoastOutcome(
        event_type="success",
        render_data=roasted_data,
        extra_text=extra_text,
        food_name=food_pig["name"],
    )


async def build_backfire_roast_outcome(
    attacker_pig: Optional[dict],
    *,
    attacker_name: str,
    target_name: str,
    extra_text: str = "",
) -> RoastOutcome:
    """构造反噬结果；攻击者形态可烤时发图，否则只发反噬纯文本。"""
    fail_intro = pick_backfire_text(attacker_name, target_name, attacker_pig)
    if not can_backfire_roast(attacker_pig):
        return RoastOutcome(
            event_type="backfire",
            plain_text=extra_text + fail_intro,
        )

    food_pig = pick_food_pig()
    text = await roast_manager.get_roast_text(attacker_pig, food_pig)
    text = clarify_backfire_roast_text(text, attacker_name)
    roasted_data = food_pig.copy()
    roasted_data["analysis"] = fail_intro + "\n\n" + text
    return RoastOutcome(
        event_type="backfire",
        render_data=roasted_data,
        extra_text=extra_text,
        food_name=food_pig["name"],
    )


async def build_member_roast_outcome(
    *,
    attacker_pig: Optional[dict],
    target_pig: dict,
    attacker_name: str,
    target_name: str,
    force_mode: Optional[str] = None,
    intro_text: str = "",
) -> RoastOutcome:
    """执行烤群友核心判定，返回成功/逃脱/反噬之一。"""
    if force_mode in {"normal", "super"}:
        prefix_text = pick_force_prefix_text(target_name, is_super_mode=(force_mode == "super"))
        return await build_success_roast_outcome(
            target_pig,
            attacker_name=attacker_name,
            target_name=target_name,
            extra_text=prefix_text,
        )

    roll = random.randint(1, 100)
    if roll <= 60:
        return await build_success_roast_outcome(
            target_pig,
            attacker_name=attacker_name,
            target_name=target_name,
            extra_text=intro_text,
        )
    if roll <= 90:
        return RoastOutcome(
            event_type="escape",
            plain_text=intro_text + pick_escape_text(attacker_name, target_name, target_pig),
        )
    return await build_backfire_roast_outcome(
        attacker_pig,
        attacker_name=attacker_name,
        target_name=target_name,
        extra_text=intro_text,
    )
