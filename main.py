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
PLUGIN_FOLDER = "plugins"
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

# 插件加载器 NEXT 3
def load_plugins():
    global loaded_plugins, disabled_plugins, failed_plugins, plugins_help, reminder, bot_name, PLUGIN_FOLDER
    plugins = []
    plugins_help = ""
    protocol_now = str(config.protocol).lower()
    incompatible_in_feishu = {
        "CheckAccount",
        "CheckGroup",
        "LikePlugin",
        "AdvancedQuote",
        "SumUp_MySQL",
    }

    loaded_plugins.clear()
    disabled_plugins.clear()
    failed_plugins.clear()

    for filename in os.listdir(PLUGIN_FOLDER):
        module_name = filename  # Folder name as module name
        plugin_base_name = module_name[:-3] if module_name.endswith(".py") else module_name
        logger.debug(f"check file or directory: {filename}")

        if filename == "__pycache__":
            logger.debug("Directory __pycache__ not load.")
            continue

        # 检查是否禁用
        if filename.startswith("d_"):
            disabled_plugins.append(module_name)
            continue

        if protocol_now == "feishu" and plugin_base_name in incompatible_in_feishu:
            disabled_plugins.append(plugin_base_name)
            logger.info(f"Feishu 模式跳过不兼容插件: {plugin_base_name}")
            continue

        # 处理目录形式插件
        plugin_path = os.path.join(PLUGIN_FOLDER, filename)  # Full plugin path
        if os.path.isdir(plugin_path):
            setup_file = os.path.join(plugin_path, "setup.py")
            if os.path.exists(setup_file):
                try:
                    # Load setup.py
                    unique_module_name = f"{module_name}_{uuid.uuid4().hex}"  # Generate unique module name
                    spec = importlib.util.spec_from_file_location(unique_module_name, setup_file)
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[unique_module_name] = module
                    spec.loader.exec_module(module)
                    logger.debug(f"Loaded setup.py from folder plugin: {module_name}")

                    # Verify plugin
                    if hasattr(module, 'TRIGGHT_KEYWORD') and hasattr(module, 'on_message'):
                        if isinstance(module.TRIGGHT_KEYWORD, str):
                            plugins.append(module)  # Add module
                            loaded_plugins.append(unique_module_name) 
                            if hasattr(module, 'HELP_MESSAGE'):
                                if isinstance(module.HELP_MESSAGE, str):
                                    for help_message in [line.strip() for line in module.HELP_MESSAGE.splitlines() if line.strip()]:
                                        plugins_help += f"\n       {help_message}"

                            logger.info(f"已加载插件: {unique_module_name} (关键词: {module.TRIGGHT_KEYWORD})")
                        else:
                            failed_plugins.append(f"{module_name} (TRIGGHT_KEYWORD 必须是字符串)")
                    else:
                        failed_plugins.append(f"{module_name} (缺少 TRIGGHT_KEYWORD：触发标识符 或 on_message：触发函数后端)")

                except FileNotFoundError as e:
                    failed_plugins.append(f"{module_name} (文件未找到: {e})")
                    logger.error(f"加载插件 {unique_module_name} 失败，是因为: {e}")
                    if unique_module_name in sys.modules:
                        del sys.modules[unique_module_name]
                except ImportError as e:
                    failed_plugins.append(f"{module_name} (导入错误: {e})")
                    logger.error(f"加载插件 {unique_module_name} 失败，是因为: \n{traceback.format_exc()}\n")
                    if unique_module_name in sys.modules:
                        del sys.modules[unique_module_name]
                except Exception as e:
                    failed_plugins.append(f"{module_name} (其他错误: {str(e)})")
                    logger.error(f"加载插件 {unique_module_name} 失败: \n{traceback.format_exc()}\n")
                    if unique_module_name in sys.modules:
                        del sys.modules[unique_module_name]  # Cleanup

            else:
                logger.warning(f"目录 {filename} 中缺少 setup.py 文件")
                failed_plugins.append(f"{filename} (入口错误: 缺少 setup.py 文件)")

        # 处理文件形式插件
        elif filename.endswith(".py") or filename.endswith(".pyw"):
            module_name = filename[:-3] if filename.endswith(".py") else filename[:-4]

            # 检查是否禁用
            if filename.startswith("d_"):
                disabled_plugins.append(str(module_name)[3:])
                continue

            # 生成唯一的模块名
            unique_module_name = f"{module_name}_{uuid.uuid4().hex}"

            try:
                # 检查模块是否已经加载
                if unique_module_name in sys.modules:
                    logger.warning(f"模块 {unique_module_name} 已经加载，跳过")
                    continue

                # 创建模块规范
                spec = importlib.util.spec_from_file_location(unique_module_name, os.path.join(PLUGIN_FOLDER, filename))
                module = importlib.util.module_from_spec(spec)
                sys.modules[unique_module_name] = module  # 添加到 sys.modules
                spec.loader.exec_module(module)

                # 验证模块是否符合插件规范
                if hasattr(module, 'TRIGGHT_KEYWORD') and hasattr(module, 'on_message'):
                    if isinstance(module.TRIGGHT_KEYWORD, str):
                        plugins.append(module)  # 重要：把整个模块全tm加入到列表
                        loaded_plugins.append(unique_module_name)
                        if hasattr(module, 'HELP_MESSAGE'):
                            if isinstance(module.HELP_MESSAGE, str):
                                for help_message in [line.strip() for line in module.HELP_MESSAGE.splitlines() if line.strip()]:
                                    plugins_help += f"\n       {help_message}"

                        logger.info(f"已加载插件: {unique_module_name} (关键词: {module.TRIGGHT_KEYWORD})")
                    else:
                        failed_plugins.append(f"{module_name} (TRIGGHT_KEYWORD 必须是字符串)")
                else:
                    failed_plugins.append(f"{module_name} (缺少 TRIGGHT_KEYWORD：触发标识符 或 on_message：触发函数后端)")

            except FileNotFoundError as e:
                failed_plugins.append(f"{module_name} (文件未找到: {e})")
                logger.error(f"加载插件 {unique_module_name} 失败，原因是: {e}")
                if unique_module_name in sys.modules:
                    del sys.modules[unique_module_name]
            except ImportError as e:
                failed_plugins.append(f"{module_name} (导入错误: {e})")
                logger.error(f"加载插件 {unique_module_name} 失败，原因是: \n{traceback.format_exc()}\n")
                if unique_module_name in sys.modules:
                    del sys.modules[unique_module_name]
            except Exception as e:
                failed_plugins.append(f"{module_name} (其他错误: {str(traceback.format_exc())})")
                logger.error(f"加载插件 {unique_module_name} 失败: \n{traceback.format_exc()}\n")
                if unique_module_name in sys.modules:
                    del sys.modules[unique_module_name]  # Cleanup

        else:
            logger.debug(f"跳过非插件文件或目录: {filename}")

    logger.info(f"成功加载 {len(loaded_plugins)} 个插件")
    return plugins

plugins = load_plugins() #在任何操作执行之前加载插件

# 插件运行器 NEXT 3
async def execute_plugins(isAny: bool, **main_context) -> bool: # 接受 main.py 的上下文，也就是所有的关键字
    has_plugin = False
    user_message = main_context["order"] if "order" in main_context else ""

    for plugin_module in plugins:
        if (not isAny and f"{reminder}{plugin_module.TRIGGHT_KEYWORD}" in f"{reminder}{user_message}") or (isAny and plugin_module.TRIGGHT_KEYWORD == "Any"): 
            try:
                # 动态构建参数
                on_message_params = inspect.signature(plugin_module.on_message).parameters
                kwargs = {}
                for param_name, param in on_message_params.items():
                    if param_name in main_context:
                        kwargs[param_name] = main_context[param_name]  # 从 main_context 获取
                    elif param.default is not inspect.Parameter.empty:
                        pass  # 使用默认值
                    else:
                        raise ValueError(f'''插件 {plugin_module.__name__} 未提供参数 {param_name} ：
无法在所有上下文中找到具有该标识符的变量且该标识符不具有默认值，这样的变量可能在定义前被使用或本就没有定义。
如果您是开发者，请在 main.py 中提供此值。如果您是用户，请忽略此消息并通知管理员及时地修复。
详见 https://github.com/SRInternet-Studio/Jianer_QQ_bot/wiki''')

                response = await plugin_module.on_message(**kwargs)  # 传递 event 和动态参数

                if response is not None:
                    if response == True:
                        has_plugin = True
                        break

            except Exception as e:
                logger.error(f"\n插件 {plugin_module.__name__} 执行出错，是因为: \n{traceback.format_exc()}")
                if not isAny:
                    has_plugin = True
    
    return has_plugin

def load_blacklist():
    try:
        with open("blacklist.sr", "r", encoding="utf-8") as f:
            blacklist115 = set(line.strip() for line in f)  # 这里是集合
        return blacklist115
    except FileNotFoundError:
        return set() 
             
def has_emoji(s: str) -> bool: # emoji +1 功能
    # 判断找到的 emoji 数量是否为 1 并且字符串的长度大于等于 1
    return emoji.emoji_count(s) == 1 and len(s) == 1

def timing_message(actions: Listener.Actions):
    while True:
        if not os.path.isfile("timing_message.ini"):
            now1 = datetime.datetime.now()
            logger.debug(f"Current: {now1.hour:02}:{now1.minute:02}")
            time.sleep(60 - now1.second)
            continue
        
        with open("timing_message.ini", "r", encoding="utf-8") as f:
            content = f.read().strip()
        
        if "⊕" in content:
            # 找到第一个换行符的位置
            first_newline_pos = content.find("\n")
            if first_newline_pos != -1:
                # 如果有换行，只在第一行查找⊕符号
                first_line = content[:first_newline_pos]
                remaining_lines = content[first_newline_pos:]
                if "⊕" in first_line:
                    time_part, message_part = first_line.split("⊕", 1)
                    # 合并消息部分和剩余行
                    full_message = message_part + remaining_lines
                else:
                    # 如果第一行没有⊕符号，使用整个内容作为消息
                    full_message = content
            else:
                # 如果没有换行，直接分割整个内容
                time_part, full_message = content.split("⊕", 1)
        else:
            # 如果没有⊕符号，使用整个内容作为消息
            full_message = content
            time_part = ""
        
        now = datetime.datetime.now()
        logger.debug(f"Current: {now.hour:02}:{now.minute:02}, target: {time_part}")
        if time_part and f"{now.hour:02}:{now.minute:02}" == time_part:
            logger.info("send timing messages")
            asyncio.run(send_msg_all_groups(full_message, actions))
        
        time.sleep(60 - now.second)
        
async def send_msg_all_groups(text, actions: Listener.Actions, message: Manager.Message = None):
    echo = await actions.custom.get_group_list()
    result = Manager.Ret.fetch(echo)
    blacklist = load_blacklist()  # 必须在发送消息前加载黑名单
    logger.info(f"sys: 群发 {result.data.raw}")
    # Apply global suffix for broadcast messages
    processed_text = suffix_manager.process_text(text, 0)
    for group in result.data.raw:
        group_id = str(group['group_id'])  # 将group_id转为字符串类型,不然来个error会溶血
        if group_id not in blacklist:  # 检查群组 ID 是否在黑名单中,在就别给lz发
            await actions.send(group_id=group['group_id'], message=Manager.Message(Segments.Text(processed_text)))
            time.sleep(random.random()*3)
        else:
            logger.warning(f"群聊 {group_id} 在黑名单内，取消发送")


def Read_Settings():
    global Super_User, Manage_User
    
    def load_user_list(filename):
        if not os.path.exists(filename):
            with open(filename, 'w'):
                pass
            
        with open(filename, 'r') as f:
            return list({line.strip() for line in f if line.strip()})
    
    Super_User = load_user_list("Super_User.ini")
    Manage_User = load_user_list("Manage_User.ini")
    logger.info(f'''————————————————
sys: User_Group loaded.
Super_User: {Super_User}
Manage_User: {Manage_User}
————————————————''')

def Write_Settings(s: list, m: list) -> bool:
    s = [item for item in s if item]
    m = [item for item in m if item]
    global Super_User, Manage_User
    su = ""
    for item in range(len(s)):
        su += s[item]
        if item != len(s) - 1:
            su += "\n"
    ma = ""
    for item in range(len(m)):
        ma += m[item]
        if item != len(m) - 1:
            ma += "\n"

    try:
        with open("Super_User.ini", "w") as f:
            f.write(su)
            f.close()
        with open("Manage_User.ini", "w") as f:
            f.write(ma)
            f.close()

        Super_User = s
        Manage_User = m

        return True
    except:
        return False


def load_feishu_bindings() -> dict:
    if not os.path.exists(FEISHU_BIND_FILE):
        return {}
    try:
        with open(FEISHU_BIND_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if k and v}
    except Exception:
        pass
    return {}


def save_feishu_bindings(bindings: dict) -> bool:
    try:
        with open(FEISHU_BIND_FILE, "w", encoding="utf-8") as f:
            json.dump(bindings, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def bind_feishu_user(open_id: str, qq_id: str) -> bool:
    open_id = str(open_id or "").strip()
    qq_id = str(qq_id or "").strip()
    if not open_id or not qq_id:
        return False
    bindings = load_feishu_bindings()
    bindings[open_id] = qq_id
    return save_feishu_bindings(bindings)


def get_bound_qq(open_id: str) -> str | None:
    bindings = load_feishu_bindings()
    return bindings.get(str(open_id))


def is_qq_protocol() -> bool:
    return str(config.protocol).lower() in {"onebot", "milky"}


def is_feishu_protocol() -> bool:
    return str(config.protocol).lower() == "feishu"


def load_help_mode_settings() -> dict:
    if not os.path.exists(HELP_MODE_FILE):
        return {}
    try:
        with open(HELP_MODE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if k and v}
    except Exception:
        pass
    return {}


def save_help_mode_settings(settings: dict) -> bool:
    try:
        with open(HELP_MODE_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def normalize_help_mode(raw_mode: str) -> str | None:
    mode = str(raw_mode or "").strip().lower()
    if mode in {"图片", "图", "image", "img"}:
        return "图片"
    if mode in {"文本", "文字", "转发", "forward", "text"}:
        return "文本"
    return None


help_mode_settings = load_help_mode_settings()


def get_help_mode(user_id: str | int) -> str:
    default_mode = normalize_help_mode(config.others.get("help_mode_default", "图片")) or "图片"
    mode = normalize_help_mode(help_mode_settings.get(str(user_id), default_mode))
    return mode or default_mode


def set_help_mode(user_id: str | int, mode: str) -> bool:
    global help_mode_settings
    parsed_mode = normalize_help_mode(mode)
    if parsed_mode is None:
        return False
    help_mode_settings[str(user_id)] = parsed_mode
    if save_help_mode_settings(help_mode_settings):
        return True
    help_mode_settings = load_help_mode_settings()
    return False


def normalize_group_message_text(event: Events.GroupMessageEvent, text: str) -> str:
    msg = str(text or "").strip()
    if str(config.protocol).lower() == "feishu":
        msg = re.sub(r"^(?:@\S+\s*)+", "", msg).strip()
        msg = msg.replace("\u200b", "").replace("\ufeff", "").strip()
    return msg


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
        if str(event.sub_type) == "poke" and int(event.target_id) == int(event.self_id): # 被戳一戳
            logger.info(f"({event.user_id}) POKED")
            try:
                if event.group_id:
                    poke_result = await actions.custom.group_poke(group_id=event.group_id, user_id=event.user_id)
                    poke_result = Manager.Ret.fetch(poke_result).data.raw
                    if poke_result.get("status", "error") != "ok":
                        logger.warning(f"sys: 戳一戳失败 {poke_result}")
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(random.choice(config.others["poke_rejection_phrases"]))))
                elif event.user_id:
                    poke_result = await actions.custom.friend_poke(user_id=event.user_id)
                    poke_result = Manager.Ret.fetch(poke_result).data.raw
                    if poke_result.get("status", "error") != "ok":
                        logger.warning(f"sys: 戳一戳失败 {poke_result}")
                    await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(random.choice(config.others["poke_rejection_phrases"]))))
            except KeyError:
                logger.warning("不接受戳一戳")
                
    if isinstance(event, Events.HyperListenerStartNotify):
        if os.path.exists("restart.temp"):
            with open("restart.temp", "r" ,encoding="utf-7") as f:
                group_id = f.read()
                f.close()
            os.remove("restart.temp")
            r_admin = f'''在 {event.time_str} QQ机器人已手动重启成功'''
            await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin))) #管理员操作通知ROOT用户
            await actions.send(group_id=group_id, message=Manager.Message(Segments.Text(f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
欢迎! {bot_name} 已经重启成功！ 现在你可以发送 {reminder}帮助 来知道更多。''')))

    elif isinstance(event, Events.GroupMemberIncreaseEvent):
        if Wait_for_add_in:
            Wait_for_add_in = False
            return
        
        user = event.user_id
        welcome = f''' 加入{bot_name}的大家庭，{bot_name}是你最忠实可爱的女朋友噢o(*≧▽≦)ツ
随时和{bot_name}交流，你只需要在问题的前面加上 {reminder} 就可以啦！( •̀ ω •́ )✧
@{bot_name} 可以看看{bot_name}会做什么有趣的事情哦~o((>ω< ))o
祝你在{bot_name}的大家庭里生活愉快！♪(≧∀≦)ゞ☆'''
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(f"http://q2.qlogo.cn/headimg_dl?dst_uin={user}&spec=640"), Segments.Text("欢迎"), Segments.At(user), Segments.Text(welcome)))
        
    elif isinstance(event, Events.GroupMemberDecreaseEvent):
        user_nick = await get_user_nickname(event.user_id, Manager, actions)
        if user_nick:
            user_nick = f"@{user_nick} "
        else:
            user_nick = "有人又"

        text = f'''{user_nick}离开了{bot_name}的大家庭，{bot_name}好伤心o(TヘTo)……
大家一定要记得多来陪{bot_name}玩玩ヾ(•ω•`)o'''
        logger.info(f"group: {event.user_id} 已离开群聊 {event.group_id}")
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(text)))

    elif isinstance(event, Events.GroupAddInviteEvent):
      keywords: list = config.others["Auto_approval"]
      cleaned_text = event.comment.strip().lower()

      for keyword in keywords:
        processed_keyword = keyword.strip().lower()
        if processed_keyword in cleaned_text: 
            try:
                user = event.user_id
                logger.info(f"group: {await get_user_nickname(user, Manager, actions)} 的入群回答 {processed_keyword} 符合正确答案，已准许入群 {event.group_id}")
                await actions.set_group_add_request(flag=event.flag, sub_type=event.sub_type, approve=True, reason="")
                Wait_for_add_in = True
                welcome = f'''{await get_user_nickname(user, Manager, actions)} 的答案正确，欢迎加入{bot_name}的大家庭！o(*≧▽≦)ツ
随时和{bot_name}交流，只需在问题的前面加上 {reminder} 就可以啦！( •̀ ω •́ )✧
@{bot_name} 可以看看{bot_name}会做什么有趣的事情哦~o((>ω< ))o
祝你在{bot_name}的大家庭里生活愉快！♪(≧∀≦)ゞ☆'''  
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(f"http://q2.qlogo.cn/headimg_dl?dst_uin={user}&spec=640"), Segments.Text(welcome)))
                break
            except:
                logger.error(traceback.format_exc())
          
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
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(suffix_manager.process_text("pong! 爆炸！v(◦'ωˉ◦)~♡ ", event.user_id))))
            
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
                    with open("restart.temp", "w" ,encoding="utf-7") as f:
                        f.write(str(event.group_id))
                        f.close()
                except:
                    pass

                Listener.restart()
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
        
        elif f"{reminder}重载插件" == user_message:
            if str(event.user_id) in ADMINS:
                global plugins
                plugins = load_plugins()

                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
外部后端已重载完成。发送 {reminder}插件视角 以查看更多信息。''')))
                
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
        elif f"{reminder}禁用插件 " in user_message:
            if str(event.user_id) in ADMINS:
                message = user_message
                parts = message.split("禁用插件")
                if len(parts) > 1:
                    plugin_name = parts[-1].strip() # 获取命令后面的插件名
                    disable = True
                else: 
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"管理员：你的格式有误。\n格式：{reminder}禁用插件 (plugin_name)\n参考：{reminder}禁用插件 Hello World")))

                if not plugin_name:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"管理员：你的格式有误。\n格式：{reminder}禁用插件 (plugin_name)\n参考：{reminder}禁用插件 Hello World")))
                    return

                possible_paths = [
                    os.path.join(os.path.abspath(PLUGIN_FOLDER), f"{plugin_name}.py"),
                    os.path.join(os.path.abspath(PLUGIN_FOLDER), f"{plugin_name}.pyw"),
                    os.path.join(os.path.abspath(PLUGIN_FOLDER), plugin_name),  # 文件夹
                ]

                found_path = None
                for path in possible_paths:
                    if os.path.exists(path):
                        found_path = path
                        break

                if not found_path:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: 找不到插件 {plugin_name}。''')))
                    return

                dirname, basename = os.path.split(found_path)

                new_name = "d_" + basename
                new_path = os.path.join(dirname, new_name)

                if not basename.startswith("d_"):
                    try:
                        os.rename(found_path, new_path)
                    except Exception as e:
                        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: 禁用插件 {plugin_name} 时发生错误。
错误信息：{str(e)}''')))
                        return

                plugins = load_plugins()

                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
插件 {plugin_name} 已经成功禁用''')))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))

        elif f"{reminder}启用插件 " in user_message:
            if str(event.user_id) in ADMINS:
                message = user_message
                parts = message.split("启用插件")
                if len(parts) > 1:
                    plugin_name = parts[-1].strip() # 获取命令后面的插件名
                    disable = False
                else: 
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"管理员：你的格式有误。\n格式：{reminder}启用插件 (plugin_name)\n参考：{reminder}启用插件 Hello World")))

                if not plugin_name:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"管理员：你的格式有误。\n格式：{reminder}启用插件 (plugin_name)\n参考：{reminder}启用插件 Hello World")))
                    return

                possible_paths = [
                    os.path.join(os.path.abspath(PLUGIN_FOLDER), f"d_{plugin_name}.py"),
                    os.path.join(os.path.abspath(PLUGIN_FOLDER), f"d_{plugin_name}.pyw"),
                    os.path.join(os.path.abspath(PLUGIN_FOLDER), f"d_{plugin_name}"),  # 文件夹
                ]

                found_path = None
                for path in possible_paths:
                    if os.path.exists(path):
                        found_path = path
                        break

                if not found_path:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: 找不到插件 {plugin_name}。''')))
                    return

                dirname, basename = os.path.split(found_path)

                if basename.startswith("d_"):
                    original_name = basename[2:]  # 去除 d_ 前缀，这意味着插件可以被执行
                    original_path = os.path.join(dirname, original_name)
                    try:
                        os.rename(found_path, original_path)
                    except Exception as e:
                        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: 启用插件 {plugin_name} 时发生错误。
错误信息：{str(e)}''')))
                        return

                plugins = load_plugins() # 自动重载插件

                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
插件 {plugin_name} 已经成功启用''')))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))

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
            # if str(event.user_id) in ADMINS: # 所有人可用
            ais = ARC_AI.list_available_ais()
            current_ai_friendly = ARC_AI.get_current_ai_name(EnableNetwork)
            
            ai_list_str = "\n".join([f"- {friendly} (代码: {name})" for name, friendly in ais.items()])
            
            menu = f'''{bot_name} {bot_name_en} - AI管理菜单
————————————————————
当前使用的AI: {current_ai_friendly} (代码: {EnableNetwork})

可用AI列表:
{ai_list_str}

指令:
{reminder}切换AI [AI代码] —> 切换到指定的AI
例如: {reminder}切换AI gemini
'''
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(menu)))

        elif f"{reminder}切换AI " in user_message:
            # if str(event.user_id) in ADMINS: # 所有人可用
            target_ai = user_message.replace(f"{reminder}切换AI ", "").strip()
            available_ais = ARC_AI.list_available_ais()
            
            if target_ai in available_ais:
                EnableNetwork = target_ai
                friendly_name = available_ais[target_ai]
                logger.info(f"sys: AI Mode change to {friendly_name} ({target_ai})")
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"成功切换到AI: {friendly_name}")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"找不到AI配置: {target_ai}，请检查代码拼写。")))

        elif user_message.startswith(f"{reminder}简儿记忆"):
            cmd = user_message[len(reminder) :].strip()
            parts = [p for p in cmd.split() if p]
            action = parts[1] if len(parts) >= 2 else "帮助"

            def parse_interval_seconds(s: str) -> int:
                s = (s or "").strip().lower()
                m = re.match(r"^(\d+)\s*([smhd]?)$", s)
                if not m:
                    return 0
                n = int(m.group(1))
                unit = m.group(2)
                if unit == "s" or unit == "":
                    return n
                if unit == "m":
                    return n * 60
                if unit == "h":
                    return n * 3600
                if unit == "d":
                    return n * 86400
                return 0

            if action in ("帮助", "help"):
                info = f"""简儿记忆
————————————————————
记忆AI配置: {memory_mode}
数据库: {memory_db_path}

指令:
{reminder}简儿记忆 状态
{reminder}简儿记忆 开启 / 关闭
{reminder}简儿记忆 间隔 6h/30m/3600
{reminder}简儿记忆 立即生成
"""
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(info)))

            elif action == "状态":
                st = await memory_service.get_status(event.group_id, event.user_id, False)
                if not st:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("未找到记忆状态。")))
                    return
                last_at = st.get("last_generated_at", 0) or 0
                last_at_str = "从未" if int(last_at) <= 0 else datetime.datetime.fromtimestamp(int(last_at)).strftime("%Y-%m-%d %H:%M:%S")
                msg = f"""简儿记忆状态
————————————————————
开启: {bool(st.get("enabled", 0))}
间隔(秒): {st.get("interval_seconds", 0)}
上次生成: {last_at_str}
原始记录: {st.get("raw_count", 0)} (+{st.get("new_raw_count", 0)})
个人/本群记忆: {st.get("mem_count", 0)}
全局记忆: {st.get("global_count", 0)}
"""
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(msg)))

            elif action == "开启":
                await memory_service.set_enabled(event.group_id, event.user_id, False, True)
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("已开启简儿记忆。")))

            elif action == "关闭":
                await memory_service.set_enabled(event.group_id, event.user_id, False, False)
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("已关闭简儿记忆。")))

            elif action == "间隔":
                if len(parts) < 3:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("用法：#简儿记忆 间隔 6h/30m/3600")))
                    return
                seconds = parse_interval_seconds(parts[2])
                if seconds <= 0:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("间隔格式无效。")))
                    return
                await memory_service.set_interval_seconds(event.group_id, event.user_id, False, seconds)
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"已设置简儿记忆间隔为 {seconds} 秒。")))

            elif action == "立即生成":
                ok = await memory_service.generate_now(event.group_id, event.user_id, False)
                await actions.send(
                    group_id=event.group_id,
                    message=Manager.Message(Segments.Text("已生成一轮简儿记忆。" if ok else "暂无足够新增聊天记录生成记忆。")),
                )
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("指令不支持，发送 #简儿记忆 帮助。")))

        elif "列出黑名单" == order:
          if str(event.user_id) in ADMINS:
            try:
                with open("blacklist.sr", "r", encoding="utf-8") as f:
                    blacklist1 = set(line.strip() for line in f) 
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名单列表加载完成: {blacklist1}")))
            except FileNotFoundError:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("黑名单列表加载失败,原因:没有文件")))
            except UnicodeDecodeError:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("黑名单列表加载失败,原因:解码失败")))
          else:
              await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
        elif "添加黑名单 " in order:
            blacklist_file = "blacklist.sr"
            if str(event.user_id) in ADMINS:
                Toset2 = order[order.find("添加黑名单 ") + len("添加黑名单 "):].strip()
                blacklist114 = load_blacklist() # 加载现有的黑名单,防止已修改沒更新
                if Toset2 not in blacklist114:
                    blacklist114.add(Toset2) 
                    try:
                        with open(blacklist_file, "w", encoding="utf-8") as f:
                         for item in blacklist114:
                            f.write(item + "\n")  # 防止之前的丟失555，并添加换行符
                        r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 将群 {Toset2} 添加到禁止群发黑名单'''
                        await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin))) #管理员操作通知ROOT用户
                        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名单添加成功\n现在的群发黑名单: {blacklist114}")))
                    except Exception as e:
                       await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名单添加失败, 是因为\n{e}")))
                else:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名单添加失败,是因为{Toset2}已在黑名单")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
        elif "删除黑名单 " in order:
            blacklist_file = "blacklist.sr"
            if str(event.user_id) in ADMINS:
                Toset1 = order[order.find("删除黑名单 ") + len("删除黑名单 "):].strip()
                blacklist117 = load_blacklist() # 加载现有的黑名单,防止已修改沒更新
                if Toset1 in blacklist117:
                    blacklist117.remove(Toset1) 
                    try:
                        with open(blacklist_file, "w", encoding="utf-8") as f:
                         for item in blacklist117:
                            f.write(item + "\n")  # 防止之前的丟失555，并添加换行符
                        r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 将群 {Toset1} 从禁止群发黑名单中删除'''
                        await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin))) #管理员操作通知ROOT用户
                        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名单删除成功\n现在黑名单: {blacklist117}")))
                    except Exception as e:
                       await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名单删除失败, 是因为\n{e}")))
                else:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名单删除失败, 是因为群{Toset1}不在黑名单")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
            
        elif "删除管理 " in order:
            r = ""
            r_admin = ""
            Toset = ""
            for i in event.message:
                if isinstance(i, Segments.At):
                    Toset = str(i.qq)
                    
            if str(event.user_id) in SUPERS:
                Toset = order[order.find("删除管理 ") + len("删除管理 "):].strip() if Toset == "" else Toset
                s = Super_User
                m = Manage_User
                if Toset in ROOT_User:
                    r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败：指定的用户是 ROOT_User 且组 ROOT_User 为只读。'''
                    r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 尝试夺取您的 ROOT_User 权限，已被阻止'''
                else:
                    if Toset in s:
                        s.remove(Toset)
                    if Toset in m:
                        m.remove(Toset)
                        
                    nick = await get_user_nickname(Toset, Manager, actions)
                    if Write_Settings(s, m):
                        r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
成功: {nick} 现在是一个普通用户了。
现在发送 {reminder}帮助 了解你拥有的权限。'''
                        r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 删除了用户 {nick} 的管理员权限'''
                    else:
                        r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败：设置文件不可写。'''
                        r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 尝试删除用户 {nick} 的管理员权限，但因为无法读写配置文件导致修改失败'''
            else:
                r  = CONFUSED_WORD.format(bot_name=bot_name)

            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
            if r_admin:
                await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin))) #管理员操作通知ROOT用户
            
        elif "管理 " in order:
            r = ""
            r_admin = ""
            Toset = ""
            for i in event.message:
                if isinstance(i, Segments.At):
                    Toset = str(i.qq)
                    
            if str(event.user_id) in SUPERS:
                if "管理 M " in order:
                    Toset = order[order.find("管理 M ") + len("管理 M "):].strip() if Toset == "" else Toset
                    logger.debug(f"try to get_user {Toset}")
                    nikename = await get_user_nickname(Toset, Manager, actions)
                    logger.debug(str(nikename))
                    if len(nikename) == 0:
                        r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: {Toset} 不是一个有效的用户。'''
                    else:
                        nikename = nikename
                        m = Manage_User
                        s = Super_User
                        if Toset in Manage_User:
                            r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
成功: {nikename}(@{Toset}) 已加入管理组 Manage_User 。'''
                        elif Toset in Super_User:
                            s.remove(Toset)
                            m.append(Toset)
                            if Write_Settings(s, m):
                                r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
成功: {nikename}(@{Toset}) 已加入管理组 Manage_User 。
现在发送 {reminder}帮助 了解你拥有的权限。'''
                                r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 将用户 {nikename}(@{Toset}) 从 Super_User 设置为了 Manage_User '''
                            else:
                                r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: 设置文件不可写。'''
                                r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 尝试将用户 {nikename}(@{Toset}) 设置为 Manage_User 但因为无法读写配置文件导致修改失败'''
                        elif Toset in ROOT_User:
                            r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败：指定的用户是 ROOT_User 且组 ROOT_User 为只读。'''
                            r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 尝试改变您的 ROOT_User 权限，已被阻止'''
                        else:
                            m.append(Toset)
                            if Write_Settings(s, m):
                                r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
成功: {nikename}(@{Toset}) 已加入管理组 Manage_User 。
现在发送 {reminder}帮助 了解你拥有的权限。'''
                                r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 将用户 {nikename}(@{Toset}) 设置为了 Manage_User '''
                            else:
                                r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: 设置文件不可写'''
                                r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 尝试将用户 {nikename}(@{Toset}) 设置为 Manage_User 但因为无法读写配置文件导致修改失败'''
                       
                elif "管理 S " in order:
                    Toset = order[order.find("管理 S ") + len("管理 S "):].strip() if Toset == "" else Toset
                    logger.debug(f"try to get_user {Toset}")
                    nikename = await get_user_nickname(Toset, Manager, actions)
                    logger.debug(str(nikename))
                    if len(nikename) == 0:
                        r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: {Toset} 不是一个有效的用户'''
                    else:
                        nikename = nikename
                        m = Manage_User
                        s = Super_User
                        if Toset in Manage_User:
                            m.remove(Toset)
                            s.append(Toset)
                            if Write_Settings(s, m):
                                r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
成功: {nikename}(@{Toset}) 已加入管理组 Super_User 。
现在发送 {reminder}帮助 了解你拥有的权限。'''
                                r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 将用户 {nikename}(@{Toset}) 从 Manage_User 设置为了 Super_User '''
                            else:
                                r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败：设置文件不可写。'''
                                r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 尝试将用户 {nikename}(@{Toset}) 设置为 Super_User 但因为无法读写配置文件导致修改失败'''
                        elif Toset in Super_User:
                            r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
成功: {nikename}(@{Toset}) 已加入管理组 Super_User 。'''
                        elif Toset in ROOT_User:
                            r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败：指定的用户是 ROOT_User 且组 ROOT_User 为只读。'''
                            r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 尝试改变您的 ROOT_User 权限，已被阻止'''
                        else:
                            s.append(Toset)
                            if Write_Settings(s, m):
                                r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
成功: {nikename}(@{Toset}) 已加入管理组 Super_User 。
现在发送 {reminder}帮助 了解你拥有的权限。'''
                                r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 将用户 {nikename}(@{Toset}) 设置为了 Super_User '''
                            else:
                                r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败：设置文件不可写。'''
                                r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 尝试将用户 {nikename}(@{Toset}) 设置为 Super_User 但因为无法读写配置文件导致修改失败'''
                else:
                    r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败：只能设置 Manage_User 或 Super_User 。'''
            else:
                r  = CONFUSED_WORD.format(bot_name=bot_name)

            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
            if r_admin:
                await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin))) #管理员操作通知ROOT用户
            
        elif "让我访问" in order:
            if str(event.user_id) in ADMINS:
                manage_users = await asyncio.gather(*[get_user_nickname_with_userid(uid, Manager, actions) for uid in Manage_User])
                super_users = await asyncio.gather(*[get_user_nickname_with_userid(uid, Manager, actions) for uid in Super_User])
                root_users = await asyncio.gather(*[get_user_nickname_with_userid(uid, Manager, actions) for uid in ROOT_User])
                r = f"""{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
Manage_User: {", ".join(manage_users)}
————————————————————
Super_User: {", ".join(super_users)}
————————————————————
ROOT_User: {", ".join(root_users)}
————————————————————
If you are a Super_User or ROOT_User, you can manage these users. Use {reminder}帮助 to know more.
""".strip()
            
            else:
                r  = CONFUSED_WORD.format(bot_name=bot_name)
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(r)))

        elif "插件视角" in order:
            status = f'''{bot_name} {bot_name_en} - 插件视角
————————————————————
✅ 已加载插件 ({len(loaded_plugins)}):
{chr(10).join(f"{i+1}. {str(plugin).rsplit('_', 1)[0]}" for i, plugin in enumerate(loaded_plugins)) if loaded_plugins else "无"}

❌ 已禁用插件 ({len(disabled_plugins)}):
{chr(10).join(
    f"{i+1}. {str(plugin).replace('d_', '').split('.')[0]}" 
    for i, plugin in enumerate(disabled_plugins)) if disabled_plugins else "无"}

⚠️ 加载失败 ({len(failed_plugins)}):
{chr(10).join(f"{i+1}. {str(plugin)}" 
    for i, plugin in enumerate(failed_plugins)) 
if failed_plugins else "无"}'''

            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(status)))
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
                content = [
                    (f"{reminder}让我访问", "检索有权限的用户"), # Managers' help content 管理员帮助
                    (f"{reminder}注销", "删除所有用户的上下文"),
                    (f"{reminder}修改 (hh:mm) (内容)", "改变定时消息时间与内容"),
                    (f"{reminder}感知", "查看运行状态"),
                    (f"{reminder}休眠", f"奖励{bot_name}精致睡眠 💤"),
                    (f"{reminder}重启", f"关闭所有线程和进程，关闭{bot_name}。然后重新启动{bot_name}。"),
                    (f"{reminder}启用插件（插件名称）", "启用特定插件"),
                    (f"{reminder}禁用插件（插件名称）", "忽略特定插件"),
                    (f"{reminder}重载插件", "重新加载所有插件"),
                    (f"{reminder}群发 (内容)", "在所有群聊中（黑名单群聊除外）发送一条消息"),
                    (f"{reminder}冷静 (@QQ+时间)/(@all)", "冷静用户一段时间"),
                    (f"{reminder}取消冷静 (@QQ)/(@all)", "解除用户冷静"),
                    (f"{reminder}送飞机票 (@QQ)", "将用户移出群聊"),
                    ("撤回【引用消息】", "撤回指定消息"),
                    (f"{reminder}群发黑名单", "管理群发消息时不会发送到的群聊"),
                    (f"{reminder}角色扮演", "管理角色预设"),
                    (f"{reminder}更改TTS状态", "切换语音回复功能（默认启用）"),
                    (f"{reminder}表情复述", "切换是否开启表情复述功能（默认启用）"),
                    (f"{reminder}设置全局后缀 (后缀)", "设置默认后缀（所有人）"),
                    (f"{reminder}删除全局后缀", "删除默认后缀（所有人）"),
                    (f"{reminder}设置特定后缀 (后缀)", "设置你的特定后缀（优先于全局）"),
                    (f"{reminder}删除特定后缀", "删除你的特定后缀")
                ]
                if is_qq_protocol():
                    content.append((f"{reminder}设置帮助模式 图片/文本", "切换帮助展示样式（仅QQ平台）"))
                
                if str(event.user_id) in SUPERS:
                    content += [
                        (f"{reminder}管理 M (QQ号)", "为用户添加 Manage_User 权限"),
                        (f"{reminder}管理 S (QQ号)", "为用户添加 Super_User 权限"),
                        (f"{reminder}删除管理 (QQ号)", "删除指定用户所有权限"),
                        (f"{reminder}退出本群", "退出当前群聊")
                    ]
                    
                command_lines = [
                    f"{idx+1}. {cmd} —> {desc}"
                    for idx, (cmd, desc) in enumerate(content)
                ]
                
                content = "\n".join([
                    f"管理我们的{bot_name}\n————————————————————",
                    f"你拥有管理{bot_name}的权限，以下是你可以使用的命令。若要查看普通帮助，请@{bot_name} 或发送【{reminder}用户帮助】",
                    *command_lines,
                    "你的每一步操作，与用户息息相关。"
                ])
                
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
            framework = await actions.get_version_info()
            framework = framework.data.raw
            
            # 读取模板
            template_path = os.path.join("static", "about_template.html")
            with open(template_path, "r", encoding="utf-8") as f:
                html_content = f.read()
                
            # 替换变量
            html_content = html_content.replace("{{bot_name}}", str(bot_name))
            html_content = html_content.replace("{{bot_name_en}}", str(bot_name_en))
            html_content = html_content.replace("{{ONE_SLOGAN}}", str(ONE_SLOGAN))
            html_content = html_content.replace("{{version_name}}", str(version_name))
            html_content = html_content.replace("{{app_name}}", str(framework.get("app_name", "Unknown")))
            html_content = html_content.replace("{{protocol_version}}", str(framework.get("protocol_version", "")))
            html_content = html_content.replace("{{app_version}}", str(framework.get("app_version", "")))
            html_content = html_content.replace("{{year}}", str(datetime.datetime.now().year))
            
            # 写入临时HTML
            temp_html_path = os.path.abspath(os.path.join("static", f"about_temp_{int(time.time())}.html"))
            with open(temp_html_path, "w", encoding="utf-8") as f:
                f.write(html_content)
                
            # 截图
            url = f"file:///{temp_html_path.replace(chr(92), '/')}"
            image_path = await capture_screenshot(url, "about_image", "png")
            
            # 发送
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(image_path)))
            
            # 清理
            try:
                os.remove(temp_html_path)
                os.remove(image_path)
            except:
                pass

        elif "群发黑名单" == order:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(f'''{bot_name} {bot_name_en} - 群发黑名单管理控制面板
————————————————————
{reminder}列出黑名单 —> 显示所有黑名单群组
{reminder}删除黑名单 +群号 —> 允许群发消息到该群
{reminder}添加黑名单 +群号 —> 禁止群发消息到该群
''')))

        elif f"设置全局后缀 " in order:
            if str(event.user_id) in ADMINS:
                suffix = order[order.find("设置全局后缀 ") + len("设置全局后缀 "):].strip()
                if suffix:
                    suffix_manager.set_global_suffix(suffix)
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"全局后缀已设置为：{suffix}")))
                else:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("后缀不能为空！")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))

        elif f"删除全局后缀" == order:
            if str(event.user_id) in ADMINS:
                suffix_manager.remove_global_suffix()
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("全局后缀已删除。")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))

        elif f"设置特定后缀 " in order:
            suffix = order[order.find("设置特定后缀 ") + len("设置特定后缀 "):].strip()
            if suffix:
                suffix_manager.set_user_suffix(event.user_id, suffix)
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"已为你配置特定后缀：{suffix}")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("后缀不能为空！")))

        elif f"删除特定后缀" == order:
            suffix_manager.remove_user_suffix(event.user_id)
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("你的特定后缀已删除。")))
            
        elif f"{reminder}角色扮演" == user_message:
            preset_list = "\n".join(
                [
                    f"    {reminder}{data['name']}（当前） - {data['info']}"
                    if data['name'] == presets_tool.current_preset
                    else f"    {reminder}{data['name']} - {data['info']}"
                    for data in presets.values()
                ]
            )

            prerequisites_info = f"""{bot_name} {bot_name_en} - 角色扮演后台
————————————————————
{presets_tool.list_presets(presets, presets_tool.current_preset, reminder)}

发送相应的关键词，{bot_name}会尽力扮演不同角色和你交流哒！⌯>ᴗoᴗ⌯ .ᐟ.ᐟ
————————————————————
若您是 Manage_User, Super_User 或 ROOT_User，你可以管理这些角色，尝试：
    {reminder}添加预设 [name] [info] : [content]
    {reminder}删除预设 [name]
其中，name 为角色名称， info 为预设简介， content 为预设内容。"""

            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(prerequisites_info)))

        elif f"添加预设 " in order:
            if str(event.user_id) in ADMINS:
                match = re.match(r"添加预设\s+(.+?)\s+(.+?)\s*[:：]\s*(.+)", order, re.DOTALL)
                if not match:
                    prerequisites_info = f"""{bot_name} {bot_name_en} - 角色扮演后台
————————————————————
添加预设 格式错误。
用法：{reminder}添加预设 [name] [info] : [content]
其中，name 为角色名称， info 为预设简介， content 为预设内容。

示例：{reminder}添加预设 助手 让{bot_name}成为你有帮助的助手！ : 你是一个有帮助的助手。"""

                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(prerequisites_info)))
                    return 

                name, info, content = match.groups()
                
                # 唯一标识符看起来太乱了，这里使用随机数生成预设id
                while True:
                    preset_id = "p" + str(random.randint(1000000, 9999999))
                    if not os.path.exists(os.path.join(PRESET_DIR, f"{preset_id}.txt")):
                        break

                # 检查是否已经存在具有相同 name 的预设
                existing_preset_id = None
                for pid, pdata in presets.items():
                    if pdata["name"] == name:
                        existing_preset_id = pid
                        break

                if existing_preset_id:
                    # 如果存在，则更新已存在的预设文件
                    preset_id = existing_preset_id
                    preset_path = os.path.join(PRESET_DIR, presets[preset_id]["path"])
                    with open(preset_path, "w", encoding="utf-8") as f:
                        f.write(content)
                    presets[preset_id]["info"] = info
                else:
                    # 如果不存在，则创建新的预设
                    preset_filename = f"{preset_id}.txt"
                    preset_path = os.path.join(PRESET_DIR, preset_filename)

                    with open(preset_path, "w", encoding="utf-8") as f:
                        f.write(content)

                    presets[preset_id] = {
                        "name": name,
                        "uid": [],
                        "info": info,
                        "path": preset_filename,
                    }
                    
                presets_tool.write_presets(presets)
                rootmsg = f"{'更新现有' if existing_preset_id else '添加'}预设: {name}"
                await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(f"用户 {event.user_id} 在群 {event.group_id} 中{rootmsg} "))) #管理员操作通知ROOT用户
                prerequisites_info = f"""{bot_name} {bot_name_en} - 角色扮演后台
————————————————————
已{'更新现有' if existing_preset_id else '添加'}预设: {name}"""
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(prerequisites_info)))
        
            else:
                r  = CONFUSED_WORD.format(bot_name=bot_name)
            
        elif f"删除预设 " in order:
            if str(event.user_id) in ADMINS:
                match = re.match(r"删除预设\s+(.+)", order)
                if not match:
                    prerequisites_info = f"""{bot_name} {bot_name_en} - 角色扮演后台
————————————————————
删除预设 格式错误。
用法：{reminder}删除预设 [name] 
其中，name 为角色名称。

示例：{reminder}删除预设 助手"""

                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(prerequisites_info)))
                    return 

                name = match.group(1).strip()

                preset_id_to_delete = None
                for preset_id, preset_data in presets.items():
                    if preset_data["name"] == name:
                        preset_id_to_delete = preset_id
                        break

                if preset_id_to_delete:
                    # 删除预设文件
                    preset_path = os.path.join(PRESET_DIR, presets[preset_id_to_delete]["path"])
                    logger.info(f"Removed {preset_path}")
                    os.remove(preset_path)

                # 从配置中删除预设
                del presets[preset_id_to_delete]
                
                presets_tool.write_presets(presets)
                await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(f"用户 {event.user_id} 在群 {event.group_id} 中删除 {name} 预设"))) #管理员操作通知ROOT用户
                prerequisites_info = f"""{bot_name} {bot_name_en} - 角色扮演后台
————————————————————
已删除预设: {name}"""
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(prerequisites_info)))

            else:
                r  = CONFUSED_WORD.format(bot_name=bot_name)
                
        elif "休眠" == order:
            if str(event.user_id) in ADMINS:
                stop_working = True
                r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 休眠QQ机器人'''
                await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin))) #管理员操作通知ROOT用户
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(suffix_manager.process_text(f"谢谢喵，{bot_name}睡觉去了 ヾ(＠ ˘ω˘ ＠)ノ💤", event.user_id))))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))

        elif f"{reminder}感知" in user_message:
            if str(event.user_id) in ADMINS:
                system_info = get_system_info()
                feel = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
系统当前运行状况
运行时间：{seconds_to_hms(round(time.time() - second_start, 2))}
系统版本：{system_info["version_info"]}
体系结构：{system_info["architecture"]}
CPU占用：{str(system_info["cpu_usage"]) + "%"}
内存占用：{str(system_info["memory_usage_percentage"]) + "%"}'''
                for i, usage in enumerate(system_info["gpu_usage"]):
                    feel = feel + f"\nGPU {i} Usage：{usage * 100:.2f}%"
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(feel)))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
            
        elif f"{reminder}注销" in user_message:
            if str(event.user_id) in ADMINS:
                # del cmc
                # cmc = ContextManager()
                user_lists.clear()
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"卸下包袱，{bot_name}更轻松了~ (/≧▽≦)/")))
                r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 手动清空了所有用户的 AI 对话上下文'''
                await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin))) #管理员操作通知ROOT用户
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
      
        elif f"{reminder}生成" == user_message:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(os.path.abspath("./assets/sc114.png"))))
            
        elif "修改 " in order:
            if str(event.user_id) in ADMINS:
                try:
                    tm = order[order.find("修改 ") + len("修改 "):].strip()
                    if not bool(re.match(r'^([01][0-9]|2[0-3]):([0-5][0-9])$', tm[:5])):
                        r = f'''{bot_name}不能识别给定的时间是什么 Σ( ° △ °|||)︴
举个🌰子：{reminder}修改 00:00 早安 —> 即可让{bot_name}在0点0分准时问候早安噢⌯oᴗo⌯'''
                    else:
                        timing_settings = f"{tm[:5]}⊕{tm[6::].strip()}"
                        with open("timing_message.ini", "w", encoding="utf-8") as f:
                            f.write(timing_settings)
                            f.close()
                        r = f"{bot_name}设置成功！(*≧▽≦) "
                        r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 将机器人的定时群发消息修改为时间：{tm[:5]} 
内容：{tm[6::]}'''
                        await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin))) #管理员操作通知ROOT用户
                except Exception as e:
                    r = f'''{str(type(e))}
{bot_name}设置失败了…… (╥﹏╥)'''
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
            
        elif f"{reminder}群发" in user_message:
            if str(event.user_id) in ADMINS:
                words = order.split(" ")
                if len(words) < 2 and len(event.message) == 1:
                    r = f'''群发格式错误 Σ( ° △ °|||)︴
举个🌰子：{reminder}群发 {bot_name}有更新新功能啦！ —> 在所有群聊中发送消息 “{bot_name}有更新新功能啦！”'''
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(r)))
                else:
                    logger.debug(f"消息长度: {len(event.message)}")
                    if len(event.message) > 0 and isinstance(event.message[0], Segments.Text):
                        new_text = str(event.message[0]).replace(f"{reminder}群发 ", "", 1) if f"{reminder}群发 " in str(event.message[0]) else str(event.message[0]).replace(f"{reminder}群发", "", 1)
                        if len(event.message) > 1:
                            m = Manager.Message(Segments.Text(new_text), *event.message[1:])
                        else:
                            m = Manager.Message(Segments.Text(new_text))
                    else:
                        m = event.message

                    words.pop(0)
                    word = " ".join(words)
                    r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 启动群发消息：\n'''
                    await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin), *m)) #管理员操作通知ROOT用户
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(f'''已启动群发消息：\n'''), *m))
                    await send_msg_all_groups(word, actions, m)
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
                
        elif f"{reminder}生草" == user_message:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("🌿")))

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
           if str(event.user_id) in ADMINS:
            start_index = order.find("取消冷静 ")
            if start_index != -1:
                result = order[start_index + len("取消冷静 "):].strip()
                numbers = re.findall(r'\d+', result)
                complete = False
                for i in event.message:
                    if isinstance(i, Segments.At):
                        logger.debug("At in loading...")
                        userid114 = numbers[0]  
                        time114 = 0
                        await actions.set_group_ban(group_id=event.group_id,user_id=userid114,duration=time114)
                        complete = True
                        break

                if not complete:
                    if "@all" in order:
                        await actions.custom.set_group_whole_ban(group_id=event.group_id, enable=False)
                        complete = True
                    else:
                        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"管理员：你的格式有误。\n格式：{reminder}取消冷静 @anyone/@all\n参考：{reminder}取消冷静 @Harcic#8042")))
     
           else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
                
        elif "冷静" in order:
            if str(event.user_id) in ADMINS:
                try:
                    start_index = order.find("冷静")
                    if start_index != -1:
                        result = order[start_index + len("冷静"):].strip()
                        numbers = re.findall(r'\d+', result)
                        complete = False
                        for i in event.message:
                            if isinstance(i, Segments.At):
                                userid114 = numbers[0]  
                                time114 = numbers[1]
                                
                                if str(userid114) == str(event.user_id):
                                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"你抖M是吧！{bot_name}生气了！自己找个没人的地方自己处理自己去，懒得理你 ┗(•̀へ •́ ╮)")))
                                    complete = None
                                else:
                                    await actions.set_group_ban(group_id=event.group_id, user_id=userid114, duration=time114)
                                    complete = True
                                    break 
                        
                        if complete is not None:
                            if not complete:
                                if "@all" in order:
                                    await actions.custom.set_group_whole_ban(group_id=event.group_id, enable=True)
                                    complete = True
                                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"管理员：已冷静。")))
                                else:
                                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"管理员：你的格式有误。\n格式：{reminder}冷静 @anyone/@all (seconds of duration)\n参考：{reminder}冷静 @Harcic#8042 128")))
                            else:
                                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"管理员：已冷静，时长 {time114} 秒。")))
                    
                except Exception as e:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"管理员：你的格式有误。\n格式：{reminder}冷静 @anyone/@all (seconds of duration)\n参考：{reminder}冷静 @Harcic#8042 128")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
          
        elif "送飞机票" in order:
          if str(event.user_id) in ADMINS:
                for i in event.message:
                    if isinstance(i, Segments.At):
                        await actions.set_group_kick(group_id=event.group_id,user_id=i.qq)
                        r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 使 {await get_user_nickname(i.qq, Manager, actions)} 退出了群聊：{event.group_id}'''
                        await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin))) #管理员操作通知ROOT用户
          else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))  
        
        elif f"{reminder}退出本群" == user_message:
            if str(event.user_id) in SUPERS:
                r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 使机器人退出了群聊：{event.group_id}'''
                await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin))) #管理员操作通知ROOT用户
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"呜呜呜，各位再见了……")))
                await asyncio.sleep(3)
                await actions.custom.set_group_leave(group_id=event.group_id, is_dismiss=True)
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
        elif "撤回" == user_message:
            if str(event.user_id) in ADMINS:
              if isinstance(event.message[0], Segments.Reply):
                try:
                  await actions.del_message(event.message[0].id)
                except:
                    pass
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
        elif f"{reminder}更改TTS状态" == user_message:
            global gptsovitsoff
            if gptsovitsoff: 
                gptsovitsoff = False
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"开启TTS成功！")))
            else:
                gptsovitsoff = True
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"关闭TTS成功！")))
                
        elif f"{reminder}表情复述" == user_message:
            if emoji_plus_one_off: 
                emoji_plus_one_off = False
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"开启表情复述成功！")))
            else:
                emoji_plus_one_off = True
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"关闭表情复述成功！")))
                
        elif f"{reminder}更改分配头衔开放状态" == user_message:
            global self_service_titles
            if str(event.user_id) in SUPERS:
                if self_service_titles:
                    self_service_titles = False
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"分配头衔功能已取消开放！")))
                else:
                    self_service_titles = True
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"分配头衔功能已开放！")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
                
        elif "给他人分配头衔" in order:
            if str(event.user_id) in SUPERS:
                try:
                    start_index = order.find("给他人分配头衔")
                    if start_index != -1:
                        result = order[start_index + len("给他人分配头衔"):].strip() 
                    match = re.search(r'(\d+)\s+(.+)', result)
                    if match:  
                        userid114 = match.group(1)  
                        title114 = match.group(2).strip() 

                        if len(title114) > 6:  
                            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("头衔不能超过6个字！")))
                        else:
                            try:  
                                await actions.custom.set_group_special_title(group_id=event.group_id, user_id=userid114, title=title114)
                                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("已设置！")))
                            except Exception as set_title_error:
                                logger.error(f"设置头衔失败: {set_title_error}")
                                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"设置头衔失败：{set_title_error}")))

                    else:   
                        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("指令格式有误，请使用 用户ID 头衔 的格式。")))

                except Exception as e: 
                    logger.error(f"处理分配头衔指令时出错: {e}")
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("格式有误或发生未知错误！")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
                
        elif f"分配头衔 " in order:
            titletext = order[order.find("分配头衔 ") + len("分配头衔 "):].strip()
            if len(titletext) > 6:
                await actions.send(group_id=event.group_id,message=Manager.Message(Segments.Text("头衔不能超过6个字！")))
            else:
                if str(event.user_id) in SUPERS:
                    await actions.custom.set_group_special_title(group_id=event.group_id,user_id=event.user_id,title=titletext)
                    await actions.send(group_id=event.group_id,message=Manager.Message(Segments.Text("已设置！")))
                else:
                    if self_service_titles:
                        await actions.custom.set_group_special_title(group_id=event.group_id,user_id=event.user_id,special_title=titletext,duration=-1)
                        await actions.send(group_id=event.group_id,message=Manager.Message(Segments.Text("已设置！")))
                    else:
                        await actions.send(group_id=event.group_id,message=Manager.Message(Segments.Text("当前功能未开放,请联系管理员(高级用户 或者 根用户)开放权限！")))
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
    global EnableNetwork, bot_name, reminder, plugins_help
    if isinstance(event, Events.GroupMessageEvent):
        lines = [
            f"如何与{bot_name}交流( •̀ ω •́ )✧",
            f"       注：对话前必须加上 {reminder} 噢！~",
            f"       {reminder}(任意问题，必填) —> {bot_name}回复",
            f"       {reminder}ai管理菜单 —> 切换和管理AI模型",
            f"       {reminder}插件视角 —> 看看{bot_name}又收集了哪些好好用的工具🔮{plugins_help}",
            f"       {reminder}角色扮演 —> {bot_name}切换不同的角色互动噢！~",
            f"       {reminder}设置特定后缀 (后缀) —> 给你自己的回复加后缀",
            f"       {reminder}删除特定后缀 —> 删除你自己的后缀",
        ]
        if is_feishu_protocol():
            lines.append(f"       {reminder}绑定QQ [QQ号] —> 绑定当前飞书账号到QQ")
            lines.append(f"       {reminder}我的绑定 —> 查看当前飞书账号绑定的QQ")
        if is_qq_protocol():
            lines.append(f"       {reminder}设置帮助模式 图片/文本 —> 切换帮助为图片或转发文本（仅QQ）")
        lines.append("快来聊天吧(*≧︶≦)")
        return "\n".join(lines)
    elif isinstance(event, Events.PrivateMessageEvent):
        lines = [
            f"如何与{bot_name}私聊( •̀ ω •́ )✧",
            f"       (任意问题，必填) —> {bot_name}回复",
            f"       {reminder}ai管理菜单 —> 查看你可用的私聊AI配置",
            f"       {reminder}切换AI [AI代码] —> 仅切换你自己的私聊AI",
            f"       {reminder}角色扮演 —> {bot_name}切换不同的角色互动噢！~",
        ]
        if is_qq_protocol():
            lines.append(f"       {reminder}设置帮助模式 图片/文本 —> 切换帮助为图片或文本（仅QQ）")
        if is_feishu_protocol():
            lines.append(f"       {reminder}绑定QQ [QQ号] —> 绑定当前飞书账号到QQ")
            lines.append(f"       {reminder}我的绑定 —> 查看当前飞书账号绑定的QQ")
        lines.append("快来聊天吧(*≧︶≦)")
        return "\n".join(lines)


async def send_help_visual(actions, event, content: str, reply_message_id: str = None):
    mode = get_help_mode(getattr(event, "user_id", ""))
    if isinstance(event, Events.GroupMessageEvent) and is_qq_protocol() and mode == "文本":
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        if not lines:
            lines = [content]
        try:
            nodes = [
                Segments.CustomNode(
                    str(event.self_id),
                    bot_name,
                    Manager.Message(Segments.Text(line))
                )
                for line in lines
            ]
            await actions.send_group_forward_msg(
                group_id=event.group_id,
                message=Manager.Message(*nodes)
            )
            return
        except Exception:
            logger.warning("群聊帮助转发发送失败，已回退为文本")
            if reply_message_id:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(reply_message_id), Segments.Text(content)))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(content)))
            return
    bg_path = os.path.join(os.path.dirname(__file__), "assets", "bg.jpeg")
    image_path = await create_help_message_image_async(content, bg_path)
    if isinstance(event, Events.PrivateMessageEvent):
        if is_qq_protocol() and mode == "文本":
            await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(content)))
            return
        if image_path:
            try:
                await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Image(image_path)))
                return
            except Exception:
                logger.warning("帮助图片发送失败，已回退为文本")
        await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(content)))
        return
    if isinstance(event, Events.GroupMessageEvent):
        if image_path:
            try:
                if reply_message_id:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(reply_message_id), Segments.Image(image_path)))
                else:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(image_path)))
                return
            except Exception:
                logger.warning("群聊帮助图片发送失败，已回退为文本")
        if reply_message_id:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(reply_message_id), Segments.Text(content)))
        else:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(content)))
        return
    await actions.send(group_id=getattr(event, "group_id", None), user_id=getattr(event, "user_id", None), message=Manager.Message(Segments.Text(content)))

Listener.run()

