# -*- coding: utf-8 -*-

# 简儿 Jianer QQ 机器人项目
# Made by 思锐工作室
# link: https://github.com/SRInternet-Studio/Jianer_QQ_bot/

from Tools.tools import *

import faulthandler

faulthandler.enable()

import contextvars
import datetime
import os
import sys
import time

os.chdir(os.path.dirname(os.path.abspath(sys.argv[0])))

from cfgr.manager import Serializers

from jianer import configurator as Configurator

Configurator.BotConfig.load_from("config.json", Serializers.JSON, "jianer-bot")

from jianer.adapters import builtins as adapters

adapters.load_configured()

from jianer import (
    common as Manager,
    events as Events,
    hyperogger as Logger,
    listener as Listener,
    run_awaitable,
    segments as Segments,
    shutdown_dispatch_runner,
)
from jianer.events import *
from jianer.utils import logic as Logic

from bot import auth_store as _bot_auth_store
from bot import feishu_bindings as _bot_feishu
from bot import misc_commands as _bot_misc_commands
from bot import plugin_state as _bot_plugin_state
from bot import protocol as _bot_protocol


config = Configurator.BotConfig.get("jianer-bot")
reminder: str = config.others["reminder"]
bot_name = config.others["bot_name"]
bot_name_en = config.others["bot_name_en"]
ONE_SLOGAN: str = config.others["slogan"]
CONFUSED_WORD: str = config.others.get(
    "confused_words",
    "不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱",
)

ROOT_User: list = config.others["ROOT_User"]
Super_User: list = []
Manage_User: list = []

logger = Logger.Logger()
logger.set_level(config.log_level)

stop_working = False

cooldowns = {}
cooldowns1 = {}
second_start = time.time()

print(" " * 114, end="\r")

PLUGIN_FOLDER = _bot_plugin_state.PLUGIN_FOLDER
if not os.path.exists(PLUGIN_FOLDER):
    os.makedirs(PLUGIN_FOLDER)

_bot_plugin_state.configure(
    config=config,
    logger=logger,
    reminder=reminder,
    bot_name=bot_name,
    bot_name_en=bot_name_en,
    one_slogan=ONE_SLOGAN,
    confused_word=CONFUSED_WORD,
    root_users=ROOT_User,
    cooldowns=cooldowns,
    cooldowns1=cooldowns1,
)
_bot_plugin_state.get_runtime().update(
    {
        "started_at": second_start,
        "emoji_repeat_enabled": True,
        "emoji_send_count": None,
        "self_service_titles": False,
        "wait_for_add_in": False,
    }
)


def load_plugins():
    result = _bot_plugin_state.reload_plugins(logger)
    return result.plugins


plugins = load_plugins()

_active_plugin_pipeline = contextvars.ContextVar(
    "active_plugin_dispatch_pipeline",
    default=None,
)


def _release_active_plugin_pipeline() -> None:
    pipeline = _active_plugin_pipeline.get()
    if pipeline is not None:
        pipeline.close()


async def execute_plugins(event, actions, message_text: str | None = None) -> bool:
    pipeline = _active_plugin_pipeline.get()
    if pipeline is not None:
        return await pipeline.dispatch_normal(
            message_text=message_text,
            run_observers=False,
        )
    return await _bot_plugin_state.dispatch_normal(
        event,
        actions,
        message_text=message_text,
        run_observers=False,
    )


async def observe_plugins(event, actions, message_text: str | None = None) -> None:
    pipeline = _active_plugin_pipeline.get()
    if pipeline is not None:
        await pipeline.observe(message_text=message_text)
        return
    await _bot_plugin_state.observe_plugins(
        event,
        actions,
        message_text=message_text,
    )


async def execute_plugin_fallback(
    event,
    actions,
    message_text: str | None = None,
) -> bool:
    pipeline = _active_plugin_pipeline.get()
    if pipeline is not None:
        return await pipeline.dispatch_fallback(message_text=message_text)
    return await _bot_plugin_state.dispatch_fallback(
        event,
        actions,
        message_text=message_text,
    )


async def restart_bot() -> None:
    _release_active_plugin_pipeline()
    report = await _bot_plugin_state.shutdown_plugins()
    if not report.completed:
        raise RuntimeError(
            "插件关闭未完成，已取消重启：" + "; ".join(report.errors)
        )
    os.execv(sys.executable, [sys.executable] + sys.argv)


def Read_Settings():
    global Super_User, Manage_User
    Super_User, Manage_User = _bot_auth_store.read_user_groups()
    logger.info(
        f"""————————————————
sys: User_Group loaded.
Super_User: {Super_User}
Manage_User: {Manage_User}
————————————————"""
    )


def build_auth_groups() -> tuple[list[str], list[str]]:
    super_users, manage_users = _bot_auth_store.read_user_groups()
    admins = [str(i) for i in (super_users + ROOT_User + manage_users)]
    supers = [str(i) for i in (super_users + ROOT_User)]
    if str(config.protocol).lower() != "feishu":
        return admins, supers
    bindings = _bot_feishu.load_feishu_bindings()
    admin_set = set(admins)
    super_set = set(supers)
    for open_id, qq_id in bindings.items():
        if qq_id in admin_set:
            admin_set.add(str(open_id))
        if qq_id in super_set:
            super_set.add(str(open_id))
    return list(admin_set), list(super_set)


def normalize_group_message_text(
    event: Events.GroupMessageEvent,
    text: str,
) -> str:
    return _bot_protocol.normalize_group_message_text(config, text)


def plugin_message_text(event: Events.MessageEvent, fallback: str) -> str:
    return _bot_protocol.plugin_message_text(
        config,
        event.message,
        fallback,
        text_segment_type=Segments.Text,
    )


def _core_is_admin(event) -> bool:
    admins, _ = build_auth_groups()
    return str(getattr(event, "user_id", "")) in admins


async def _handler_impl(
    event: Events.Event,
    actions: Listener.Actions,
) -> None:
    global stop_working
    ADMINS, SUPERS = build_auth_groups()
    super_users, manage_users = _bot_auth_store.read_user_groups()
    _bot_plugin_state.set_auth_snapshot(
        ADMINS,
        SUPERS,
        ROOT_User,
        super_users,
        manage_users,
    )
    event.time_str = (
        f"{datetime.datetime.now().hour:02}:"
        f"{datetime.datetime.now().minute:02}:"
        f"{datetime.datetime.now().second:02}"
    )

    if hasattr(event, "message") and hasattr(event, "user_id"):
        await observe_plugins(event, actions)

    if stop_working:
        user_id = getattr(event, "user_id", None)
        message = getattr(event, "message", None)
        if (
            user_id
            and message
            and str(message).startswith(reminder)
            and str(user_id) in ADMINS
        ):
            stop_working = False
            if hasattr(event, "group_id"):
                await actions.send(
                    group_id=event.group_id,
                    message=Manager.Message(
                        Segments.Text(f"{bot_name} 已从休眠中恢复 ♡=•ㅅ＜=)")
                    ),
                )
        else:
            logger.info("sys: 触发停止运行事件")
            return

    if isinstance(event, Events.PrivateMessageEvent):
        user_message = str(event.message).strip()
        if await execute_plugins(
            event,
            actions,
            message_text=plugin_message_text(event, user_message),
        ):
            return
        await execute_plugin_fallback(
            event,
            actions,
            message_text=user_message,
        )
        return

    if isinstance(event, Events.GroupMessageEvent):
        if len(event.message) <= 0:
            return
        raw_user_message = str(event.message).strip()
        user_message = normalize_group_message_text(event, raw_user_message)
        _bot_protocol.restore_feishu_mention_flag(
            config,
            event,
            raw_user_message,
        )
        plugin_text = plugin_message_text(event, user_message)
        if await execute_plugins(event, actions, message_text=plugin_text):
            return

        client = _bot_plugin_state.get_plugin_client()
        if client is not None and await client.dispatch_core_commands(
            event,
            actions,
            message_text=user_message,
        ):
            return

        order = ""
        if user_message.startswith(reminder):
            order = user_message[len(reminder):].strip()
        if user_message == f"{reminder}重启":
            if str(event.user_id) in ADMINS:
                nick = await get_user_nickname(
                    event.user_id,
                    Manager,
                    actions,
                    sender=getattr(event, "sender", None),
                )
                await actions.send(
                    user_id=ROOT_User[0],
                    message=Manager.Message(
                        Segments.Text(
                            f"用户 {nick} 在 {event.time_str} 重启QQ机器人"
                        )
                    ),
                )
                await actions.send(
                    group_id=event.group_id,
                    message=Manager.Message(
                        Segments.Text(f"正在重启{bot_name}－O－……")
                    ),
                )
                try:
                    with open("restart.temp", "w", encoding="utf-8") as handle:
                        handle.write(str(event.group_id))
                except OSError:
                    pass
                await restart_bot()
            else:
                await actions.send(
                    group_id=event.group_id,
                    message=Manager.Message(
                        Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))
                    ),
                )
            return

        if order == "休眠":
            if await _bot_misc_commands.cmd_sleep(
                actions,
                Manager,
                Segments,
                event,
                ADMINS,
                ROOT_User,
                CONFUSED_WORD,
                bot_name,
                get_user_nickname,
            ):
                stop_working = True
            return

        await execute_plugin_fallback(
            event,
            actions,
            message_text=user_message,
        )
        return


@Listener.reg
@Logic.ErrorHandler().handle_async
async def handler(event: Events.Event, actions: Listener.Actions) -> None:
    async with _bot_plugin_state.plugin_dispatch_pipeline(
        event,
        actions,
    ) as pipeline:
        token = _active_plugin_pipeline.set(pipeline)
        try:
            await _bot_plugin_state.dispatch_subscriptions(event, actions)
            return await _handler_impl(event, actions)
        finally:
            _active_plugin_pipeline.reset(token)


Read_Settings()

_client = _bot_plugin_state.get_plugin_client()
if _client is not None:
    _client.configure_core_commands(
        is_admin=_core_is_admin,
        before_reload=_release_active_plugin_pipeline,
    )

try:
    Listener.run()
finally:
    try:
        shutdown_report = run_awaitable(
            _bot_plugin_state.shutdown_plugins(),
            timeout=60,
        )
        if not shutdown_report.completed:
            logger.error(
                "插件关闭未完成：%s",
                "; ".join(shutdown_report.errors),
            )
    finally:
        shutdown_dispatch_runner(timeout=10)
