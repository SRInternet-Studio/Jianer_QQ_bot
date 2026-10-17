from __future__ import annotations

import asyncio
import datetime as dt
import random
import time
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import replace

from ..host import get_bots, on_bot_connect, on_shutdown, on_startup
from ..host import Bot, MessageSegment
from ..host import ActionFailed
from ..host import logger
from ..host import scheduler
from ..host import localstore

from .card_renderer import shutdown_card_renderer
from .catalog_renderer import shutdown_catalog_renderer
from .config import plugin_config
from .data_manager import LocalStoreUnavailableError
from .daily_protection import ProtectionRecoveryQueue, select_daily_protected_user_ids
from .daily_report import (
    DailyReport,
    DailyUserReportProfile,
    ProtectionReportItem,
    build_daily_report,
    normalize_daily_events,
)
from .daily_report_card_renderer import (
    render_daily_report_card,
    shutdown_daily_report_card_renderer,
)
from .pighub_service import PIGHUB_REFRESH_INTERVAL_HOURS, pighub_service
from .resource_manager import get_pig_by_id, pig_resource_manager, sync_rollpig_resources
from .yesterday_card_renderer import shutdown_yesterday_card_renderer
from .runtime import (
    ROLLPIG_TIMEZONE,
    is_daily_report_enabled,
    is_group_rollpig_enabled,
    rollpig_date_str,
)
from .store import store
from .store.base import RollpigStore
from .store.cloud import (
    DAILY_REPORT_ERROR_MAX_LENGTH,
    CloudDailyReportUnsupportedError,
    CloudStoreError,
)
from .store.models import (
    DailyReportDeliveryClaim,
    DailyReportDeliveryClaimResult,
    DailyReportDeliveryTransitionResult,
)
from .store.local_json import LocalJsonStore

background_resource_sync_tasks: set[asyncio.Task[None]] = set()
background_maintenance_tasks: set[asyncio.Task[None]] = set()
background_daily_report_tasks: set[asyncio.Task[None]] = set()
daily_report_recovery_dates: set[str] = set()
pending_daily_report_recovery_dates: set[str] = set()
DAILY_REPORT_INSTANCE_ID = uuid.uuid4().hex
DAILY_REPORT_SCHEDULE_TIME = dt.time(23, 45)
DAILY_REPORT_RETRY_CUTOFF = dt.time(0, 10)
DAILY_REPORT_TRANSITION_RETRY_SECONDS = 30
PROTECTION_SETTLEMENT_RETRY_DELAYS = (0.0, 1.0, 3.0)
protection_recovery_queue = ProtectionRecoveryQueue(
    localstore.get_plugin_data_file("daily_protection_recovery.json")
)
protection_recovery_task: asyncio.Task[None] | None = None
DAILY_REPORT_QUERY_CACHE_SECONDS = 300
DAILY_REPORT_QUERY_UNCONFIRMED_CACHE_SECONDS = 30
DAILY_REPORT_QUERY_CACHE_LIMIT = 12
daily_report_query_cache: dict[tuple[str, str, str, str], tuple[float, bytes | None, bool]] = {}
daily_report_query_tasks: dict[tuple[str, str, str, str], asyncio.Task[tuple[bytes | None, bool]]] = {}


# ================================ 运行时生命周期 ================================ #
@on_shutdown
async def shutdown_rollpig_runtime() -> None:
    """释放渲染器缓存与数据库连接。"""

    # 退出前等待后台同步与维护任务结束
    for task_set in (
        background_resource_sync_tasks,
        background_maintenance_tasks,
        background_daily_report_tasks,
    ):
        for task in list(task_set):
            task.cancel()
        for task in list(task_set):
            with suppress(asyncio.CancelledError):
                await task
        task_set.clear()
    from .reservation_delivery import shutdown_reservation_delivery_tasks

    await shutdown_reservation_delivery_tasks()
    await pighub_service.shutdown()
    await shutdown_yesterday_card_renderer()
    await shutdown_daily_report_card_renderer()
    await shutdown_card_renderer()
    await shutdown_catalog_renderer()
    await store.close()


# ================================ 资源同步任务 ================================ #
def get_resource_sync_interval_hours() -> int:
    """读取资源同步间隔配置（默认 24 小时）。"""

    try:
        return max(1, int(plugin_config.rollpig_resource_sync_interval_hours or 24))
    except Exception as error:
        logger.warning(f"rollpig_resource_sync_interval_hours 配置非法，已回退到 24 小时: {error}")
        return 24


async def run_background_resource_sync(source: str) -> None:
    """后台同步云端小猪资源包。"""

    try:
        message = await sync_rollpig_resources(force=False)
        logger.info(f"[小猪资源同步] {source}: {message}")
        # 延迟导入避免循环引用
        from .reservation_delivery import schedule_all_owned_deliveries
        from .reservation_flow import clear_resource_reservation_backoffs

        if clear_resource_reservation_backoffs():
            schedule_all_owned_deliveries(force=True)
    except Exception as error:
        logger.warning(f"[小猪资源同步] {source} 失败，继续使用当前资源: {error}")


def schedule_background_resource_sync(source: str) -> None:
    """注册后台资源同步任务；统一追踪 task，shutdown 时可取消并等待。"""

    task: asyncio.Task[None] = asyncio.create_task(run_background_resource_sync(source))
    background_resource_sync_tasks.add(task)
    task.add_done_callback(background_resource_sync_tasks.discard)


# ================================ 数据维护任务 ================================ #


async def run_data_maintenance(source: str) -> None:
    """独立清理过期历史和事件；单项失败不能阻止另一项继续执行。"""

    failures: list[str] = []
    for name, operation in (
        ("events", lambda: store.prune_events(days_to_keep=7)),
        ("history", lambda: store.prune_history(days_to_keep=14)),
    ):
        try:
            await operation()
        except Exception as error:
            failures.append(f"{name}={error}")
    if failures:
        logger.warning(f"[数据维护] {source} 部分失败: {'; '.join(failures)}")
        return
    logger.info(f"[数据维护] {source} 完成")


def schedule_background_maintenance(source: str) -> None:
    """把启动期清理放入后台并纳入 shutdown 收束，避免拖慢插件启动。"""

    task: asyncio.Task[None] = asyncio.create_task(run_data_maintenance(source))
    background_maintenance_tasks.add(task)
    task.add_done_callback(background_maintenance_tasks.discard)


@on_startup
async def startup_data_maintenance() -> None:
    """启动后补做一次清理，覆盖 Bot 长期无群活动或停机后的积压。"""

    schedule_background_maintenance("startup")


@scheduler.scheduled_job(
    "cron",
    hour=3,
    minute=30,
    timezone=ROLLPIG_TIMEZONE,
    id="rollpig_data_maintenance",
    max_instances=1,
)
async def data_maintenance_job() -> None:
    """每天独立执行数据保留策略，不再依赖日报是否启用或当天是否活跃。"""

    await run_data_maintenance("scheduled")


@on_startup
async def startup_pighub_refresh():
    """启动后随机延迟刷新 PigHub 索引；失败只影响外部找猪功能，不影响本地抽猪。"""

    pighub_service.schedule_startup_refresh()


@scheduler.scheduled_job("interval", hours=PIGHUB_REFRESH_INTERVAL_HOURS, id="rollpig_pighub_refresh", max_instances=1)
async def pighub_refresh_job():
    """低频刷新 PigHub 元数据索引；只请求列表 JSON，不下载图片本体。"""

    await pighub_service.refresh("interval")


@on_startup
async def startup_resource_sync():
    """启动后异步检查资源；关闭联网时仍需应用共享文案的本地清理语义。"""

    # 各同步器会自行判断总开关。这里始终安排一次轻量后台任务，
    # 让用户把共享文案 URL 设为空后，无需临时重新开启联网也能移除纯共享正文。
    schedule_background_resource_sync("startup")


@scheduler.scheduled_job("interval", hours=get_resource_sync_interval_hours(), id="rollpig_resource_sync", max_instances=1)
async def resource_sync_job():
    """低频检查云端资源包，减少多实例手动同步新猪素材的运维成本。"""

    if not plugin_config.rollpig_resource_sync_enabled:
        return
    await run_background_resource_sync("interval")


# ================================ 猪圈日报任务 ================================ #


def _group_ids_from_response(response: object) -> set[str] | None:
    """兼容 OneBot 直接列表与包裹 data 的群列表响应；格式非法返回 None。"""

    if isinstance(response, dict):
        response = response.get("data")
    if not isinstance(response, list):
        return None
    group_ids: set[str] = set()
    for item in response:
        if not isinstance(item, dict):
            return None
        group_id = item.get("group_id") or item.get("groupId")
        if not isinstance(group_id, (int, str)) or not str(group_id).strip():
            return None
        group_ids.add(str(group_id))
    return group_ids


async def resolve_daily_report_bots(group_ids: list[str]) -> dict[str, Bot]:
    """为目标群匹配发送 Bot（多 Bot 同群取最小 self_id）。"""

    unresolved = {str(group_id) for group_id in group_ids if group_id}
    resolved: dict[str, Bot] = {}
    bots = sorted(get_bots().items(), key=lambda item: str(item[0]))
    fallback_bots: list[tuple[str, Bot]] = []
    if not bots:
        return resolved

    # 优先批量读取群列表
    for self_id, bot in bots:
        try:
            visible_group_ids = _group_ids_from_response(await bot.get_group_list())
        except Exception as error:
            logger.warning(f"[猪圈日报] Bot 群列表读取失败，准备逐群确认: bot={self_id} error={error}")
            fallback_bots.append((self_id, bot))
            continue
        if visible_group_ids is None:
            logger.warning(f"[猪圈日报] Bot 群列表格式无效，准备逐群确认: bot={self_id}")
            fallback_bots.append((self_id, bot))
            continue
        for group_id in sorted(unresolved & visible_group_ids):
            resolved[group_id] = bot
            unresolved.remove(group_id)
        if not unresolved:
            return resolved

    # 成功读取的群列表已经给出明确成员范围，不能再用旧群资料把退群 Bot 捞回来。
    # 只有列表不可用时才实时确认 Bot 本人的成员身份；群资料可查询不代表仍在群。
    for group_id in sorted(unresolved):
        for self_id, bot in fallback_bots:
            try:
                member = await bot.get_group_member_info(
                    group_id=int(group_id), user_id=int(bot.self_id), no_cache=True,
                )
            except Exception:
                continue
            if isinstance(member, dict) and isinstance(member.get("data"), dict):
                member = member["data"]
            if not isinstance(member, dict) or (
                str(member.get("group_id")) != group_id
                or str(member.get("user_id")) != str(bot.self_id)
            ):
                continue
            resolved[group_id] = bot
            break
        if group_id not in resolved:
            logger.error(f"[猪圈日报] 没有在线 Bot 能确认目标群，已跳过投递: group={group_id}")
    return resolved


# ================================ 日报群资料聚合 ================================ #


def _group_member_rows(response: object) -> list[dict]:
    """兼容 OneBot 直接列表与包裹 data 的群成员列表响应。"""

    if isinstance(response, dict):
        response = response.get("data")
    if not isinstance(response, list):
        return []
    return [dict(item) for item in response if isinstance(item, dict)]


async def load_daily_group_member_names(bot: Bot, group_id: str) -> dict[str, str]:
    """每群只读取一次成员列表；失败时由事件快照姓名和用户 ID 继续降级。"""

    try:
        response = await bot.get_group_member_list(group_id=int(group_id), no_cache=False)
    except Exception as error:
        logger.warning(f"[猪圈日报] 群成员昵称读取失败，继续使用事件快照: group={group_id} error={error}")
        return {}
    names: dict[str, str] = {}
    for item in _group_member_rows(response):
        user_id = str(item.get("user_id") or item.get("userId") or "").strip()
        display_name = str(item.get("card") or item.get("nickname") or "").strip()
        if user_id and display_name:
            names[user_id] = display_name
    return names


def _resolved_profile_pig(pig_id: str, expert_level: int) -> tuple[str, str]:
    """解析排行用小猪名称和实际生效立绘；缺图交给 renderer 使用猪章兜底。"""

    pig = get_pig_by_id(pig_id) if pig_id else None
    if not pig:
        return pig_id, ""
    appearance = pig_resource_manager.resolve_pig_appearance(pig, expert_level)
    image_path = appearance.image_path or appearance.base_image_path
    return str(pig.get("name") or pig_id), str(image_path.resolve()) if image_path else ""


async def build_daily_user_profiles(
    report_store: RollpigStore,
    *,
    report: DailyReport,
    group_id: str,
    date_str: str,
    cutoff_at: str,
    group_rolls: dict[str, str],
    member_names: dict[str, str],
) -> dict[str, DailyUserReportProfile]:
    """补齐昵称、EX 与图鉴资料；Local 与 Cloud 都使用整群批量快照。"""

    profiles = {
        user_id: DailyUserReportProfile(
            user_id=user_id,
            display_name=member_names.get(user_id, ""),
        )
        for user_id in report.participant_ids
    }
    try:
        snapshots = await report_store.get_daily_report_profiles(
            group_id=group_id,
            date_str=date_str,
            cutoff_at=cutoff_at,
            user_ids=report.participant_ids,
        )
    except CloudDailyReportUnsupportedError:
        # 仅当 Cloud 已支持日报领取、但尚未提供批量资料接口时才会走到这里。
        # 不能逐用户补查，否则千人群会把一次日报放大成数百个请求；真正的
        # 旧 Cloud 会在领取阶段停止日报，避免多个实例各自降级后重复发送。
        logger.info(
            f"[猪圈日报] 当前 Cloud 不支持批量排行资料，已隐藏 EX 与图鉴榜: "
            f"group={group_id}"
        )
        return profiles
    except Exception as error:
        if not isinstance(report_store, LocalJsonStore):
            raise
        logger.warning(f"[猪圈日报] 本地排行资料读取失败，已隐藏相关排行: group={group_id} error={error}")
        return profiles

    snapshot_by_user = {item.user_id: item for item in snapshots}
    for user_id in report.participant_ids:
        snapshot = snapshot_by_user.get(user_id)
        if snapshot is None:
            continue

        daily_pig_id = str(group_rolls.get(user_id) or "")
        daily_matches = bool(
            daily_pig_id and snapshot.daily_pig_id == daily_pig_id
        )
        daily_level = snapshot.daily_ex_level if daily_matches else None
        daily_name, daily_image = (
            _resolved_profile_pig(daily_pig_id, daily_level or 0)
            if daily_pig_id
            else ("", "")
        )
        recent_name, recent_image = (
            _resolved_profile_pig(
                snapshot.recent_pig_id,
                snapshot.recent_ex_level or 0,
            )
            if snapshot.recent_pig_id
            else ("", "")
        )
        profiles[user_id] = DailyUserReportProfile(
            user_id=user_id,
            display_name=member_names.get(user_id, ""),
            daily_pig_id=daily_pig_id,
            daily_pig_name=daily_name,
            daily_ex_level=daily_level,
            daily_image_name=daily_image,
            daily_achieved_at=(snapshot.daily_achieved_at if daily_matches else ""),
            catalog_count=snapshot.catalog_count,
            catalog_achieved_at=snapshot.catalog_achieved_at,
            recent_pig_id=snapshot.recent_pig_id,
            recent_pig_name=recent_name,
            recent_image_name=recent_image,
        )
    return profiles


async def settle_daily_protections(
    group_ids: Sequence[str],
    *,
    date_str: str,
    protect_date: str,
    cutoff_at: str,
) -> None:
    """为不投递日报的群结算次日保护。

    次日保护是当日游戏事实的结算，只依赖截止点之前的事件，与是否开启
    日报通知无关；开启日报的群仍在 build_group_daily_report 内写入，
    保持"写入成功才在卡片中承诺保护"的一致性，两条路径互不重叠。
    """

    if not group_ids:
        return

    # ================================ 查询隔离与写入重试 ================================ #
    event_query = None
    for attempt, delay in enumerate(PROTECTION_SETTLEMENT_RETRY_DELAYS, start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            result = await store.query_daily_events(date_str=date_str, cutoff_at=cutoff_at)
            if not result.available:
                raise CloudStoreError("保护结算事件记录暂时不可用")
            event_query = result
            break
        except Exception as error:
            logger.warning(
                f"[猪圈日报] 保护结算事件查询失败: "
                f"attempt={attempt}/{len(PROTECTION_SETTLEMENT_RETRY_DELAYS)} error={error}"
            )
    if event_query is None:
        # 不可用不等于零事件；仅保存失败群及固定截止点，后续逐群补查。
        for group_id in group_ids:
            if is_group_rollpig_enabled(group_id):
                await protection_recovery_queue.enqueue(
                    group_id, date_str, protect_date, cutoff_at, protected_ids=None,
                )
        return
    events_by_group: dict[str, list[dict]] = {}
    for item in event_query.items:
        group_id = str(item.get("group_id") or "")
        if group_id:
            events_by_group.setdefault(group_id, []).append(item)
    failures: list[str] = []
    for group_id in group_ids:
        if not is_group_rollpig_enabled(group_id):
            continue
        events = normalize_daily_events(
            events_by_group.get(group_id, ()),
            group_id=group_id,
            cutoff_at=cutoff_at,
        )
        protected_ids = select_daily_protected_user_ids(events)
        settled = False
        last_error: Exception | None = None
        for attempt, delay in enumerate(PROTECTION_SETTLEMENT_RETRY_DELAYS, start=1):
            if delay:
                await asyncio.sleep(delay)
            if not is_group_rollpig_enabled(group_id):
                break
            try:
                await store.replace_group_protections(group_id, protected_ids, protect_date)
                settled = True
                await protection_recovery_queue.discard(date_str, group_id)
                break
            except Exception as error:
                last_error = error
                logger.warning(
                    f"[猪圈日报] 群保护写入失败，准备重试: group={group_id} "
                    f"attempt={attempt}/{len(PROTECTION_SETTLEMENT_RETRY_DELAYS)} error={error}"
                )
        if not settled:
            failures.append(f"{group_id}={last_error}")
            if is_group_rollpig_enabled(group_id):
                await protection_recovery_queue.enqueue(
                    group_id, date_str, protect_date, cutoff_at, protected_ids=protected_ids,
                )
    if failures:
        logger.warning(f"[猪圈日报] 部分群次日保护结算失败: {'; '.join(failures)}")
        return
    logger.info(f"[猪圈日报] 已完成 {len(group_ids)} 个未投递群的次日保护结算")


async def settle_daily_protections_safely(
    group_ids: Sequence[str],
    *,
    date_str: str,
    protect_date: str,
    cutoff_at: str,
) -> None:
    """隔离附加保护结算异常，避免它阻断日报投递或任务收尾。"""

    try:
        await settle_daily_protections(
            group_ids,
            date_str=date_str,
            protect_date=protect_date,
            cutoff_at=cutoff_at,
        )
    except Exception as error:
        logger.exception(f"[猪圈日报] 次日保护结算异常，继续处理日报: error={error}")


# ================================ 失败保护结算恢复 ================================ #


async def _run_protection_recovery() -> None:
    """只补写失败群的保护，不领取日报或发送消息。"""

    try:
        await protection_recovery_queue.run_due(store)
    except Exception as error:
        logger.exception(f"[猪圈日报] 保护补偿任务异常: error={error}")


def schedule_protection_recovery() -> None:
    """合并启动和定时检查，纳入 shutdown 收束以免关闭 Store 后仍在写入。"""

    global protection_recovery_task
    if protection_recovery_task is not None and not protection_recovery_task.done():
        return
    protection_recovery_task = asyncio.create_task(_run_protection_recovery())
    background_maintenance_tasks.add(protection_recovery_task)
    protection_recovery_task.add_done_callback(background_maintenance_tasks.discard)


@on_startup
async def startup_protection_recovery() -> None:
    """重启后从本地补偿记录恢复原次数与下一次时间，不依赖 Bot 连接。"""

    schedule_protection_recovery()


@scheduler.scheduled_job("interval", seconds=30, id="rollpig_protection_recovery", max_instances=1)
async def protection_recovery_job() -> None:
    """轻量检查失败队列；没有待补偿群时不查询 Cloud。"""

    schedule_protection_recovery()


async def load_group_daily_report(
    report_store: RollpigStore,
    bot: Bot,
    *,
    date_str: str,
    group_id: str,
    cutoff_at: str,
) -> DailyReport:
    """只读地装配固定截止点的群日报，供定时投递与主动查询共用。"""

    group_rolls, event_query = await asyncio.gather(
        report_store.get_group_rolls(
            group_id,
            date_str,
            cutoff_at=cutoff_at,
        ),
        report_store.query_daily_events(
            date_str=date_str,
            group_id=group_id,
            cutoff_at=cutoff_at,
        ),
    )
    if not event_query.available:
        raise CloudStoreError("日报事件记录暂时不可用")
    try:
        active_user_ids = await report_store.get_group_active_user_ids(
            group_id,
            date_str,
            cutoff_at=cutoff_at,
        )
    except Exception as error:
        logger.warning(f"[猪圈日报] 活跃用户读取失败，排行仅使用抽猪和互动用户: group={group_id} error={error}")
        active_user_ids = set()

    bot_user_ids = tuple(str(self_id) for self_id in get_bots())
    preliminary = build_daily_report(
        date_str=date_str,
        group_id=group_id,
        group_rolls=group_rolls,
        raw_events=event_query.items,
        active_user_ids=active_user_ids,
        bot_user_ids=bot_user_ids,
        cutoff_at=cutoff_at,
    )
    if not preliminary.has_activity:
        return preliminary
    member_names = await load_daily_group_member_names(bot, group_id)
    profiles = await build_daily_user_profiles(
        report_store,
        report=preliminary,
        group_id=group_id,
        date_str=date_str,
        cutoff_at=cutoff_at,
        group_rolls=group_rolls,
        member_names=member_names,
    )
    return build_daily_report(
        date_str=date_str,
        group_id=group_id,
        group_rolls=group_rolls,
        raw_events=event_query.items,
        active_user_ids=active_user_ids,
        bot_user_ids=bot_user_ids,
        user_profiles=profiles,
        cutoff_at=cutoff_at,
    )


def _with_daily_protections(
    report: DailyReport,
    protect_date: str,
    user_ids: Sequence[str],
) -> DailyReport:
    """仅把已确认写入的保护名单填入卡片，不在展示层推断权益。"""

    expires_at = dt.datetime.combine(
        dt.date.fromisoformat(protect_date),
        dt.time(23, 59),
        tzinfo=ROLLPIG_TIMEZONE,
    ).isoformat()
    return replace(
        report,
        protections=tuple(
            ProtectionReportItem(
                user_id=user_id,
                display_name=report.display_names.get(user_id, ""),
                expires_at=expires_at,
            )
            for user_id in user_ids
        ),
    )


async def build_group_daily_report(
    report_store: RollpigStore,
    bot: Bot,
    *,
    date_str: str,
    protect_date: str,
    group_id: str,
    cutoff_at: str,
) -> DailyReport:
    """定时投递专用：读取日报、写入次日保护，再装配保护券。"""

    report = await load_group_daily_report(
        report_store, bot, date_str=date_str, group_id=group_id, cutoff_at=cutoff_at,
    )
    if not report.has_activity:
        return report
    protected_ids = select_daily_protected_user_ids(report.events)
    await report_store.replace_group_protections(
        group_id,
        protected_ids,
        protect_date,
    )
    return _with_daily_protections(report, protect_date, protected_ids)


# ================================ 主动查询最近一期日报 ================================ #


def latest_daily_report_query_date(now: dt.datetime | None = None) -> str | None:
    """日报投递和保护结算窗口结束后，才开放昨天的正式卡片。"""

    current = (now or dt.datetime.now(ROLLPIG_TIMEZONE)).astimezone(ROLLPIG_TIMEZONE)
    wall_time = current.time().replace(tzinfo=None)
    if wall_time >= DAILY_REPORT_SCHEDULE_TIME or wall_time < DAILY_REPORT_RETRY_CUTOFF:
        return None
    return (current.date() - dt.timedelta(days=1)).isoformat()


async def _build_daily_report_query_card(
    bot: Bot,
    group_id: str,
    date_str: str,
) -> tuple[bytes | None, bool]:
    """只读重建封版数据，并用已生效记录核对唯一保护候选。"""

    cutoff_at = dt.datetime.combine(
        dt.date.fromisoformat(date_str), DAILY_REPORT_SCHEDULE_TIME,
        tzinfo=ROLLPIG_TIMEZONE,
    ).isoformat()
    protect_date = (dt.date.fromisoformat(date_str) + dt.timedelta(days=1)).isoformat()
    report = await load_group_daily_report(
        store, bot, date_str=date_str, group_id=group_id, cutoff_at=cutoff_at,
    )
    if not report.has_activity:
        return None, False

    candidates = select_daily_protected_user_ids(report.events)
    try:
        confirmed = [
            user_id for user_id in candidates
            if await store.is_protected(group_id, user_id, protect_date)
        ]
    except (CloudStoreError, LocalStoreUnavailableError) as error:
        # 附加权益查询失败不能挡住日报主体；只展示确已生效的保护，
        # 未确认结果沿用 30 秒短缓存与图片后的提示，不在查卡路径补写。
        confirmed = []
        logger.warning(f"[猪圈日报] 查卡保护未确认，隐藏保护券: group={group_id} error={error}")
    report = _with_daily_protections(report, protect_date, confirmed)
    rendered = await render_daily_report_card(
        report, cutoff_time=DAILY_REPORT_SCHEDULE_TIME.strftime("%H:%M"),
    )
    return rendered.data, bool(candidates and not confirmed)


async def render_latest_daily_report_query_card(
    bot: Bot,
    group_id: str,
    date_str: str,
) -> tuple[bytes | None, bool]:
    """同群同日合并并发查卡，短暂复用成图以限制重复渲染。"""

    key = (group_id, date_str, str(bot.self_id), pig_resource_manager.resource_version)
    now = time.monotonic()
    cached = daily_report_query_cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1], cached[2]

    task = daily_report_query_tasks.get(key)
    if task is None:
        task = asyncio.create_task(_build_daily_report_query_card(bot, group_id, date_str))
        daily_report_query_tasks[key] = task
        task.add_done_callback(
            lambda finished: daily_report_query_tasks.pop(key, None)
            if daily_report_query_tasks.get(key) is finished else None
        )
    result = await asyncio.shield(task)
    daily_report_query_cache[key] = (
        time.monotonic() + (
            DAILY_REPORT_QUERY_UNCONFIRMED_CACHE_SECONDS
            if result[1] else DAILY_REPORT_QUERY_CACHE_SECONDS
        ),
        *result,
    )
    for expired_key, value in list(daily_report_query_cache.items()):
        if value[0] <= now:
            daily_report_query_cache.pop(expired_key, None)
    while len(daily_report_query_cache) > DAILY_REPORT_QUERY_CACHE_LIMIT:
        oldest_key = min(daily_report_query_cache, key=lambda item: daily_report_query_cache[item][0])
        daily_report_query_cache.pop(oldest_key)
    return result


def _daily_report_message_id(response: object) -> str:
    """兼容 OneBot 直接字典与包裹 data 的发送响应，只记录可确认的消息 ID。"""

    if not isinstance(response, dict):
        return ""
    payload = response.get("data") if isinstance(response.get("data"), dict) else response
    return str(payload.get("message_id") or payload.get("messageId") or "")


def _normalize_daily_report_claim_result(
    result: object,
) -> DailyReportDeliveryClaimResult:
    """兼容尚未升级的自定义 Store，使日报升级不会直接打断宿主扩展。"""

    if isinstance(result, DailyReportDeliveryClaimResult):
        return result
    if isinstance(result, (tuple, list)):
        return DailyReportDeliveryClaimResult(
            claims=tuple(
                item for item in result if isinstance(item, DailyReportDeliveryClaim)
            )
        )
    return DailyReportDeliveryClaimResult()


def _normalize_daily_report_transition_result(
    result: object,
) -> DailyReportDeliveryTransitionResult:
    """将旧 Store 的 bool 返回值归一成包含状态与重试时间的新结果。"""

    if isinstance(result, DailyReportDeliveryTransitionResult):
        return result
    return DailyReportDeliveryTransitionResult(ok=bool(result))


def _parse_daily_report_retry_at(raw_value: str) -> dt.datetime | None:
    """Cloud 数据库存储 UTC naive；客户端明确按 UTC 解释，避免宿主时区漂移。"""

    normalized = str(raw_value or "").strip()
    if not normalized:
        return None
    try:
        parsed = dt.datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        logger.warning(f"[猪圈日报] Cloud 返回了非法重试时间，已停止本次重领: value={normalized}")
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(ROLLPIG_TIMEZONE)


def _daily_report_retry_delay(
    date_str: str,
    retry_times: list[str],
    *,
    now: dt.datetime | None = None,
) -> float | None:
    """选择服务端允许的最早重领时间，并硬性限制在次日 00:10 以前。"""

    deadline = dt.datetime.combine(
        dt.date.fromisoformat(date_str) + dt.timedelta(days=1),
        DAILY_REPORT_RETRY_CUTOFF,
        tzinfo=ROLLPIG_TIMEZONE,
    )
    current_time = now or dt.datetime.now(ROLLPIG_TIMEZONE)
    if current_time >= deadline:
        return None
    parsed_times = [
        parsed
        for raw_value in retry_times
        if (parsed := _parse_daily_report_retry_at(raw_value)) is not None
        and parsed < deadline
    ]
    if not parsed_times:
        return None
    delay = (min(parsed_times) - current_time).total_seconds()
    # 到点后仍未领取通常只是客户端与 Cloud 存在亚秒级时钟差；保留一秒下限，
    # 避免异常响应造成定时任务原地空转。
    return max(1.0, delay)


def _daily_report_deadline_reached(date_str: str) -> bool:
    """判断指定日报日期是否已越过次日投递硬截止点。"""

    deadline = dt.datetime.combine(
        dt.date.fromisoformat(date_str) + dt.timedelta(days=1),
        DAILY_REPORT_RETRY_CUTOFF,
        tzinfo=ROLLPIG_TIMEZONE,
    )
    return dt.datetime.now(ROLLPIG_TIMEZONE) >= deadline


def _daily_report_transition_retry_at() -> str:
    """状态迁移响应丢失时短暂再查 Cloud，由服务端最终状态决定能否重领。"""

    return (
        dt.datetime.now(dt.timezone.utc)
        + dt.timedelta(seconds=DAILY_REPORT_TRANSITION_RETRY_SECONDS)
    ).isoformat()


def _daily_report_error_summary(error: Exception) -> str:
    """保留异常类型和开头诊断信息，并满足 Cloud 状态字段长度约束。"""

    return f"{type(error).__name__}: {error}"[:DAILY_REPORT_ERROR_MAX_LENGTH]


def _is_retryable_daily_report_send_failure(error: Exception) -> bool:
    """只把 NapCat 明确返回的富媒体传输失败视为确认未发送。"""

    if not isinstance(error, ActionFailed):
        return False
    details = " ".join(
        str(error.info.get(key) or "")
        for key in ("message", "wording", "msg", "errMsg", "error")
    ).casefold()
    return "rich media transfer failed" in details


def _daily_report_retry_status(
    transition: DailyReportDeliveryTransitionResult,
    retry_at: str,
) -> str:
    """区分已安排重领与服务端已判定失败，供逐群日志准确展示。"""

    if transition.status == "failed":
        return "failed"
    return "retry" if transition or retry_at else "failed"


async def _transition_daily_report_safely(
    claim: DailyReportDeliveryClaim,
    action: str,
    *,
    message_id: str = "",
    error: str = "",
) -> DailyReportDeliveryTransitionResult:
    """收束失败后的 Cloud 状态；二次异常只能记录，不能阻断其他群。"""

    try:
        return _normalize_daily_report_transition_result(
            await store.transition_daily_report_delivery(
                claim,
                action,
                message_id=message_id,
                error=error,
            )
        )
    except Exception as transition_error:
        logger.error(
            f"[猪圈日报] 投递状态更新失败: group={claim.group_id} "
            f"action={action} error={transition_error}"
        )
        return DailyReportDeliveryTransitionResult(ok=False)


async def _deliver_daily_report_claim(
    claim: DailyReportDeliveryClaim,
    *,
    delivery_bots: dict[str, Bot],
    protect_date: str,
    cutoff_time: str,
) -> tuple[bool, bool, str, str]:
    """处理一份已领取日报，返回生成、发送、重领时间及本次最终状态。"""

    group_id = claim.group_id
    bot = delivery_bots.get(group_id)
    if bot is None or str(bot.self_id) != claim.delivery_bot_id:
        transition = await _transition_daily_report_safely(
            claim,
            "release",
            error="delivery_bot_unavailable",
        )
        retry_at = transition.next_attempt_at
        if not transition and not retry_at:
            retry_at = _daily_report_transition_retry_at()
        return False, False, retry_at, _daily_report_retry_status(transition, retry_at)

    # Claim 与实际处理之间可能隔着前序群渲染或 Cloud 退避；必须在任何日报
    # 聚合及保护名单写入前重读开关，避免管理员关闭后仍产生群内副作用。
    if not is_group_rollpig_enabled(group_id) or not is_daily_report_enabled(group_id):
        transition = await _transition_daily_report_safely(
            claim,
            "skip",
            error="daily_report_disabled_before_delivery",
        )
        retry_at = transition.next_attempt_at
        if not transition and not retry_at:
            retry_at = _daily_report_transition_retry_at()
        logger.info(f"[猪圈日报] 群开关已关闭，跳过已领取日报: group={group_id}")
        return False, False, retry_at, "skipped" if transition else "retry"

    sending_started = False
    report_built = False
    try:
        report = await build_group_daily_report(
            store,
            bot,
            date_str=claim.date_str,
            protect_date=protect_date,
            group_id=group_id,
            cutoff_at=claim.cutoff_at,
        )
        if not report.has_activity:
            if not await store.transition_daily_report_delivery(claim, "skip"):
                raise CloudStoreError("日报空活动状态确认失败")
            logger.info(f"[猪圈日报] 群内没有可展示活动，已跳过: group={group_id}")
            return False, False, "", "skipped"

        report_built = True
        rendered = await render_daily_report_card(
            report,
            cutoff_time=cutoff_time,
        )
        # 渲染可能耗时较长；在提交发送意图前再次确认时间和开关，避免卡片
        # 已经跨过硬截止点或管理员刚关闭日报后仍发送出去。build 内已写入的
        # 次日保护属于当日事实结算，终止投递时只放弃发送，不回滚保护。
        if _daily_report_deadline_reached(claim.date_str):
            await _transition_daily_report_safely(
                claim,
                "release",
                error="delivery_deadline_passed_after_render",
            )
            logger.info(f"[猪圈日报] 渲染完成时已超过投递截止，释放日报: group={group_id}")
            return report_built, False, "", "failed"
        if not is_group_rollpig_enabled(group_id) or not is_daily_report_enabled(group_id):
            transition = await _transition_daily_report_safely(
                claim,
                "skip",
                error="daily_report_disabled_after_render",
            )
            # 与投递前的 skip 分支保持一致：迁移失败时按短退避重领，
            # 由下一轮再次确认 skip 状态，避免 Cloud 端租约悬置到过期。
            retry_at = transition.next_attempt_at
            if not transition and not retry_at:
                retry_at = _daily_report_transition_retry_at()
            logger.info(f"[猪圈日报] 渲染完成时群开关已关闭，跳过日报: group={group_id}")
            return report_built, False, retry_at, "skipped" if transition else "retry"
        if not await store.transition_daily_report_delivery(claim, "sending"):
            raise CloudStoreError("日报发送意图未获 Cloud 确认")
        sending_started = True
        response = await bot.send_group_msg(
            group_id=int(group_id),
            message=MessageSegment.image(rendered.data),
        )
        if not await store.transition_daily_report_delivery(
            claim,
            "sent",
            message_id=_daily_report_message_id(response),
        ):
            raise CloudStoreError("日报发送完成状态未获 Cloud 确认")
        return True, True, "", "sent"
    except Exception as error:
        # sending 前可以安全释放；进入 sending 后，只有 NapCat 明确拒绝富媒体发送
        # 才请求重领。超时、断连等结果不明的异常仍冻结为 uncertain，避免重复消息。
        transition_action = "release"
        if sending_started:
            transition_action = (
                "retry"
                if _is_retryable_daily_report_send_failure(error)
                else "uncertain"
            )
        transition = await _transition_daily_report_safely(
            claim,
            transition_action,
            error=_daily_report_error_summary(error),
        )
        retryable = transition_action in {"release", "retry"}
        retry_at = transition.next_attempt_at if retryable else ""
        if retryable and not transition and not retry_at:
            retry_at = _daily_report_transition_retry_at()
        if retryable:
            result_status = _daily_report_retry_status(transition, retry_at)
        else:
            result_status = "uncertain" if transition else "failed"
        logger.warning(
            f"[猪圈日报] 推送失败: group={group_id} attempt={claim.attempt_count} "
            f"fallback={result_status} error={error}"
        )
        return report_built, False, retry_at, result_status


@scheduler.scheduled_job("cron", hour=23, minute=45, timezone=ROLLPIG_TIMEZONE, id="rollpig_daily_report")
async def daily_report_job(
    *,
    report_date: str | None = None,
    random_delay_enabled: bool = True,
):
    """每晚 23:45 起推送日报，失败重试最晚持续到次日 00:10。"""

    # 日期与截止点必须在随机延迟前固定；所有后端读取都使用同一截止点，
    # 23:45 后的新抽取和互动只能进入下一日实时功能，不能混入本期日报。
    report_date = report_date or rollpig_date_str()
    protect_date = (dt.date.fromisoformat(report_date) + dt.timedelta(days=1)).isoformat()
    cutoff = dt.datetime.combine(
        dt.date.fromisoformat(report_date),
        DAILY_REPORT_SCHEDULE_TIME,
        tzinfo=ROLLPIG_TIMEZONE,
    )
    cutoff_at = cutoff.isoformat()
    cutoff_time = cutoff.strftime("%H:%M")
    delay = random.randint(0, 600) if random_delay_enabled else 0
    source = "定时" if random_delay_enabled else "启动恢复"
    logger.info(f"[猪圈日报] {source}触发，延迟 {delay} 秒后推送")
    await asyncio.sleep(delay)
    protection_group_ids: list[str] = []
    protection_settlement_task: asyncio.Task[None] | None = None
    try:
        active_groups = await store.get_active_group_ids(report_date)
        if not active_groups:
            logger.info("[猪圈日报] 今日无活跃群，跳过推送")
            return

        # ================================ 控制台开关过滤 ================================ #
        # rollpig 总开关关闭的群既不推日报，也不参与次日保护结算，
        # 保证"彻底关闭"；日报推送开关只控制通知投递，不影响游戏结算。
        enabled_active_groups = [
            group_id for group_id in sorted(active_groups)
            if is_group_rollpig_enabled(group_id)
        ]
        if not enabled_active_groups:
            logger.info("[猪圈日报] 今日没有启用 rollpig 的活跃群，跳过推送")
            return

        # ================================ 日报推送开关过滤 ================================ #
        # 日报支持“默认关闭 + 分群开启”，先过滤出真正开启日报的群，
        # 之后再单独结算未投递群的保护。
        report_push_groups = [
            group_id for group_id in enabled_active_groups
            if is_daily_report_enabled(group_id)
        ]
        report_push_group_set = set(report_push_groups)
        protection_group_ids = [
            group_id
            for group_id in enabled_active_groups
            if group_id not in report_push_group_set
        ]
        if protection_group_ids:
            # 未开启日报群的保护不能等待日报重试结束；启动后与投递并行，
            # 并在 finally 中等待同一个任务，保证只结算一次且任务退出前收尾。
            protection_settlement_task = asyncio.create_task(
                settle_daily_protections_safely(
                    protection_group_ids,
                    date_str=report_date,
                    protect_date=protect_date,
                    cutoff_at=cutoff_at,
                )
            )

        if not report_push_groups:
            logger.info("[猪圈日报] 没有群开启日报推送")
            return

        delivery_bots = await resolve_daily_report_bots(report_push_groups)
        if not delivery_bots:
            logger.warning("[猪圈日报] 无可用 Bot，跳过推送")
            return

        report_groups: set[str] = set()
        sent_groups: set[str] = set()
        claimed_attempts = 0

        # ================================ Cloud 安全重领循环 ================================ #
        # Claim 响应同时携带“其他实例租约何时到期”；release 响应携带当前群的
        # 服务端退避时间。这里只按这些明确时间唤醒，不做固定频率空轮询。
        while delivery_bots:
            claim_result = _normalize_daily_report_claim_result(
                await store.claim_daily_report_deliveries(
                    instance_id=DAILY_REPORT_INSTANCE_ID,
                    delivery_bots={
                        group_id: str(bot.self_id)
                        for group_id, bot in delivery_bots.items()
                    },
                    date_str=report_date,
                    cutoff_at=cutoff_at,
                )
            )
            retry_times = [claim_result.next_claim_at]
            claimed_attempts += len(claim_result.claims)
            deadline_reached = False
            for index, claim in enumerate(claim_result.claims):
                if _daily_report_deadline_reached(report_date):
                    # 同一批 claim 会顺序渲染和发送；一旦到达硬截止点，当前及
                    # 后续租约都必须主动释放，不能让慢群拖着整批越界投递。
                    for remaining_claim in claim_result.claims[index:]:
                        transition = await _transition_daily_report_safely(
                            remaining_claim,
                            "release",
                            error="delivery_deadline_passed",
                        )
                        logger.info(
                            f"[猪圈日报] 群投递结果: group={remaining_claim.group_id} "
                            f"status={transition.status or 'failed'} "
                            f"attempt={remaining_claim.attempt_count}"
                        )
                    logger.info(
                        f"[猪圈日报] 已到次日投递截止时间，释放剩余 "
                        f"{len(claim_result.claims) - index} 个领取任务"
                    )
                    deadline_reached = True
                    break
                (
                    report_built,
                    sent,
                    retry_at,
                    result_status,
                ) = await _deliver_daily_report_claim(
                    claim,
                    delivery_bots=delivery_bots,
                    protect_date=protect_date,
                    cutoff_time=cutoff_time,
                )
                logger.info(
                    f"[猪圈日报] 群投递结果: group={claim.group_id} "
                    f"status={result_status} attempt={claim.attempt_count}"
                )
                if report_built:
                    report_groups.add(claim.group_id)
                if sent:
                    sent_groups.add(claim.group_id)
                if retry_at:
                    retry_times.append(retry_at)

            if deadline_reached:
                break
            retry_delay = _daily_report_retry_delay(report_date, retry_times)
            if retry_delay is None:
                break
            logger.info(f"[猪圈日报] 等待 {retry_delay:.1f} 秒后重新领取未完成日报")
            await asyncio.sleep(retry_delay)

            # 等待期间 Bot 可能上下线或改变群可见性；重领前重新确认路由，不能沿用
            # 旧 Bot 对象把恢复任务发往已经断开的连接。
            delivery_bots = await resolve_daily_report_bots(report_push_groups)

        if claimed_attempts == 0:
            logger.info("[猪圈日报] 当前群日报已由其他实例完成，或没有可安全领取的任务")
        else:
            logger.info(
                f"[猪圈日报] 推送完成, 成功发送 {len(sent_groups)}/{len(report_groups)}，"
                f"累计领取 {claimed_attempts} 次，候选群 {len(report_push_groups)} 个"
            )
    except CloudStoreError as error:
        logger.warning(f"[猪圈日报] 云端账本暂时不可用，跳过本轮推送: {error}")
    except Exception as error:
        logger.error(f"[猪圈日报] 任务异常: {error}")
    finally:
        if protection_settlement_task is not None:
            await protection_settlement_task


# ================================ 日报启动恢复 ================================ #


def _daily_report_startup_recovery_date(
    now: dt.datetime | None = None,
) -> str:
    """仅在 23:45～次日 00:10 的安全窗口内返回待恢复日报日期。"""

    current_time = now or dt.datetime.now(ROLLPIG_TIMEZONE)
    local_time = current_time.astimezone(ROLLPIG_TIMEZONE)
    wall_time = local_time.time().replace(tzinfo=None)
    if wall_time >= DAILY_REPORT_SCHEDULE_TIME:
        return local_time.date().isoformat()
    if wall_time < DAILY_REPORT_RETRY_CUTOFF:
        return (local_time.date() - dt.timedelta(days=1)).isoformat()
    return ""


@on_startup
async def startup_daily_report_recovery() -> None:
    """启动时补跑安全窗口内的日报；持久化 claim 负责防重与续跑。"""

    # NoneBot 的 startup 可能早于适配器建立连接。此时不安排必然空跑的任务，
    # 等 on_bot_connect 拿到真实 Bot 后再恢复，避免唯一一次机会被提前消耗。
    if not get_bots():
        return
    report_date = _daily_report_startup_recovery_date()
    schedule_daily_report_recovery(report_date)


async def _run_daily_report_recovery(report_date: str) -> None:
    """串行执行合并后的恢复请求；运行期间的新连接只追加一次重查。"""

    try:
        while True:
            pending_daily_report_recovery_dates.discard(report_date)
            await daily_report_job(
                report_date=report_date,
                random_delay_enabled=False,
            )
            if report_date not in pending_daily_report_recovery_dates:
                return
    finally:
        pending_daily_report_recovery_dates.discard(report_date)
        daily_report_recovery_dates.discard(report_date)


def schedule_daily_report_recovery(report_date: str) -> None:
    """在安全窗口安排恢复；运行期间的新 Bot 连接会合并为一次后续重查。"""

    if not report_date:
        return
    if report_date in daily_report_recovery_dates:
        # 当前任务解析过 Bot 路由后，新连接可能带来此前不可达的群。记录一次
        # 待重跑即可；多个连接事件会自然合并，不并发创建重复恢复任务。
        pending_daily_report_recovery_dates.add(report_date)
        return
    daily_report_recovery_dates.add(report_date)
    task: asyncio.Task[None] = asyncio.create_task(
        _run_daily_report_recovery(report_date)
    )
    background_daily_report_tasks.add(task)
    task.add_done_callback(background_daily_report_tasks.discard)


@on_bot_connect
async def bot_connect_daily_report_recovery(bot: Bot) -> None:
    """Bot 晚于插件启动连接时，重新尝试安全窗口内的日报恢复。"""

    schedule_daily_report_recovery(_daily_report_startup_recovery_date())
