from __future__ import annotations

import asyncio
import time

from ..host import get_bots, on_bot_connect, on_shutdown, on_startup
from ..host import logger
from ..host import scheduler

from .store import store
from .store.cloud import CloudReservationUnsupportedError


DELIVERY_CHECK_INTERVAL_SECONDS = 60.0
_owned_bot_ids: set[str] = set()
_retryable_bot_ids: set[str] = set()
_last_check_by_bot: dict[str, float] = {}
_tasks_by_bot: dict[str, asyncio.Task] = {}


def register_owned_reservation(delivery_bot_id: str) -> None:
    """创建预约后记住负责 Bot，供机会式检查与低频 owner 轮询复用。"""

    if delivery_bot_id:
        bot_id = str(delivery_bot_id)
        _owned_bot_ids.add(bot_id)
        _retryable_bot_ids.discard(bot_id)


async def restore_owned_reservations(delivery_bot_id: str) -> None:
    """Bot 连接时恢复 owner；瞬时失败保留为可在后续指令上重试的未知状态。"""

    bot_id = str(delivery_bot_id)
    try:
        if await store.has_owned_roast_reservations(bot_id):
            _owned_bot_ids.add(bot_id)
        else:
            _owned_bot_ids.discard(bot_id)
        _retryable_bot_ids.discard(bot_id)
    except CloudReservationUnsupportedError:
        # 旧 Cloud 没有预约接口；这是稳定的不支持状态，不能当瞬时故障永久轮询。
        _owned_bot_ids.discard(bot_id)
        _retryable_bot_ids.discard(bot_id)
        logger.info(f"rollpig 当前 Cloud 不支持预约 owner 恢复，已停止轮询: bot={bot_id}")
    except Exception as error:
        _retryable_bot_ids.add(bot_id)
        logger.warning(f"rollpig 恢复预约 owner 状态失败: bot={bot_id} error={error}")


async def _deliver_if_due(bot_id: str) -> None:
    # 延迟导入避免 helpers -> reservation_delivery -> reservation_flow -> roast_flow
    # -> helpers 的模块初始化环；真正检查时插件已完成全部模块加载。
    from .reservation_flow import deliver_ready_reservations

    now = time.monotonic()
    if bot_id not in _owned_bot_ids and bot_id not in _retryable_bot_ids:
        return
    if now - _last_check_by_bot.get(bot_id, 0.0) < DELIVERY_CHECK_INTERVAL_SECONDS:
        return
    _last_check_by_bot[bot_id] = now

    try:
        # 增强版 claim 同时返回 has_owned；无论正常 Owner 还是启动恢复失败，
        # 每个节流窗口都只需要一个 Cloud 请求。
        result = await deliver_ready_reservations(bot_id)
        _retryable_bot_ids.discard(bot_id)
        if result.has_owned:
            _owned_bot_ids.add(bot_id)
        else:
            _owned_bot_ids.discard(bot_id)
    except CloudReservationUnsupportedError:
        _owned_bot_ids.discard(bot_id)
        _retryable_bot_ids.discard(bot_id)
        logger.info(f"rollpig 当前 Cloud 不支持预约领取，已停止轮询: bot={bot_id}")
    except Exception as error:
        # 保留未知状态，让下一次指令或低频轮询继续尝试。
        _retryable_bot_ids.add(bot_id)
        logger.warning(f"rollpig 机会式预约投递失败: bot={bot_id} error={error}")


def _finish_delivery_task(bot_id: str, task: asyncio.Task) -> None:
    """释放后台任务引用并显式读取意外异常，避免无人接收的 task 报警。"""

    if _tasks_by_bot.get(bot_id) is task:
        _tasks_by_bot.pop(bot_id, None)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error(f"rollpig 预约后台任务异常: bot={bot_id} error={error}")


def schedule_opportunistic_delivery(
    delivery_bot_id: str,
    *,
    active_group_id: str = "",
    force: bool = False,
) -> None:
    """按 Bot 合并并节流投递检查，供用户指令与 owner 定时轮询共同调用。"""

    bot_id = str(delivery_bot_id or "")
    if active_group_id:
        # 延迟导入避免 helpers -> reservation_delivery -> reservation_flow 的初始化环。
        from .reservation_flow import clear_group_reservation_backoffs

        if clear_group_reservation_backoffs(active_group_id):
            schedule_all_owned_deliveries(force=True)
    if not bot_id or (bot_id not in _owned_bot_ids and bot_id not in _retryable_bot_ids):
        return
    if force:
        _last_check_by_bot.pop(bot_id, None)
    task = _tasks_by_bot.get(bot_id)
    if task is not None and not task.done():
        return
    task = asyncio.create_task(_deliver_if_due(bot_id))
    _tasks_by_bot[bot_id] = task
    task.add_done_callback(lambda finished, target=bot_id: _finish_delivery_task(target, finished))


def schedule_all_owned_deliveries(*, force: bool = False) -> None:
    """唤醒当前进程内已连接的 Owner Bot；资源同步成功时可绕过节流。"""

    connected_bot_ids = set(map(str, get_bots().keys()))
    for bot_id in (_owned_bot_ids | _retryable_bot_ids) & connected_bot_ids:
        if force:
            schedule_opportunistic_delivery(bot_id, force=True)
        else:
            schedule_opportunistic_delivery(bot_id)


async def shutdown_reservation_delivery_tasks() -> None:
    """NoneBot 退出时取消并收束预约后台任务。"""

    tasks = list(_tasks_by_bot.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _tasks_by_bot.clear()


@scheduler.scheduled_job(
    "interval",
    seconds=DELIVERY_CHECK_INTERVAL_SECONDS,
    id="rollpig_reservation_owner_poll",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=30,
)
async def poll_owned_reservations() -> None:
    """低频唤醒当前进程负责的 Bot，接住其他进程激活的 Cloud 预约。"""

    schedule_all_owned_deliveries()


@on_bot_connect
async def _restore_on_bot_connect(bot) -> None:
    await restore_owned_reservations(str(bot.self_id))
