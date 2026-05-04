# -*- coding: utf-8 -*-

# 简儿 Jianer QQ 机器人项目
# Made by 思锐工作室
# link: https://github.com/SRInternet-Studio/Jianer_QQ_bot/

# import Tools functions
from Tools.tools import * 
from Tools.capture_screenshot import capture_screenshot
from Tools.suffix_manager import SuffixManager
from Tools.jianer_memory import JianerMemoryService
from Tools.Sanitizer_Tools import sanitize_for_tts
from AI_bot.AIKernal import AIKernal
from AI_bot.ContextManager import ContextManager
print(title() + "\nWelcome to Jianer QQ Bot, Starting Kernal now...", end="\r") 

# from Tools.GoogleAI import genai, Context, Parts, Roles, Schema
# from Tools.SearchOnline import network_gpt as SearchOnline
# from Tools.deepseek import dsr114 as deepseek
import Tools.ARC_AI as ARC_AI
import prerequisites.prerequisite as presets_tool

# import requirements
import faulthandler
faulthandler.enable()

import sys, os, asyncio, traceback, threading
import importlib.util   
import inspect
import random
import uuid, re
import json
import emoji
import time, datetime

# import framework
os.chdir(os.path.dirname(os.path.abspath(sys.argv[0])))
from hyperot import configurator as Configurator
Configurator.ensure_config_manager(file="config.json")
from hyperot import listener as Listener, events as Events, hyperogger as Logger, common as Manager, segments as Segments
from hyperot.utils import logic as Logic
from hyperot.events import *

# 业务模块（bot/）：承接从 main 抽出的工具/持久化/插件加载/帮助视图等逻辑
from bot import utils as _bot_utils
from bot import protocol as _bot_protocol
from bot import feishu_bindings as _bot_feishu
from bot import help_mode as _bot_help_mode
from bot import auth_store as _bot_auth_store
from bot import plugin_loader as _bot_plugin_loader
from bot import plugin_runner as _bot_plugin_runner
from bot import broadcast as _bot_broadcast
from bot import help_view as _bot_help_view
from bot import group_commands as _bot_group_commands
from bot import event_handlers as _bot_event_handlers
from bot import admin_commands as _bot_admin_commands
from bot import plugin_ops as _bot_plugin_ops
from bot import memory_commands as _bot_memory_commands
from bot import misc_commands as _bot_misc_commands

config = Configurator.cm.get_cfg()
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
generating = False
emoji_send_count: datetime = None
emoji_plus_one_off = False
self_service_titles = False

# AI Settings
EnableNetwork = config.others.get("default_mode", "openai_normal")
MAX_MESSAGE_LENGTH = int(config.others.get("max_message_length", 3))
user_lists = {}
private_ai_modes = {}
sys_prompt = ""
cmc = ContextManager()
# class Tools:
#     pass

# class ContextManager:
# ...
# cmc = ContextManager() # Gemini 的上下文管理器
# tools = []
suffix_manager = SuffixManager()

memory_db_path = config.others.get("memory_db_path", "jianer_memory.db")
memory_mode = config.others.get("memory_mode", EnableNetwork)
memory_enabled_default = bool(config.others.get("memory_enabled_default", True))
memory_interval_default = int(config.others.get("memory_interval_seconds_default", 6 * 3600))
memory_topk = int(config.others.get("memory_topk", 6))
memory_scheduler_tick = int(config.others.get("memory_scheduler_tick_seconds", 30))
memory_min_new_rows = int(config.others.get("memory_min_new_rows_to_generate", 12))
memory_max_raw_rows = int(config.others.get("memory_max_raw_rows_per_generation", 200))
memory_max_chars = int(config.others.get("memory_max_chars_per_generation", 12000))
memory_cleanup_interval = int(config.others.get("memory_cleanup_interval_seconds", 6 * 3600))
memory_cleanup_keep_days = int(config.others.get("memory_cleanup_keep_days", 30))
memory_cleanup_min_weight = float(config.others.get("memory_cleanup_min_weight", 0.25))
memory_cleanup_keep_max_rows = int(config.others.get("memory_cleanup_keep_max_rows", 3000))
memory_global_optimize_interval = int(config.others.get("memory_global_optimize_interval_seconds", 24 * 3600))
memory_global_candidate_limit = int(config.others.get("memory_global_candidate_limit", 200))

memory_service = JianerMemoryService(
    db_path=memory_db_path,
    memory_mode=memory_mode,
    default_enabled=memory_enabled_default,
    default_interval_seconds=memory_interval_default,
    scheduler_tick_seconds=memory_scheduler_tick,
    max_raw_rows_per_generation=memory_max_raw_rows,
    min_new_rows_to_generate=memory_min_new_rows,
    max_chars_per_generation=memory_max_chars,
    cleanup_interval_seconds=memory_cleanup_interval,
    cleanup_keep_days=memory_cleanup_keep_days,
    cleanup_min_weight=memory_cleanup_min_weight,
    cleanup_keep_max_rows=memory_cleanup_keep_max_rows,
    global_optimize_interval_seconds=memory_global_optimize_interval,
    global_candidate_limit=memory_global_candidate_limit,
)

preset_template_cache = {}
memory_service.set_bot_name(bot_name)

gptsovitsoff = False

print(" " * 114, end="\r") # Staring Completed

# Plugin like
PLUGIN_FOLDER = _bot_plugin_loader.PLUGIN_FOLDER
if not os.path.exists(PLUGIN_FOLDER):
    os.makedirs(PLUGIN_FOLDER)

loaded_plugins = []
disabled_plugins = []
failed_plugins = []
plugins_help = ""

# 配置文件名
CONFIG_FILE = presets_tool.CONFIG_FILE
# 预设文件存放目录
PRESET_DIR = presets_tool.PRESET_DIR
# 默认预设名称
NORMAL_PRESET = presets_tool.NORMAL_PRESET

# 插件加载器：委托给 bot.plugin_loader（保留原入口名 load_plugins）
def load_plugins():
    global loaded_plugins, disabled_plugins, failed_plugins, plugins_help
    result = _bot_plugin_loader.load_plugins(config, logger)
    loaded_plugins[:] = result.loaded
    disabled_plugins[:] = result.disabled
    failed_plugins[:] = result.failed
    plugins_help = result.help_text
    return result.plugins

plugins = load_plugins() #在任何操作执行之前加载插件

# 插件运行器：委托给 bot.plugin_runner
async def execute_plugins(isAny: bool, **main_context) -> bool:
    return await _bot_plugin_runner.execute_plugins(plugins, reminder, logger, isAny, **main_context)

def load_blacklist():
    return _bot_utils.load_blacklist()

def has_emoji(s: str) -> bool:
    return _bot_utils.has_emoji(s)

def timing_message(actions: Listener.Actions):
    _bot_broadcast.timing_message_loop(actions, Manager, Segments, suffix_manager, logger)

async def send_msg_all_groups(text, actions: Listener.Actions, message: Manager.Message = None):
    await _bot_broadcast.send_msg_all_groups(text, actions, Manager, Segments, suffix_manager, logger, message=message)


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
    s = [item for item in s if item]
    m = [item for item in m if item]
    if _bot_auth_store.write_user_groups(s, m):
        Super_User = s
        Manage_User = m
        return True
    return False


def load_feishu_bindings() -> dict:
    return _bot_feishu.load_feishu_bindings()


def save_feishu_bindings(bindings: dict) -> bool:
    return _bot_feishu.save_feishu_bindings(bindings)


def bind_feishu_user(open_id: str, qq_id: str) -> bool:
    return _bot_feishu.bind_feishu_user(open_id, qq_id)


def get_bound_qq(open_id: str) -> str | None:
    return _bot_feishu.get_bound_qq(open_id)


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

@Listener.reg
@Logic.ErrorHandler().handle_async
async def handler(event: Events.Event, actions: Listener.Actions) -> None:
    global in_timing, bot_name, bot_name_en, reminder, config, ONE_SLOGAN, CONFUSED_WORD, stop_working, Wait_for_add_in, version_name
    global Super_User, Manage_User, ROOT_User # 全局用户组
    global cmc, user_lists, sys_prompt, EnableNetwork, private_ai_modes # AI对话所必须
    ADMINS, SUPERS = build_auth_groups()
    AIbot = AIKernal(actions, config, bot_name, reminder)
    event.time_str = f"{datetime.datetime.now().hour:02}:{datetime.datetime.now().minute:02}:{datetime.datetime.now().second:02}"

    try:
        if hasattr(event, "message") and hasattr(event, "user_id"):
            memory_service.capture_message_event(event, Segments)
    except Exception:
        pass
    
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
        memory_service.start()
        
    # 执行永久加载插件
    local_vars = globals().copy()
    local_vars.update(locals().copy())
    if await execute_plugins(True, **local_vars):
        return  # 只传递 event 作为位置参数
    
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
        event_user = await get_user_nickname(event.user_id, Manager, actions)
        user_message, order = str(event.message).strip(), ""
        sys_prompt = presets_tool.gen_presets(event.user_id, bot_name, bot_name_en, event_user)
        presets = presets_tool.read_presets()
        private_ai = private_ai_modes.get(str(event.user_id), EnableNetwork)
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
                elif bind_feishu_user(str(event.user_id), qq_id):
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
                if set_help_mode(event.user_id, mode):
                    await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(f"已切换帮助模式为【{mode}】。")))
                else:
                    await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text("设置失败：无法写入帮助模式配置。")))
                return
            elif f"ai管理菜单" == order:
                ais = ARC_AI.list_available_ais()
                current_ai_friendly = ARC_AI.get_current_ai_name(private_ai)
                ai_list_str = "\n".join([f"- {friendly} (代码: {name})" for name, friendly in ais.items()])
                menu = f'''{bot_name} {bot_name_en} - 私聊AI管理菜单
————————————————————
当前使用的AI: {current_ai_friendly} (代码: {private_ai})

可用AI列表:
{ai_list_str}

指令:
{reminder}切换AI [AI代码] —> 仅切换你自己的私聊AI
例如: {reminder}切换AI gemini
'''
                await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(menu)))
                return
            elif order.startswith("切换AI "):
                target_ai = order.replace("切换AI ", "", 1).strip()
                available_ais = ARC_AI.list_available_ais()
                if target_ai in available_ais:
                    private_ai_modes[str(event.user_id)] = target_ai
                    private_ai = target_ai
                    friendly_name = available_ais[target_ai]
                    await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(f"已切换你的私聊AI为: {friendly_name} ({target_ai})")))
                else:
                    await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(f"找不到AI配置: {target_ai}，请检查代码拼写。")))
                return
            elif "角色扮演" == order:
                prerequisites_info = f"""{bot_name} {bot_name_en} - 角色扮演后台
————————————————————
{presets_tool.list_presets(presets, presets_tool.current_preset, reminder)}

发送相应的关键词，{bot_name}会尽力扮演不同角色和你交流哒！⌯>ᴗoᴗ⌯ .ᐟ.ᐟ
————————————————————
若您要管理这些角色，请前往群聊中发送相关指令哦o((>ω< ))o"""

                await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(prerequisites_info)))
                return
            else:
                presets, p_info, is_changed = presets_tool.change_presets(presets, order, event)
                if is_changed:
                    # 清除ContextManager和user_lists中的单个用户上下文
                    cmc.del_context(event.user_id, event.group_id)
                    await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(p_info)))
                    return

        private_input = order if order else user_message
        response_stream = ARC_AI.get_response_stream(
            private_ai,
            private_input,
            user_lists,
            event.user_id,
            sys_prompt,
            []
        )
        private_result = ""
        async for partial, r_type in response_stream:
            if r_type != "message":
                user_lists = partial
                continue
            private_result += str(partial)
        private_result = private_result.rstrip()
        if not private_result:
            private_result = "（无可用回复）"
        await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(private_result)))
            
    elif isinstance(event, Events.GroupMessageEvent):
        global second_start
        global generating
        global CONFIG_FILE, PRESET_DIR, NORMAL_PRESET
        global emoji_plus_one_off

        event_user = await get_user_nickname(event.user_id, Manager, actions)
        if event_user :
            event_user = event_user
        else:
            event_user = str(event.user_id)
                    
        # 初始化预设
        sys_prompt = presets_tool.gen_presets(event.user_id, bot_name, bot_name_en, event_user)
        base_sys_prompt = sys_prompt
        presets = presets_tool.read_presets()
        try:
            global preset_template_cache
            templates = {}
            for preset_id, preset_data in (presets or {}).items():
                preset_path = os.path.join(PRESET_DIR, str(preset_data.get("path", "") or ""))
                if not preset_path or not os.path.exists(preset_path):
                    continue
                mtime = os.path.getmtime(preset_path)
                cached = preset_template_cache.get(preset_id)
                if not cached or cached[0] != mtime:
                    with open(preset_path, "r", encoding="utf-8") as f:
                        preset_template_cache[preset_id] = (mtime, f.read())
                templates[preset_id] = preset_template_cache[preset_id][1]
            templates["default"] = templates.get(presets_tool.NORMAL_PRESET, base_sys_prompt)
            memory_service.set_bot_name(bot_name)
            memory_service.set_preset_templates(templates)
        except Exception:
            pass
        
        if len(event.message) <= 0:
            return  # 只在函数中有效
        
        raw_user_message = str(event.message).strip()
        user_message = normalize_group_message_text(event, raw_user_message)
        feishu_mode = str(config.protocol).lower() == "feishu"
        feishu_mentioned = bool(getattr(event, "is_mentioned", False))
        feishu_mention_like = feishu_mentioned or (feishu_mode and raw_user_message.startswith("@"))
        if str(config.protocol).lower() == "feishu" and raw_user_message != user_message:
            logger.debug(f"Feishu 消息标准化: {repr(raw_user_message)} -> {repr(user_message)}")
        order = ""

        if "ping" == user_message:
            logger.debug(str(event.user_id))
            await _bot_group_commands.cmd_ping(actions, Manager, Segments, event, suffix_manager)
            
        elif f"{bot_name}真棒" in user_message and str(reminder) not in user_message:
            try:
                compliments: list = config.others.get("compliment", ["谢谢夸奖 (◍•ᴗ•◍)❤"])
                m = str(compliments[random.randint(0, len(compliments))])
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(suffix_manager.process_text(m, event.user_id))))
            except:
                logger.warning("不接受夸赞")

        global emoji_send_count
        if has_emoji(user_message) and not emoji_plus_one_off:
            if emoji_send_count is None or datetime.datetime.now() - emoji_send_count > datetime.timedelta(seconds=15):
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(user_message)))
                emoji_send_count = datetime.datetime.now()
            else:
                logger.debug(f"emoji +1 延迟 {abs(datetime.datetime.now() - emoji_send_count)} s")
        
        user_message_for_cmd = user_message
        if feishu_mode and feishu_mention_like and user_message and not user_message.startswith(reminder):
            user_message_for_cmd = f"{reminder}{user_message}"

        if user_message_for_cmd.startswith(reminder) or (feishu_mode and reminder in user_message_for_cmd):
            black_list_str = {str(i) for i in config.black_list}
            if str(event.group_id) in black_list_str:
                logger.warning(f"sys: 黑名单内，拒绝群聊 {event.group_id} 的消息")
                await actions.send(group_id=event.group_id, message=Manager.Message(
                    Segments.Text(f'''❌ Error 403: Chat location restriction
Source Model: {EnableNetwork}
Location: This chat context is not permitted.
Version: {version_name}
Document: jianer.isok.dev

For more information, see the administrator or check the system logs.''')))
                return
    
            order_i = user_message_for_cmd.find(reminder)
            if order_i != -1:
                order = user_message_for_cmd[order_i + len(reminder):].strip()
                logger.debug(f"({event_user}) ORDER: {repr(order)}")

        if feishu_mode and feishu_mention_like and not order:
            order = user_message.strip().strip("'\"“”‘’`")
            if order:
                logger.debug(f"({event_user}) Feishu Mention ORDER Fallback: {repr(order)}")

        user_message = user_message_for_cmd

        if order.startswith("绑定QQ "):
            qq_id = order.replace("绑定QQ ", "", 1).strip()
            if not re.fullmatch(r"\d{5,20}", qq_id):
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text("绑定失败：请输入正确的QQ号，例如：~绑定QQ 123456789")))
            elif bind_feishu_user(str(event.user_id), qq_id):
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(f"绑定成功：当前飞书账号已绑定 QQ {qq_id}")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text("绑定失败：写入绑定数据时出错")))
            return
        elif "我的绑定" == order:
            qq_id = get_bound_qq(str(event.user_id))
            msg = f"当前绑定QQ：{qq_id}" if qq_id else "当前未绑定QQ。请发送：~绑定QQ 你的QQ号"
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(msg)))
            return

        if f"{reminder}重启" == user_message:
            if str(event.user_id) in ADMINS:
                r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 重启QQ机器人'''
                await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin))) #管理员操作通知ROOT用户
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"正在重启{bot_name}－O－……")))

                try:
                    with open("restart.temp", "w" ,encoding="utf-8") as f:
                        f.write(str(event.group_id))
                        f.close()
                except:
                    pass

                Listener.restart()
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
        
        elif f"{reminder}重载插件" == user_message:
            global plugins
            new_plugins = await _bot_plugin_ops.cmd_reload_plugins(
                actions, Manager, Segments, event,
                ADMINS, CONFUSED_WORD, bot_name, bot_name_en, ONE_SLOGAN, reminder,
                load_plugins)
            if new_plugins is not None:
                plugins = new_plugins
        elif f"{reminder}禁用插件 " in user_message:
            new_plugins = await _bot_plugin_ops.cmd_disable_plugin(
                actions, Manager, Segments, event, user_message,
                ADMINS, CONFUSED_WORD, bot_name, bot_name_en, ONE_SLOGAN, reminder,
                load_plugins)
            if new_plugins is not None:
                plugins = new_plugins

        elif f"{reminder}启用插件 " in user_message:
            new_plugins = await _bot_plugin_ops.cmd_enable_plugin(
                actions, Manager, Segments, event, user_message,
                ADMINS, CONFUSED_WORD, bot_name, bot_name_en, ONE_SLOGAN, reminder,
                load_plugins)
            if new_plugins is not None:
                plugins = new_plugins

        elif "默认4" == order:
            # EnableNetwork = "Net"
            # print(f"sys: AI Mode change to ChatGPT-4")
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("该指令已停用，请使用 ai管理菜单。")))
        elif "深度" == order:
            # EnableNetwork = "Ds"
            # print(f"sys: AI Mode change to DeepSeek")
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("该指令已停用，请使用 ai管理菜单。")))
        elif "默认3.5" == order:
            # EnableNetwork = "Normal"
            # print(f"sys: AI Mode change to ChatGPT-3.5")
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("该指令已停用，请使用 ai管理菜单。")))
        elif "读图" == order:
            # EnableNetwork = "Pixmap"
            # print(f"sys: AI Mode change to Gemini")
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"该指令已停用，请使用 ai管理菜单。")))

        elif f"{reminder}ai管理菜单" == user_message:
            await _bot_misc_commands.cmd_ai_menu(actions, Manager, Segments, event, bot_name, bot_name_en, reminder, EnableNetwork)

        elif f"{reminder}切换AI " in user_message:
            new_ai = await _bot_misc_commands.cmd_switch_ai(actions, Manager, Segments, event, user_message, reminder, logger)
            if new_ai is not None:
                EnableNetwork = new_ai

        elif user_message.startswith(f"{reminder}简儿记忆"):
            await _bot_memory_commands.cmd_memory(
                actions, Manager, Segments, event, user_message,
                reminder, memory_service, memory_mode, memory_db_path)

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
            await _bot_group_commands.cmd_plugin_view(actions, Manager, Segments, event, bot_name, bot_name_en, loaded_plugins, disabled_plugins, failed_plugins)
        elif order.startswith("设置帮助模式"):
            if not is_qq_protocol():
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text("该功能仅支持QQ平台（OneBot/Milky）。")))
            else:
                mode_text = order.replace("设置帮助模式", "", 1).strip()
                mode = normalize_help_mode(mode_text)
                if mode is None:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(f"参数错误。示例：{reminder}设置帮助模式 图片 或 {reminder}设置帮助模式 文本")))
                elif set_help_mode(event.user_id, mode):
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

        elif f"设置全局后缀 " in order:
            await _bot_misc_commands.cmd_set_global_suffix(actions, Manager, Segments, event, order, ADMINS, CONFUSED_WORD, bot_name, suffix_manager)

        elif f"删除全局后缀" == order:
            await _bot_misc_commands.cmd_remove_global_suffix(actions, Manager, Segments, event, ADMINS, CONFUSED_WORD, bot_name, suffix_manager)

        elif f"设置特定后缀 " in order:
            await _bot_misc_commands.cmd_set_user_suffix(actions, Manager, Segments, event, order, suffix_manager)

        elif f"删除特定后缀" == order:
            await _bot_misc_commands.cmd_remove_user_suffix(actions, Manager, Segments, event, suffix_manager)
            
        elif f"{reminder}角色扮演" == user_message:
            await _bot_misc_commands.cmd_role_play(actions, Manager, Segments, event, presets, presets_tool, bot_name, bot_name_en, reminder)

        elif f"添加预设 " in order:
            await _bot_misc_commands.cmd_add_preset(actions, Manager, Segments, event, order, ADMINS, ROOT_User, CONFUSED_WORD, bot_name, bot_name_en, reminder, presets, presets_tool, PRESET_DIR)

        elif f"删除预设 " in order:
            await _bot_misc_commands.cmd_del_preset(actions, Manager, Segments, event, order, ADMINS, ROOT_User, CONFUSED_WORD, bot_name, bot_name_en, reminder, presets, presets_tool, PRESET_DIR, logger)

        elif "休眠" == order:
            if await _bot_misc_commands.cmd_sleep(actions, Manager, Segments, event, ADMINS, ROOT_User, CONFUSED_WORD, bot_name, suffix_manager, get_user_nickname):
                stop_working = True

        elif f"{reminder}感知" in user_message:
            await _bot_misc_commands.cmd_status(actions, Manager, Segments, event, ADMINS, CONFUSED_WORD, bot_name, bot_name_en, ONE_SLOGAN, second_start)

        elif f"{reminder}注销" in user_message:
            await _bot_misc_commands.cmd_logout(actions, Manager, Segments, event, ADMINS, ROOT_User, CONFUSED_WORD, bot_name, user_lists, get_user_nickname)
      
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
                order = "生图 ACG 随机"
                local_vars = globals().copy()
                local_vars.update(locals().copy())
                if not await execute_plugins(False, **local_vars):
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"{bot_name}需要 GenerateFromACG 插件才能生成好看的涩图哦 (੭ु ˃̶͈̀ ω ˂̶͈́)੭ु⁾⁾")))
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
        elif f"{reminder}更改TTS状态" == user_message:
            global gptsovitsoff
            gptsovitsoff = not gptsovitsoff
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(
                "开启TTS成功！" if not gptsovitsoff else "关闭TTS成功！")))

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
            # 没有匹配到用户发送的任何关键字，进入二级响应
            # 1. 检查用户是否是想要切换预设
            selected_preset_id = None
            for preset_id, preset_data in presets.items():
                if preset_data["name"] == order:
                    selected_preset_id = preset_id
                    break

            if selected_preset_id:
                # 将用户 ID 添加到所选预设的 uid 列表中
                if "uid" not in presets[selected_preset_id]:
                    presets[selected_preset_id]["uid"] = []
                if event.user_id not in presets[selected_preset_id]["uid"]:
                    presets[selected_preset_id]["uid"].append(event.user_id)

                # 从其他预设中移除用户 ID
                for preset_id, preset_data in presets.items():
                    if preset_id != selected_preset_id and "uid" in preset_data:
                        if event.user_id in preset_data["uid"]:
                            presets[preset_id]["uid"].remove(event.user_id)

                # 保存更新后的预设
                presets_tool.write_presets(presets)
                # del cmc # 注销
                # cmc = ContextManager()
                user_lists.clear()
                
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(presets[selected_preset_id]["info"])))
                return 


            # 2. 检查用户是否要执行插件中的功能
            local_vars = globals().copy()
            local_vars.update(locals().copy())
            try:
                if await execute_plugins(False, **local_vars):
                    return  # 只传递 event 作为位置参数
            except Exception as e:
                logger.error(f"处理插件时发生错误: {e}")
                return
            
            # 3. 全都匹配不到，进入AI回复
            if len(order) < 2:  # 不响应小于两个字的废话
                return
            
            url = ""
            sended = False
            sendedID = []
            messages_for_node = []
            enable_forward_msg_num = False
            result = ""
            
            async def process_reply_message():
                # 优先处理引用消息
                nonlocal msg
                if isinstance(event.message[0], Segments.Reply):
                    content = await actions.get_msg(event.message[0].id)
                    message = gen_message({"message": content.data["message"]})
                    for i in message:
                        if isinstance(i, Segments.Text):
                            msg += f"{i.text} "

            async def build_message_content():
                new = []
                # 处理引用消息中的内容
                if isinstance(event.message[0], Segments.Reply):
                    content = await actions.get_msg(event.message[0].id)
                    message = gen_message({"message": content.data["message"]})
                    for i in message:
                        handle_content_item(i, new)
                        
                # 处理当前消息内容
                for i in event.message:
                    handle_content_item(i, new)
                return new

            def handle_content_item(item, container):
                if isinstance(item, Segments.Text):
                    container.append(Parts.Text(item.text.replace(reminder, "", 1)))
                elif isinstance(item, Segments.Image):
                    url = item.file if item.file.startswith("http") else item.url
                    logger.debug(f"AI: URL位置 {replace_scheme_with_http(url)}")
                    container.append(Parts.File.upload_from_url(replace_scheme_with_http(url)))
                    logger.debug("AI: 有图")

            async def handle_message_stream(response_stream, is_openai=True):
                nonlocal result, sended, enable_forward_msg_num
                async for partial, r_type in response_stream:
                    if is_openai:
                        if r_type != 'message':
                            user_lists = partial
                            continue

                    processed_text = suffix_manager.process_text(str(partial), event.user_id)
                    message = Segments.Text(processed_text)
                    if enable_forward_msg_num:
                        messages_for_node.append(message)
                    else:
                        if not sended:
                            await actions.send(
                                group_id=event.group_id,
                                message=Manager.Message(Segments.Reply(event.message_id), message)
                            )
                        else:
                            await actions.send(
                                group_id=event.group_id,
                                message=Manager.Message(message)
                            )
                        messages_for_node.append(message)
                    
                    if len(messages_for_node) > MAX_MESSAGE_LENGTH - 1 and not enable_forward_msg_num:
                        enable_forward_msg_num = True

                    if enable_forward_msg_num and len(messages_for_node) == MAX_MESSAGE_LENGTH + 1:
                        sendedID.append(await actions.send(
                            group_id=event.group_id,
                            message=Manager.Message(Segments.Text(r"**[thinking]**"))
                        ))

                    sended = True
                    result += processed_text + '\n'

            async def finalize_messages():
                if enable_forward_msg_num:
                    # 删除临时消息
                    for msg_id in sendedID:
                        await actions.del_message(msg_id.data.message_id) # 禁用消息连续撤回以防止QQ检测
                    
                    for m in range(len(messages_for_node)):
                        messages_for_node[m] = Segments.CustomNode(
                            str(event.self_id),
                            bot_name,
                            Manager.Message(messages_for_node[m])
                        )
                    
                    # 发送合并转发
                    if len(messages_for_node) > MAX_MESSAGE_LENGTH:
                        await actions.send_group_forward_msg(
                            group_id=event.group_id,
                            message=Manager.Message(*messages_for_node)
                        )

            try:
                # 收集图片链接 (适配 Gemini 多模态)
                images = []
                # if EnableNetwork == "Pixmap": # 这里可以放宽判断，或者依赖 ARC_AI 内部判断
                for i in event.message:
                    if isinstance(i, Segments.Image):
                        url = i.file if i.file.startswith("http") else i.url
                        url = replace_scheme_with_http(url)
                        images.append(url)
                
                msg = ""
                await process_reply_message()
                msg += order

                # 获取用户当前的系统提示词
                sys_prompt = ""
                # presets_tool.current_preset (全局默认预设) 已经在前面被加载
                # 检查用户是否有特定预设
                active_preset_id = presets_tool.NORMAL_PRESET
                for preset_id, preset_data in presets.items():
                    if "uid" in preset_data and event.user_id in preset_data["uid"]:
                        active_preset_id = preset_id
                        # 读取预设内容
                        preset_path = os.path.join(PRESET_DIR, preset_data["path"])
                        if os.path.exists(preset_path):
                            with open(preset_path, "r", encoding="utf-8") as f:
                                sys_prompt = f.read()
                            sys_prompt = sys_prompt.replace("{self.bot_name}", bot_name)
                            sys_prompt = sys_prompt.replace("{self.event_user}", event_user)
                            sys_prompt = sys_prompt.replace("{self.event_user_id}", str(event.user_id))
                            sys_prompt = sys_prompt.rstrip()
                        logger.info(f"[{event.time_str}] '{preset_data['name']}' 已载入用户预设")
                        break
                
                # 如果没有用户特定预设，使用全局默认预设 (如果有的话，presets_tool 可能已经处理了)
                if not sys_prompt:
                     # 这里可以尝试加载默认预设内容，如果 presets_tool.current_preset 指向了一个有效预设
                     pass # 暂时保持为空，或者您有默认的 sys_prompt 逻辑

                persona_prompt = sys_prompt or base_sys_prompt
                memory_context = await memory_service.build_memory_context(
                    group_id=event.group_id,
                    user_id=event.user_id,
                    is_private=False,
                    query_text=msg,
                    preset_key=active_preset_id,
                    topk=memory_topk,
                )
                if memory_context:
                    final_sys_prompt = (persona_prompt + "\n\n" if persona_prompt else "") + memory_context
                else:
                    final_sys_prompt = persona_prompt

                # 调用 ARC_AI 获取回复流
                response_stream = ARC_AI.get_response_stream(
                    EnableNetwork,
                    msg,
                    user_lists,
                    event.user_id,
                    final_sys_prompt,
                    images
                )
                
                # 处理回复流 (is_openai=True 以支持 user_lists 更新)
                await handle_message_stream(response_stream, is_openai=True)

                result = result.rstrip()
                await finalize_messages()
                
                if not sended:
                    await actions.send(
                        group_id=event.group_id,
                        message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(result))
                    )
                    
                if gptsovitsoff == False:
                    """EdgeTTS 语音回复"""
                    TTSettings: dict = {}
                    if config.others["TTS"]:
                        if isinstance(config.others["TTS"], dict):             
                            TTSettings = config.others["TTS"]
                        else:             
                            TTSettings = dict(config.others["TTS"])
                    
                    audio_file_path = None
                    if TTSettings != {}:
                        audio_file_path = await amain(sanitize_for_tts(result), TTSettings.get("voiceColor", "zh-CN-XiaoyiNeural"), 
                                                      TTSettings.get("rate", "+0%"), TTSettings.get("volume", "+0%"), TTSettings.get("pitch", "+0Hz"))
                    else:
                        logger.warning("EdgeTTS 配置文件不完整，或未配置，使用默认音色。")
                        audio_file_path = await amain(sanitize_for_tts(result), "zh-CN-XiaoyiNeural", "+0%", "+0%", "+0Hz")

                    if audio_file_path and isinstance(audio_file_path, str) and os.path.isfile(audio_file_path):
                        logger.info(f"发送音频：{os.path.abspath(audio_file_path)}")
                        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Record(os.path.abspath(audio_file_path))))
                        await asyncio.sleep(3)
                        try:
                            if os.path.exists(audio_file_path):
                                os.remove(audio_file_path)
                                logger.info(f"删除音频 {os.path.basename(audio_file_path)} 成功。")
                        except Exception:
                            try:
                                import gc
                                gc.collect()  # 强制垃圾回收
                                await asyncio.sleep(1)
                                if os.path.exists(audio_file_path):
                                    os.remove(audio_file_path)
                            except Exception as e:
                                logger.error(f"强制删除缓存音频 {audio_file_path} 失败: {e}")

            except UnboundLocalError:
                raise
            except TimeoutError:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id),Segments.Text(suffix_manager.process_text(f"哎呀，你问的问题太复杂了，{bot_name}想不出来了 ┭┮﹏┭┮", event.user_id))))
            except Exception as e:
                logger.error(traceback.format_exc())
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id),Segments.Text(suffix_manager.process_text(f"{type(e)}\n{url}\n{bot_name}发生错误，不能回复你的消息了，请稍候再试吧 ε(┬┬﹏┬┬)3", event.user_id))))
      
def help_message(event) -> str:
    return _bot_help_view.build_help_message(config, Events, event, bot_name, reminder, plugins_help)


async def send_help_visual(actions, event, content: str, reply_message_id: str = None):
    await _bot_help_view.send_help_visual(
        config, Events, Manager, Segments, help_mode_settings,
        bot_name, logger, actions, event, content, reply_message_id
    )

Listener.run()

