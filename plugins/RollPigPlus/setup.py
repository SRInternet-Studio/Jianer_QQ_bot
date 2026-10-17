"""JianerCore plugin entry for RollPigPlus（今天是什么小猪 Plus）。

导入本模块即完成两件事：``rollpig.handlers`` 注册全部 Alconna 命令，
``rollpig.jobs`` / ``rollpig.reservation_delivery`` 注册定时任务与生命周期回调。
"""

from __future__ import annotations

from jianer import events
from jianer.plugins import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="jianerbot-plugin-rollpig-plus",
    description="今天是什么小猪 Plus：抽猪、图鉴收集、烤群友、烤箱续火与猪圈日报。",
    usage=(
        "{reminder}今日小猪 / 今天是什么小猪 —> 抽取今天的命运之猪\n"
        "{reminder}随机小猪 [数量] / {reminder}找猪 <关键词> —> PigHub 随机与搜图\n"
        "{reminder}明日小猪 / {reminder}昨日小猪 —> 运势与昨日回顾\n"
        "{reminder}今日烤猪 / {reminder}烤群友 [@成员] / {reminder}随机烤群友 —> 烧烤玩法\n"
        "{reminder}烤箱续火 —> 活跃小猪发起补货投票，恢复本群烧烤次数\n"
        "{reminder}我的猪圈 / {reminder}小猪图鉴 [页码] / {reminder}本周小猪 —> 收集与统计\n"
        "{reminder}小猪投稿 —> 前往投稿平台提交创意与差分\n"
        "{reminder}猪圈日报 / {reminder}小猪日报 开启/关闭/状态 —> 日报查询与推送开关\n"
        "{reminder}同步小猪资源 —> 超级用户手动同步小猪资源"
    ),
    requires={"jianerbot-plugin-alconna"},
)

# 导入即注册：命令、定时任务、生命周期回调。
from plugins.RollPigPlus.rollpig import config as _config  # noqa: E402,F401
from plugins.RollPigPlus.rollpig import jobs as _jobs  # noqa: E402,F401
from plugins.RollPigPlus.rollpig import reservation_delivery as _reservation  # noqa: E402,F401
from plugins.RollPigPlus.rollpig.handlers import (  # noqa: E402,F401
    collection as _collection_commands,
    control as _control_commands,
    refill as _refill_commands,
    roast as _roast_commands,
    roll as _roll_commands,
)

from plugins.RollPigPlus.host import (  # noqa: E402
    bot_for,
    run_bot_connect_hooks,
    run_shutdown_hooks,
    run_startup_hooks,
    scheduler,
)
from plugins.RollPigPlus.host import logger  # noqa: E402

SUPPORTED_PROTOCOLS = frozenset({"onebot", "milky"})

_started = False
_connected: set[str] = set()


def _protocol(event, actions) -> str:
    for value in (
        getattr(actions, "protocol", None),
        getattr(event, "protocol", None),
    ):
        if value:
            return str(value).strip().lower()
    return ""


def _supported(event, actions) -> bool:
    return _protocol(event, actions) in SUPPORTED_PROTOCOLS


async def _ensure_started(event, actions) -> None:
    """首次拿到可用连接时启动定时任务与上游 startup 钩子。"""

    global _started
    bot = bot_for(actions, getattr(event, "self_id", None))
    if _started:
        return
    _started = True
    await scheduler.start()
    await run_startup_hooks()
    key = str(bot.self_id)
    if key not in _connected:
        _connected.add(key)
        await run_bot_connect_hooks(bot)


async def on_message_observe(event, actions) -> None:
    """登记当前连接；只做轻量动作，不阻塞消息派发。"""

    if not _supported(event, actions):
        return
    try:
        await _ensure_started(event, actions)
    except Exception as error:  # pragma: no cover - 启动失败不影响宿主
        logger.error(f"rollpig 启动失败: error={error}")


async def on_message(event, actions) -> bool:
    """非命令消息钩子：回复预约通知加入。"""

    if not _supported(event, actions):
        return False
    try:
        return await _roast_commands.handle_message_hook(event, actions)
    except Exception as error:
        logger.exception(f"rollpig 消息钩子失败: error={error}")
        return False


async def _listener_started(event, actions) -> None:
    if not _supported(event, actions):
        return
    try:
        await _ensure_started(event, actions)
    except Exception as error:  # pragma: no cover
        logger.error(f"rollpig 监听器启动失败: {error}")


async def _notice_event(event, actions) -> None:
    """reaction / notify 通知：续标识投票的旁路验票。"""

    if not _supported(event, actions):
        return
    try:
        await _refill_commands.handle_notice(event, actions)
    except Exception as error:  # pragma: no cover - 旁路事件必须静默
        logger.warning(f"rollpig 通知事件处理失败: {error}")


def setup(client, manager) -> None:
    """订阅宿主生命周期与通知事件。"""

    client.subscribe(_listener_started, events.HyperListenerStartNotify)
    client.subscribe(_notice_event, events.MessageReactionEvent)
    client.subscribe(_notice_event, events.NotifyEvent)


async def shutdown(client, manager) -> None:
    global _started, _connected
    _started = False
    _connected = set()
    await scheduler.shutdown()
    await run_shutdown_hooks()
