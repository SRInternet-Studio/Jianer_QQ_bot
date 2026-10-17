from __future__ import annotations

import asyncio
import datetime
import json
from typing import Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..host import logger
from ..host import localstore

from .config import plugin_config


# ================================ 日期边界 ================================ #
# RollPig 的“今天/明天/近 7 天”都按中国用户习惯使用 Asia/Shanghai。
# 不直接调用 date.today()，避免服务器部署在 UTC 或其他时区时跨日边界漂移。
try:
    ROLLPIG_TIMEZONE = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    # Windows 精简环境可能缺少 IANA tzdata；中国业务日期当前无夏令时，
    # 固定 UTC+8 兜底可以避免插件在导入阶段直接失败。
    ROLLPIG_TIMEZONE = datetime.timezone(datetime.timedelta(hours=8), "Asia/Shanghai")


def rollpig_now() -> datetime.datetime:
    """返回 RollPig 业务时区下的当前时间。"""
    return datetime.datetime.now(ROLLPIG_TIMEZONE)


def rollpig_today() -> datetime.date:
    """返回 RollPig 业务时区下的今天日期。"""
    return rollpig_now().date()


def rollpig_date_str(offset_days: int = 0) -> str:
    """返回 RollPig 业务日期字符串；offset_days 用于昨日/明日等相对日期。"""
    return (rollpig_today() + datetime.timedelta(days=offset_days)).isoformat()


# ================================ 外部群开关适配 ================================ #
# 这里仅暴露“可选群启用检查器”接口，宿主项目若有控制台/控制面，
# 可以把自己的群开关逻辑挂进来；没有则保持默认放行。
_group_enable_checker: Optional[Callable[[str], bool]] = None
_daily_report_checker: Optional[Callable[[str], bool]] = None


# ================================ 内置日报群控制器 ================================ #
# rollpig_daily_summary_enabled 只作为“未显式设置的群”的默认值。
# 若宿主项目注册了外部日报控制器，命令会直接读写外部控制器；否则使用本地 localstore。
# 沿用旧文件名，避免升级后丢失既有群开关；代码侧统一使用 daily_report 命名。
_DAILY_REPORT_SWITCH_FILE = localstore.get_plugin_data_file("daily_summary_groups.json")
_daily_report_switch_lock = asyncio.Lock()
_daily_report_enabled_groups: set[str] = set()
_daily_report_disabled_groups: set[str] = set()
_daily_report_setter: Optional[Callable[[str, bool], object]] = None
_daily_report_source_name = "插件本地控制"


def _normalize_group_id(group_id: str | int | None) -> str:
    """统一群号格式；空值返回空字符串，便于调用方判定无效输入。"""

    return str(group_id or "").strip()


def _coerce_group_id_set(value: object) -> set[str]:
    """从 JSON 字段恢复群号集合；忽略空值和非法容器，避免坏配置拖垮插件。"""

    if not isinstance(value, list):
        return set()
    return {group_id for item in value if (group_id := _normalize_group_id(item))}


def _load_daily_report_switches() -> None:
    """启动时读取公开版内置日报群开关；读取失败时按空覆盖表处理。"""

    global _daily_report_enabled_groups, _daily_report_disabled_groups
    if not _DAILY_REPORT_SWITCH_FILE.exists():
        return

    try:
        data = json.loads(_DAILY_REPORT_SWITCH_FILE.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("daily_summary_groups.json 顶层必须是 object")
    except Exception as error:
        logger.warning(f"读取日报群开关失败，已按空配置处理: {_DAILY_REPORT_SWITCH_FILE}: {error}")
        return

    _daily_report_enabled_groups = _coerce_group_id_set(data.get("enabled_group_ids"))
    _daily_report_disabled_groups = _coerce_group_id_set(data.get("disabled_group_ids"))
    # 同一群如果因为手工改文件同时出现在两边，开启优先，避免“开了却不生效”的困惑。
    _daily_report_disabled_groups.difference_update(_daily_report_enabled_groups)


def _save_daily_report_switches_sync() -> None:
    """原子写入公开版本地日报群开关文件；写一半崩溃时不会破坏旧文件。"""

    _DAILY_REPORT_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "enabled_group_ids": sorted(_daily_report_enabled_groups),
        "disabled_group_ids": sorted(_daily_report_disabled_groups),
    }
    tmp_file = _DAILY_REPORT_SWITCH_FILE.with_suffix(_DAILY_REPORT_SWITCH_FILE.suffix + ".tmp")
    tmp_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_file.replace(_DAILY_REPORT_SWITCH_FILE)


def _get_local_daily_report_status(group_id: str | int) -> Optional[bool]:
    """读取公开版本地日报覆盖状态；未显式设置时返回 None。"""

    normalized_group_id = _normalize_group_id(group_id)
    if not normalized_group_id:
        return None
    if normalized_group_id in _daily_report_enabled_groups:
        return True
    if normalized_group_id in _daily_report_disabled_groups:
        return False
    return None


async def _set_local_daily_report_enabled(group_id: str, enabled: bool) -> None:
    """写入公开版本地日报群开关；仅在没有外部控制器时使用。"""

    async with _daily_report_switch_lock:
        if enabled:
            _daily_report_enabled_groups.add(group_id)
            _daily_report_disabled_groups.discard(group_id)
        else:
            _daily_report_disabled_groups.add(group_id)
            _daily_report_enabled_groups.discard(group_id)
        await asyncio.to_thread(_save_daily_report_switches_sync)


def is_daily_report_default_enabled() -> bool:
    """读取日报默认状态；配置非法或读取失败时按默认关闭处理。"""

    try:
        return bool(plugin_config.rollpig_daily_summary_enabled)
    except Exception as error:
        logger.warning(f"读取 rollpig_daily_summary_enabled 失败，已按默认关闭处理: {error}")
        return False


def set_daily_report_controller(
    checker: Optional[Callable[[str], bool]],
    setter: Optional[Callable[[str, bool], object]] = None,
    *,
    source_name: str = "外部控制器",
) -> None:
    """注册外部日报控制器；checker/ setter 同源，避免 V2 admin console 与本地状态分裂。"""

    global _daily_report_checker, _daily_report_setter, _daily_report_source_name
    _daily_report_checker = checker
    _daily_report_setter = setter
    _daily_report_source_name = source_name if checker is not None else "插件本地控制"


def set_daily_report_checker(checker: Optional[Callable[[str], bool]]) -> None:
    """兼容只读日报桥接；新接入应优先使用 set_daily_report_controller。"""

    set_daily_report_controller(checker, None)


def get_daily_report_group_status(group_id: str | int) -> tuple[bool, str]:
    """返回单群日报实际状态与来源说明。"""

    normalized_group_id = _normalize_group_id(group_id)
    if not normalized_group_id:
        return False, "无效群号"

    if _daily_report_checker is not None:
        return (
            _check_optional_group_switch(
                _daily_report_checker,
                normalized_group_id,
                switch_name="日报推送开关",
            ),
            _daily_report_source_name,
        )

    local_status = _get_local_daily_report_status(normalized_group_id)
    if local_status is not None:
        return local_status, "插件本地开启" if local_status else "插件本地关闭"

    default_enabled = is_daily_report_default_enabled()
    return default_enabled, "全局默认开启" if default_enabled else "全局默认关闭"


async def set_daily_report_group_enabled(group_id: str | int, enabled: bool) -> None:
    """设置单个群的日报状态；外部控制器存在时直接写外部控制器，否则写插件本地存储。"""

    normalized_group_id = _normalize_group_id(group_id)
    if not normalized_group_id:
        raise ValueError("群号不能为空")

    if _daily_report_checker is not None:
        if _daily_report_setter is None:
            raise RuntimeError("当前日报推送开关由外部控制器接管，但未提供命令写入接口")
        result = _daily_report_setter(normalized_group_id, enabled)
        if hasattr(result, "__await__"):
            await result
        return

    await _set_local_daily_report_enabled(normalized_group_id, enabled)


_load_daily_report_switches()


def resolve_roast_cooldown_seconds() -> int:
    """解析普通烤群友 CD（秒），支持通过配置覆盖。"""
    raw_hours = getattr(plugin_config, "rollpig_roast_cooldown_hours", 8.0)
    try:
        hours = float(raw_hours)
    except (TypeError, ValueError):
        logger.warning(f"rollpig_roast_cooldown_hours 配置非法: {raw_hours}，已回退到 8 小时")
        hours = 8.0

    if hours <= 0:
        logger.warning(f"rollpig_roast_cooldown_hours 必须 > 0，当前值: {hours}，已回退到 8 小时")
        hours = 8.0

    return max(1, int(hours * 3600))


def resolve_roast_charge_max() -> int:
    """解析普通烤群友充能上限；第一版限制在 1~6，避免群内刷屏。"""
    raw_max = getattr(plugin_config, "rollpig_roast_charge_max", 2)
    try:
        max_charges = int(raw_max)
    except (TypeError, ValueError):
        logger.warning(f"rollpig_roast_charge_max 配置非法: {raw_max}，已回退到 2")
        max_charges = 2
    if max_charges <= 0:
        logger.warning(f"rollpig_roast_charge_max 必须 > 0，当前值: {max_charges}，已回退到 2")
        max_charges = 2
    return max(1, min(6, max_charges))


def set_group_enable_checker(checker: Optional[Callable[[str], bool]]) -> None:
    """注册外部群启用检查器；传入 None 时恢复为“未接控制台，默认开启”模式。"""
    global _group_enable_checker
    _group_enable_checker = checker


def _check_optional_group_switch(
    checker: Optional[Callable[[str], bool]],
    group_id: str,
    *,
    switch_name: str,
) -> bool:
    """统一处理可选群开关：未接控制系统默认开启，检查异常时按关闭处理。"""
    normalized_group_id = str(group_id or "").strip()
    if not normalized_group_id:
        return True

    if checker is None:
        return True

    try:
        return bool(checker(normalized_group_id))
    except Exception as error:
        # 一旦宿主项目主动挂了外部检查器，就说明它希望群开关生效。
        # 这里若检查器本身异常，宁可按“关闭”处理，也不要意外把所有群放开。
        logger.warning(f"rollpig {switch_name} 检查失败: group={normalized_group_id} error={error}")
        return False


def is_group_rollpig_enabled(group_id: str) -> bool:
    """判断当前群是否启用 rollpig；未接入外部控制系统时默认返回 True。"""
    return _check_optional_group_switch(_group_enable_checker, group_id, switch_name="群启用")


def is_daily_report_enabled(group_id: str) -> bool:
    """判断当前群是否启用日报推送。

    若宿主接入外部控制器，则只读外部控制器；否则使用插件本地控制和全局默认值。
    确保 console 与公开版 localstore 不会出现两套状态同时生效。
    """

    enabled, _source = get_daily_report_group_status(group_id)
    return enabled


# 公开配置键和旧版 V2 桥接仍使用 daily_summary；保留函数别名避免升级破坏宿主接入。
is_daily_summary_default_enabled = is_daily_report_default_enabled
set_daily_summary_controller = set_daily_report_controller
set_daily_summary_checker = set_daily_report_checker
get_daily_summary_group_status = get_daily_report_group_status
set_daily_summary_group_enabled = set_daily_report_group_enabled
is_daily_summary_enabled = is_daily_report_enabled
