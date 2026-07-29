# -*- coding: utf-8 -*-

# 简儿 Jianer QQ 机器人项目
# Made by 思锐工作室
# link: https://github.com/SRInternet-Studio/Jianer_QQ_bot/

# import Tools functions
from Tools.tools import * 
print(title() + "\nWelcome to Jianer QQ Bot, Starting Kernal now...", end="\r") 

# import requirements
import faulthandler
faulthandler.enable()

import sys, os, threading, contextvars
import random
import re
import emoji
import time, datetime

# import framework
os.chdir(os.path.dirname(os.path.abspath(sys.argv[0])))
from cfgr.manager import Serializers
from jianer import configurator as Configurator
Configurator.BotConfig.load_from("config.json", Serializers.JSON, "jianer-bot")
from jianer.adapters import builtins as adapters
adapters.load_configured()
from jianer import (
    listener as Listener,
    events as Events,
    hyperogger as Logger,
    common as Manager,
    segments as Segments,
    run_awaitable,
    shutdown_dispatch_runner,
)
from jianer.utils import logic as Logic
from jianer.events import *

# 业务模块（bot/）：承接从 main 抽出的工具/持久化/插件加载/帮助视图等逻辑
from bot import utils as _bot_utils
from bot import protocol as _bot_protocol
from bot import feishu_bindings as _bot_feishu
from bot import help_mode as _bot_help_mode
from bot import auth_store as _bot_auth_store
from bot import plugin_state as _bot_plugin_state
from bot import broadcast as _bot_broadcast
from bot import help_view as _bot_help_view
from bot import group_commands as _bot_group_commands
from bot import event_handlers as _bot_event_handlers
from bot import admin_commands as _bot_admin_commands
from bot import plugin_ops as _bot_plugin_ops
from bot import misc_commands as _bot_misc_commands

config = Configurator.BotConfig.get("jianer-bot")
reminder: str = config.others["reminder"]
bot_name = config.others["bot_name"] #星·简
bot_name_en = config.others["bot_name_en"] #Shining girl
bot_owner = config.owner[0]
ONE_SLOGAN: str = config.others["slogan"]
CONFUSED_WORD: str = config.others.get("confused_words", 
    "不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")

ROOT_User: list = config.others["ROOT_User"]
Super_User: list = []
Manage_User: list = []
FEISHU_BIND_FILE = "feishu_bindings.json"
HELP_MODE_FILE = "help_mode_settings.json"

logger = Logger.Logger()
logger.set_level(config.log_level)
version_name = "JianerQQ机器人 版本 NEXT4Preview2"

stop_working = False
Wait_for_add_in = False

cooldowns = {}
cooldowns1 = {}
second_start = time.time()
in_timing = False
emoji_send_count: datetime = None
emoji_plus_one_off = False
self_service_titles = False

print(" " * 114, end="\r") # Staring Completed

# Plugin like
PLUGIN_FOLDER = _bot_plugin_state.PLUGIN_FOLDER
if not os.path.exists(PLUGIN_FOLDER):
    os.makedirs(PLUGIN_FOLDER)

loaded_plugins = []
disabled_plugins = []
failed_plugins = []
plugin_warnings = []
plugins_help = ""

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


# 新式插件加载器：委托给 JianerCore PluginManager
def load_plugins():
    global loaded_plugins, disabled_plugins, failed_plugins, plugin_warnings, plugins_help
    result = _bot_plugin_state.reload_plugins(logger)
    _publish_plugin_result(result)
    return result.plugins


async def reload_plugins_runtime():
    _release_active_plugin_pipeline()
    result = await _bot_plugin_state.reload_plugins_async(logger)
    _publish_plugin_result(result)
    return result.plugins


def _publish_plugin_result(result):
    global loaded_plugins, disabled_plugins, failed_plugins, plugin_warnings, plugins_help
    loaded_plugins[:] = result.loaded
    disabled_plugins[:] = _bot_plugin_state.disabled_plugins()
    failed_plugins[:] = result.failed
    plugin_warnings[:] = getattr(result, "warnings", [])
    plugins_help = _bot_plugin_state.plugin_help_text()

plugins = load_plugins() #在任何操作执行之前加载插件

# 宿主消息管线：observe -> normal -> host commands -> fallback。
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


async def execute_plugin_fallback(event, actions, message_text: str | None = None) -> bool:
    pipeline = _active_plugin_pipeline.get()
    if pipeline is not None:
        return await pipeline.dispatch_fallback(message_text=message_text)
    return await _bot_plugin_state.dispatch_fallback(
        event,
        actions,
        message_text=message_text,
    )

def has_emoji(s: str) -> bool:
    return _bot_utils.has_emoji(s)

def timing_message(actions: Listener.Actions):
    _bot_broadcast.timing_message_loop(actions, Manager, Segments, logger)

async def send_msg_all_groups(text, actions: Listener.Actions, message: Manager.Message = None):
    await _bot_broadcast.send_msg_all_groups(
        text,
        actions,
        Manager,
        Segments,
        logger,
        message=message,
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
    logger.info(f'''————————————————
sys: User_Group loaded.
Super_User: {Super_User}
Manage_User: {Manage_User}
————————————————''')

def Write_Settings(s: list, m: list) -> bool:
    global Super_User, Manage_User
    # 写入前剥离 ROOT_User，防止其被降级或重复入组
    root_set = {str(r) for r in ROOT_User}
    s = [item for item in s if item and str(item) not in root_set]
    m = [item for item in m if item and str(item) not in root_set]
    if _bot_auth_store.write_user_groups(s, m, root_users=ROOT_User):
        Super_User = s
        Manage_User = m
        return True
    return False


def load_feishu_bindings() -> dict:
    return _bot_feishu.load_feishu_bindings()


def save_feishu_bindings(bindings: dict) -> bool:
    return _bot_feishu.save_feishu_bindings(bindings)


def bind_feishu_user(
    open_id: str,
    qq_id: str,
    self_id: str | int | None = None,
) -> bool:
    return _bot_feishu.bind_feishu_user(open_id, qq_id, self_id=self_id)


def get_bound_qq(open_id: str) -> str | None:
    return _bot_feishu.get_bound_qq(open_id)


def resolve_bound_user_id(user_id: str | int | None) -> str:
    return _bot_feishu.resolve_bound_user_id(getattr(config, "protocol", ""), user_id)


def is_qq_protocol() -> bool:
    return _bot_protocol.is_qq_protocol(config)


def is_feishu_protocol() -> bool:
    return _bot_protocol.is_feishu_protocol(config)


def load_help_mode_settings() -> dict:
    return _bot_help_mode.load_help_mode_settings()


def save_help_mode_settings(settings: dict) -> bool:
    return _bot_help_mode.save_help_mode_settings(settings)


def normalize_help_mode(raw_mode: str) -> str | None:
    return _bot_help_mode.normalize_help_mode(raw_mode)


help_mode_settings = load_help_mode_settings()


def get_help_mode(user_id: str | int) -> str:
    return _bot_help_view.get_help_mode(config, help_mode_settings, user_id)


def set_help_mode(user_id: str | int, mode: str) -> bool:
    return _bot_help_view.set_help_mode(help_mode_settings, save_help_mode_settings, user_id, mode)


def normalize_group_message_text(event: Events.GroupMessageEvent, text: str) -> str:
    return _bot_protocol.normalize_group_message_text(config, text)


def build_auth_groups() -> tuple[list[str], list[str]]:
    admins = [str(i) for i in (Super_User + ROOT_User + Manage_User)]
    supers = [str(i) for i in (Super_User + ROOT_User)]
    if str(config.protocol).lower() != "feishu":
        return admins, supers
    bindings = load_feishu_bindings()
    admin_set = set(admins)
    super_set = set(supers)
    for open_id, qq_id in bindings.items():
        if qq_id in admin_set:
            admin_set.add(str(open_id))
        if qq_id in super_set:
            super_set.add(str(open_id))
    return list(admin_set), list(super_set)

async def _handler_impl(event: Events.Event, actions: Listener.Actions) -> None:
    global in_timing, bot_name, bot_name_en, reminder, config, ONE_SLOGAN, CONFUSED_WORD, stop_working, Wait_for_add_in, version_name
    global Super_User, Manage_User, ROOT_User # 全局用户组
    ADMINS, SUPERS = build_auth_groups()
    _bot_plugin_state.set_auth_snapshot(ADMINS, SUPERS, ROOT_User, Super_User, Manage_User)
    event.time_str = f"{datetime.datetime.now().hour:02}:{datetime.datetime.now().minute:02}:{datetime.datetime.now().second:02}"

    if hasattr(event, "message") and hasattr(event, "user_id"):
        await observe_plugins(event, actions)
    
    if stop_working:
        if ((user_id := getattr(event, "user_id", None)) and (message := getattr(event, "message", None)) 
            and str(message).startswith(reminder) and str(user_id) in ADMINS):
            stop_working = False
            if hasattr(event, "group_id"):
                await actions.send(
                    group_id=event.group_id,
                    message=Manager.Message(Segments.Text(f"{bot_name} 已从休眠中恢复 ♡=•ㅅ＜=)"))
                )
        else:
            logger.info("sys: 触发停止运行事件")
            return

    if not in_timing:
        Read_Settings()
        in_timing = True
        thread = threading.Thread(target=timing_message, args=(actions,))
        thread.start()
        await _bot_feishu.reconcile_pending_bindings()
        
    if isinstance(event, Events.NotifyEvent): # 优先判断自定义事件
        await _bot_event_handlers.handle_notify_poke(actions, Manager, Segments, event, config, logger)

    if isinstance(event, Events.HyperListenerStartNotify):
        await _bot_event_handlers.handle_listener_start_notify(
            actions, Manager, Segments, event,
            bot_name, bot_name_en, ONE_SLOGAN, reminder, ROOT_User)

    elif isinstance(event, Events.GroupMemberIncreaseEvent):
        if Wait_for_add_in:
            Wait_for_add_in = False
            return
        await _bot_event_handlers.handle_member_increase(actions, Manager, Segments, event, bot_name, reminder)

    elif isinstance(event, Events.GroupMemberDecreaseEvent):
        await _bot_event_handlers.handle_member_decrease(actions, Manager, Segments, event, bot_name, logger, get_user_nickname)

    elif isinstance(event, Events.GroupAddInviteEvent):
        def _set_wait(v):
            global Wait_for_add_in
            Wait_for_add_in = v
        await _bot_event_handlers.handle_group_add_invite(
            actions, Manager, Segments, event, config, logger,
            bot_name, reminder, get_user_nickname, _set_wait)
          
    # elif isinstance(event, Events.FriendAddEvent):
    #     print("sys: 同意好友")
    #     await actions.custom.set_friend_add_request(flag=event.flag, approve=True, reason="")

    elif isinstance(event, Events.PrivateMessageEvent):
        user_message, order = str(event.message).strip(), ""
        if await execute_plugins(event, actions, message_text=user_message):
            return
        event_user = await get_user_nickname(
            event.user_id, Manager, actions, sender=getattr(event, "sender", None)
        )
        state_user_id = resolve_bound_user_id(event.user_id)
        if user_message.startswith(reminder):
            order_i = user_message.find(reminder)
            if order_i != -1:
                order = user_message[order_i + len(reminder):].strip()
                order = order.strip("'\"“”‘’`")
                logger.debug(f"({event_user}) ORDER: {repr(order)}")

            if order.startswith("绑定QQ "):
                if not is_feishu_protocol():
                    await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text("该功能仅支持飞书平台。")))
                    return
                qq_id = order.replace("绑定QQ ", "", 1).strip()
                if not re.fullmatch(r"\d{5,20}", qq_id):
                    await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text("绑定失败：请输入正确的QQ号，例如：~绑定QQ 123456789")))
                elif bind_feishu_user(
                    str(event.user_id),
                    qq_id,
                    self_id=getattr(event, "self_id", None),
                ):
                    await _bot_feishu.reconcile_feishu_binding(
                        str(event.user_id),
                        qq_id,
                        self_id=getattr(event, "self_id", None),
                    )
                    await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(f"绑定成功：当前飞书账号已绑定 QQ {qq_id}")))
                else:
                    await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text("绑定失败：写入绑定数据时出错")))
                return
            elif "我的绑定" == order:
                if not is_feishu_protocol():
                    await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text("该功能仅支持飞书平台。")))
                    return
                qq_id = get_bound_qq(str(event.user_id))
                msg = f"当前绑定QQ：{qq_id}" if qq_id else "当前未绑定QQ。请发送：~绑定QQ 你的QQ号"
                await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(msg)))
                return
            elif "帮助" == order or "用户帮助" == order:
                content = help_message(event)
                await send_help_visual(actions=actions, event=event, content=content)
                return
            elif order.startswith("设置帮助模式"):
                if not is_qq_protocol():
                    await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text("该功能仅支持QQ平台（OneBot/Milky）。")))
                    return
                mode_text = order.replace("设置帮助模式", "", 1).strip()
                mode = normalize_help_mode(mode_text)
                if mode is None:
                    await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(f"参数错误。示例：{reminder}设置帮助模式 图片 或 {reminder}设置帮助模式 文本")))
                    return
                if set_help_mode(state_user_id, mode):
                    await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(f"已切换帮助模式为【{mode}】。")))
                else:
                    await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text("设置失败：无法写入帮助模式配置。")))
                return

        private_input = order if order else user_message
        await execute_plugin_fallback(
            event,
            actions,
            message_text=private_input,
        )
        return
            
    elif isinstance(event, Events.GroupMessageEvent):
        global second_start
        global emoji_plus_one_off

        if len(event.message) <= 0:
            return  # 只在函数中有效
        
        raw_user_message = str(event.message).strip()
        user_message = normalize_group_message_text(event, raw_user_message)
        host_message = user_message
        plugin_dispatched = False
        like_plugin = _bot_plugin_state.get_plugin_module(
            "jianerbot-plugin-like"
        )
        early_plugin_commands = getattr(like_plugin, "EARLY_COMMANDS", ())
        if raw_user_message in early_plugin_commands:
            plugin_dispatched = True
            if await execute_plugins(
                event,
                actions,
                message_text=raw_user_message,
            ):
                return
        feishu_mode = str(config.protocol).lower() == "feishu"
        feishu_mentioned = bool(getattr(event, "is_mentioned", False))
        feishu_mention_like = feishu_mentioned or (feishu_mode and raw_user_message.startswith("@"))
        # QQ 协议：检测是否 @ 了机器人本身（通过 Segments.At）
        qq_mentioned_me = False
        if not feishu_mode:
            for _seg in event.message:
                if isinstance(_seg, Segments.At) and str(getattr(_seg, "qq", "")) == str(event.self_id):
                    qq_mentioned_me = True
                    break
        if str(config.protocol).lower() == "feishu" and raw_user_message != user_message:
            logger.debug(f"Feishu 消息标准化: {repr(raw_user_message)} -> {repr(user_message)}")
        order = ""
        
        user_message_for_cmd = user_message
        if feishu_mode and feishu_mention_like and user_message and not user_message.startswith(reminder):
            user_message_for_cmd = f"{reminder}{user_message}"

        if user_message_for_cmd.startswith(reminder) or (feishu_mode and reminder in user_message_for_cmd):
            order_i = user_message_for_cmd.find(reminder)
            if order_i != -1:
                order = user_message_for_cmd[order_i + len(reminder):].strip()

        if feishu_mode and feishu_mention_like and not order:
            order = user_message.strip().strip("'\"“”‘’`")

        user_message = user_message_for_cmd

        if not plugin_dispatched:
            if await execute_plugins(event, actions, message_text=user_message):
                return

        event_user = await get_user_nickname(
            event.user_id, Manager, actions, sender=getattr(event, "sender", None)
        )
        if not event_user:
            event_user = str(event.user_id)
        state_user_id = resolve_bound_user_id(event.user_id)
        if order:
            log_label = (
                "Feishu Mention ORDER Fallback"
                if feishu_mode
                and feishu_mention_like
                and not user_message_for_cmd.startswith(reminder)
                else "ORDER"
            )
            logger.debug(f"({event_user}) {log_label}: {repr(order)}")

        # 普通插件未处理后，宿主才处理裸 @ 帮助与轻量消息行为。
        if qq_mentioned_me:
            _has_text = any(
                isinstance(_seg, Segments.Text) and str(_seg).strip()
                for _seg in event.message
            )
            if not _has_text:
                content = help_message(event)
                if content:
                    await send_help_visual(
                        actions=actions,
                        event=event,
                        content=content,
                        reply_message_id=event.message_id,
                    )
                return

        if host_message == "ping":
            logger.debug(str(event.user_id))
            await _bot_group_commands.cmd_ping(
                actions,
                Manager,
                Segments,
                event,
            )
            return
        elif (
            f"{bot_name}真棒" in host_message
            and str(reminder) not in host_message
        ):
            try:
                compliments: list = config.others.get(
                    "compliment",
                    ["谢谢夸奖 (◍•ᴗ•◍)❤"],
                )
                message = str(random.choice(compliments))
                await actions.send(
                    group_id=event.group_id,
                    message=Manager.Message(Segments.Text(message)),
                )
            except Exception:
                logger.warning("不接受夸赞")
            return

        global emoji_send_count
        if has_emoji(host_message) and not emoji_plus_one_off:
            if (
                emoji_send_count is None
                or datetime.datetime.now() - emoji_send_count
                > datetime.timedelta(seconds=15)
            ):
                await actions.send(
                    group_id=event.group_id,
                    message=Manager.Message(Segments.Text(host_message)),
                )
                emoji_send_count = datetime.datetime.now()
            else:
                logger.debug(
                    "emoji +1 延迟 "
                    f"{abs(datetime.datetime.now() - emoji_send_count)} s"
                )

        if order.startswith("绑定QQ "):
            if not is_feishu_protocol():
                await actions.send(
                    group_id=event.group_id,
                    message=Manager.Message(
                        Segments.Reply(event.message_id),
                        Segments.Text("该功能仅支持飞书平台。"),
                    ),
                )
                return
            qq_id = order.replace("绑定QQ ", "", 1).strip()
            if not re.fullmatch(r"\d{5,20}", qq_id):
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text("绑定失败：请输入正确的QQ号，例如：~绑定QQ 123456789")))
            elif bind_feishu_user(
                str(event.user_id),
                qq_id,
                self_id=getattr(event, "self_id", None),
            ):
                await _bot_feishu.reconcile_feishu_binding(
                    str(event.user_id),
                    qq_id,
                    self_id=getattr(event, "self_id", None),
                )
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(f"绑定成功：当前飞书账号已绑定 QQ {qq_id}")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text("绑定失败：写入绑定数据时出错")))
            return
        elif "我的绑定" == order:
            if not is_feishu_protocol():
                await actions.send(
                    group_id=event.group_id,
                    message=Manager.Message(
                        Segments.Reply(event.message_id),
                        Segments.Text("该功能仅支持飞书平台。"),
                    ),
                )
                return
            qq_id = get_bound_qq(str(event.user_id))
            msg = f"当前绑定QQ：{qq_id}" if qq_id else "当前未绑定QQ。请发送：~绑定QQ 你的QQ号"
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(msg)))
            return

        if f"{reminder}重启" == user_message:
            if str(event.user_id) in ADMINS:
                r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions, sender=getattr(event, "sender", None))} 在 {event.time_str} 重启QQ机器人'''
                await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin))) #管理员操作通知ROOT用户
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"正在重启{bot_name}－O－……")))

                try:
                    with open("restart.temp", "w" ,encoding="utf-8") as f:
                        f.write(str(event.group_id))
                        f.close()
                except:
                    pass

                await restart_bot()
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
        
        elif f"{reminder}重载插件" == user_message:
            global plugins
            new_plugins = await _bot_plugin_ops.cmd_reload_plugins(
                actions, Manager, Segments, event,
                ADMINS, CONFUSED_WORD, bot_name, bot_name_en, ONE_SLOGAN, reminder,
                reload_plugins_runtime)
            if new_plugins is not None:
                plugins = new_plugins
        elif f"{reminder}禁用插件 " in user_message:
            new_plugins = await _bot_plugin_ops.cmd_disable_plugin(
                actions, Manager, Segments, event, user_message,
                ADMINS, CONFUSED_WORD, bot_name, bot_name_en, ONE_SLOGAN, reminder,
                reload_plugins_runtime)
            if new_plugins is not None:
                plugins = new_plugins

        elif f"{reminder}启用插件 " in user_message:
            new_plugins = await _bot_plugin_ops.cmd_enable_plugin(
                actions, Manager, Segments, event, user_message,
                ADMINS, CONFUSED_WORD, bot_name, bot_name_en, ONE_SLOGAN, reminder,
                reload_plugins_runtime)
            if new_plugins is not None:
                plugins = new_plugins

        elif "列出黑名单" == order:
            await _bot_misc_commands.cmd_list_blacklist(actions, Manager, Segments, event, ADMINS, CONFUSED_WORD, bot_name)
        elif "添加黑名单 " in order:
            await _bot_misc_commands.cmd_add_blacklist(actions, Manager, Segments, event, order, ADMINS, ROOT_User, CONFUSED_WORD, bot_name, get_user_nickname)
        elif "删除黑名单 " in order:
            await _bot_misc_commands.cmd_remove_blacklist(actions, Manager, Segments, event, order, ADMINS, ROOT_User, CONFUSED_WORD, bot_name, get_user_nickname)
            
        elif "删除管理 " in order:
            await _bot_admin_commands.cmd_del_admin(
                actions, Manager, Segments, event, order,
                SUPERS, ROOT_User, Super_User, Manage_User,
                CONFUSED_WORD, bot_name, bot_name_en, ONE_SLOGAN, reminder,
                Write_Settings, get_user_nickname)

        elif "管理 " in order:
            await _bot_admin_commands.cmd_add_admin(
                actions, Manager, Segments, event, order,
                SUPERS, ROOT_User, Super_User, Manage_User,
                CONFUSED_WORD, bot_name, bot_name_en, ONE_SLOGAN, reminder,
                Write_Settings, get_user_nickname, logger)

        elif "让我访问" in order:
            await _bot_admin_commands.cmd_list_admins(
                actions, Manager, Segments, event,
                ADMINS, ROOT_User, Super_User, Manage_User,
                CONFUSED_WORD, bot_name, bot_name_en, ONE_SLOGAN, reminder,
                get_user_nickname_with_userid)

        elif "插件视角" in order:
            await _bot_group_commands.cmd_plugin_view(actions, Manager, Segments, event, bot_name, bot_name_en, loaded_plugins, disabled_plugins, failed_plugins, plugin_warnings)
        elif order.startswith("设置帮助模式"):
            if not is_qq_protocol():
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text("该功能仅支持QQ平台（OneBot/Milky）。")))
            else:
                mode_text = order.replace("设置帮助模式", "", 1).strip()
                mode = normalize_help_mode(mode_text)
                if mode is None:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(f"参数错误。示例：{reminder}设置帮助模式 图片 或 {reminder}设置帮助模式 文本")))
                elif set_help_mode(state_user_id, mode):
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(f"已切换你的帮助模式为【{mode}】。")))
                else:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text("设置失败：无法写入帮助模式配置。")))
        elif "用户帮助" == order:
            content = help_message(event)
            await send_help_visual(actions=actions, event=event, content=content, reply_message_id=event.message_id)
            
        elif "帮助" == order:
            if str(event.user_id) in ADMINS:
                content = _bot_help_view.build_admin_help(config, bot_name, reminder, str(event.user_id) in SUPERS)
            else:
                content = help_message(event)
            await send_help_visual(actions=actions, event=event, content=content)

        elif feishu_mode and feishu_mention_like and not order:
            has_valid_content = False
            for item in event.message[1:]:
                if isinstance(item, Segments.Text):
                    if normalize_group_message_text(event, str(item)).strip():
                        has_valid_content = True
                        break
                else:
                    has_valid_content = True

            content = help_message(event) if not has_valid_content else f'''你要询问什么呢？嘻嘻(●'◡'●)
和我聊天不需要@我哟(＾Ｕ＾)ノ~
直接在你想对{bot_name}想说的话前面加上 {reminder} 就行啦'''
            if not has_valid_content:
                await send_help_visual(actions=actions, event=event, content=content, reply_message_id=event.message_id)
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(content)))

        elif "关于" == order:
            await _bot_group_commands.cmd_about(actions, Manager, Segments, event, bot_name, bot_name_en, ONE_SLOGAN, version_name)

        elif "群发黑名单" == order:
            await _bot_group_commands.cmd_broadcast_blacklist_menu(actions, Manager, Segments, event, bot_name, bot_name_en, reminder)

        elif "休眠" == order:
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

        elif f"{reminder}感知" in user_message:
            await _bot_misc_commands.cmd_status(actions, Manager, Segments, event, ADMINS, CONFUSED_WORD, bot_name, bot_name_en, ONE_SLOGAN, second_start)
      
        elif f"{reminder}生成" == user_message:
            await _bot_group_commands.cmd_generate_placeholder(actions, Manager, Segments, event)
            
        elif "修改 " in order:
            await _bot_misc_commands.cmd_modify_timing(actions, Manager, Segments, event, order, ADMINS, ROOT_User, CONFUSED_WORD, bot_name, reminder, get_user_nickname)

        elif f"{reminder}群发" in user_message:
            await _bot_misc_commands.cmd_broadcast_msg(actions, Manager, Segments, event, order, user_message, ADMINS, ROOT_User, CONFUSED_WORD, bot_name, reminder, logger, send_msg_all_groups, get_user_nickname)
                
        elif f"{reminder}生草" == user_message:
            await _bot_group_commands.cmd_grass(actions, Manager, Segments, event)

        elif "zzzz...涩图...嘿嘿..." in user_message:
            try:
                acg_plugin = _bot_plugin_state.get_plugin_module(
                    "jianerbot-plugin-generate-acg"
                )
                generate_acg = getattr(acg_plugin, "generate_acg", None)
                if not callable(generate_acg):
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"{bot_name}需要 GenerateFromACG 插件才能生成好看的涩图哦 (੭ु ˃̶͈̀ ω ˂̶͈́)੭ु⁾⁾")))
                else:
                    await generate_acg("随机", event, actions)
            except:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"{bot_name}需要 GenerateFromACG 插件才能生成好看的涩图哦 (੭ु ˃̶͈̀ ω ˂̶͈́)੭ु⁾⁾")))
                
        elif "取消冷静 " in order:
            await _bot_misc_commands.cmd_uncalm(actions, Manager, Segments, event, order, ADMINS, CONFUSED_WORD, bot_name, reminder)

        elif "冷静" in order:
            await _bot_misc_commands.cmd_calm(actions, Manager, Segments, event, order, ADMINS, CONFUSED_WORD, bot_name, reminder)

        elif "送飞机票" in order:
            await _bot_misc_commands.cmd_kick(actions, Manager, Segments, event, order, ADMINS, ROOT_User, CONFUSED_WORD, bot_name, get_user_nickname)
        
        elif f"{reminder}退出本群" == user_message:
            await _bot_misc_commands.cmd_leave_group(actions, Manager, Segments, event, SUPERS, ROOT_User, CONFUSED_WORD, bot_name, get_user_nickname)
        elif "撤回" == user_message:
            await _bot_misc_commands.cmd_recall(actions, Manager, Segments, event, ADMINS, CONFUSED_WORD, bot_name)

        elif f"{reminder}表情复述" == user_message:
            emoji_plus_one_off = not emoji_plus_one_off
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(
                "开启表情复述成功！" if not emoji_plus_one_off else "关闭表情复述成功！")))

        elif f"{reminder}更改分配头衔开放状态" == user_message:
            global self_service_titles
            if str(event.user_id) in SUPERS:
                self_service_titles = not self_service_titles
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(
                    "分配头衔功能已开放！" if self_service_titles else "分配头衔功能已取消开放！")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))

        elif "给他人分配头衔" in order:
            await _bot_misc_commands.cmd_assign_title_other(actions, Manager, Segments, event, order, SUPERS, CONFUSED_WORD, bot_name, logger)

        elif f"分配头衔 " in order:
            await _bot_misc_commands.cmd_assign_title_self(actions, Manager, Segments, event, order, SUPERS, self_service_titles)
        else:
            # QQ 群聊只接受前缀触发 AI，@机器人加文本不能转入 fallback。
            if qq_mentioned_me:
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
            return await _handler_impl(event, actions)
        finally:
            _active_plugin_pipeline.reset(token)


def help_message(event) -> str:
    return _bot_help_view.build_help_message(config, Events, event, bot_name, reminder, plugins_help)


async def send_help_visual(actions, event, content: str, reply_message_id: str = None):
    await _bot_help_view.send_help_visual(
        config, Events, Manager, Segments, help_mode_settings,
        bot_name, logger, actions, event, content, reply_message_id,
        user_id_override=resolve_bound_user_id(getattr(event, "user_id", None))
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

