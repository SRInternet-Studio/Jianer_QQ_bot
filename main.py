# -*- coding: utf-8 -*-

# 简儿 Jianer QQ 机器人项目
# Made by 思锐工作室
# link: https://github.com/SRInternet-Studio/Jianer_QQ_bot/

# import Tools functions
from Tools.tools import * 
print(title() + "\nWelcome to Jianer QQ Bot, Starting Kernal now...", end="\r") 

from Tools.GoogleAI import genai, Context, Parts, Roles, Schema
from Tools.SearchOnline import network_gpt as SearchOnline
from Tools.deepseek import dsr114 as deepseek
import prerequisites.prerequisite as presets_tool
from Tools.ai_diy import *
from Tools.other_ai import *
import requests
# import requirements
import faulthandler
faulthandler.enable()
from urllib.parse import urlparse, urlunparse
from threading import Thread
import sys, os, asyncio, traceback, threading
import importlib.util   
import inspect
import random
import uuid, re
import emoji
import time, datetime

# import framework
os.chdir(os.path.dirname(os.path.abspath(sys.argv[0])))
from Hyper import Configurator
Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())
from Hyper import Listener, Events, Logger, Manager, Segments
from Hyper.Utils import Logic
from Hyper.Events import *

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

logger = Logger.Logger()
logger.set_level(config.log_level)
version_name = "3.0 - Next Preview Ultra"

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
EnableNetwork = config.others["default_mode"]
user_lists = {}
class Tools:
    pass

class ContextManager:
    def __init__(self):
        self.groups: dict[int, dict[int, Context]] = {}

    def get_context(self, uin: int, gid: int):
        try:
            return self.groups[gid][uin]
        except KeyError:
            if self.groups.get(gid):
                self.groups[gid][uin] = Context(key, model, tools=tools)
                return self.groups[gid][uin]
            else:
                self.groups[gid] = {}
                self.groups[gid][uin] = Context(key, model, tools=tools)
                return self.groups[gid][uin]

generation_config = {
    "temperature": 1,
    "top_p": 0.95,
    "top_k": 64,
    "max_output_tokens": 8192,
    "response_mime_type": "text/plain",
}

sys_prompt = ""
model = genai.GenerativeModel()
cmc = ContextManager() # Gemini 的上下文管理器
tools = []

key = config.others["gemini_key"]
genai.configure(api_key=key)

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
# 监控文件(教师监控（？）)
class PluginWatcher:
    def __init__(self, plugin_folder, reload_callback, interval=2):
        self.plugin_folder = plugin_folder
        self.reload_callback = reload_callback
        self.interval = interval
        self._running = False
        self._file_mtimes = {}
        self._thread = None
        self._update_file_mtimes()
    
    def _update_file_mtimes(self):
        for root, _, files in os.walk(self.plugin_folder):
            for file in files:
                if file.endswith(('.py', '.pyw')) or file == 'setup.py':
                    path = os.path.join(root, file)
                    try:
                        self._file_mtimes[path] = os.path.getmtime(path)
                    except OSError:
                        pass
    
    def _check_files(self):
        changed = False
        for path, old_mtime in list(self._file_mtimes.items()):
            try:
                current_mtime = os.path.getmtime(path)
                if current_mtime != old_mtime:
                    self._file_mtimes[path] = current_mtime
                    changed = True
            except OSError:
                del self._file_mtimes[path]
                changed = True
        
        for root, _, files in os.walk(self.plugin_folder):
            for file in files:
                if file.endswith(('.py', '.pyw')) or file == 'setup.py':
                    path = os.path.join(root, file)
                    if path not in self._file_mtimes:
                        try:
                            self._file_mtimes[path] = os.path.getmtime(path)
                            changed = True
                        except OSError:
                            pass
        
        return changed
    
    def _watch(self):
        while self._running:
            if self._check_files():
                plugin_system.plugin_log("WARNING", "检测到插件文件变化，将自动重新加载插件！")
                self.reload_callback()
                self._update_file_mtimes()
            
            time.sleep(self.interval)
    
    def start(self):
        if not self._running:
            self._running = True
            self._thread = Thread(target=self._watch, daemon=True)
            self._thread.start()
    
    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join()

class PluginSystem:
    def __init__(self):
        self.plugins = []
        self.loaded_plugins = []
        self.disabled_plugins = []
        self.failed_plugins = []
        self.plugins_help = ""
        self.PLUGIN_FOLDER = "plugins"
        self.watcher = None
    
    def plugin_log(self, level, message):
        colors = {
            "SUCCESS": "\033[92m", 
            "ERROR": "\033[91m", 
            "WARNING": "\033[93m", 
            "INFO": "\033[94m"
        }
        reset = "\033[0m" 
        color = colors.get(level, "\033[97m")
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Jianer_Plugin_System {color}[{level}]{reset} {message}")
    
    def _cleanup_plugins(self):
        for module_name in list(sys.modules.keys()):
            if module_name.startswith(tuple(self.loaded_plugins)):
                del sys.modules[module_name]
    
    def load_plugins(self):
        self._cleanup_plugins()
        self.plugins = []
        self.plugins_help = ""
        self.loaded_plugins.clear()
        self.disabled_plugins.clear()
        self.failed_plugins.clear()

        for filename in os.listdir(self.PLUGIN_FOLDER):
            module_name = filename
            self.plugin_log("INFO", f"检查文件或目录: {filename}")

            if filename == "__pycache__":
                self.plugin_log("WARNING", "跳过 __pycache__ 目录")
                continue

            # 检查是否禁用
            if filename.startswith("d_"):
                self.disabled_plugins.append(module_name)
                self.plugin_log("WARNING", f"跳过已禁用插件: {module_name}")
                continue

            # 处理目录形式插件
            plugin_path = os.path.join(self.PLUGIN_FOLDER, filename)
            if os.path.isdir(plugin_path):
                setup_file = os.path.join(plugin_path, "setup.py")
                if os.path.exists(setup_file):
                    try:
                        unique_module_name = f"{module_name}_{uuid.uuid4().hex}"
                        spec = importlib.util.spec_from_file_location(unique_module_name, setup_file)
                        module = importlib.util.module_from_spec(spec)
                        sys.modules[unique_module_name] = module
                        spec.loader.exec_module(module)
                        self.plugin_log("INFO", f"从目录插件加载 setup.py: {module_name}")

                        if hasattr(module, 'TRIGGHT_KEYWORD') and hasattr(module, 'on_message'):
                            if isinstance(module.TRIGGHT_KEYWORD, str):
                                self.plugins.append(module)
                                self.loaded_plugins.append(unique_module_name)
                                if hasattr(module, 'HELP_MESSAGE') and isinstance(module.HELP_MESSAGE, str):
                                    self.plugins_help += f"\n       {module.HELP_MESSAGE}"

                                self.plugin_log("SUCCESS", f"已加载插件: {unique_module_name} (关键词: {module.TRIGGHT_KEYWORD})")
                            else:
                                self.failed_plugins.append(f"{module_name} (TRIGGHT_KEYWORD 必须是字符串)")
                                self.plugin_log("ERROR", f"{module_name} (TRIGGHT_KEYWORD 必须是字符串)")
                        else:
                            self.failed_plugins.append(f"{module_name} (缺少 TRIGGHT_KEYWORD 或 on_message)")
                            self.plugin_log("ERROR", f"{module_name} (缺少 TRIGGHT_KEYWORD 或 on_message)")

                    except FileNotFoundError as e:
                        self.failed_plugins.append(f"{module_name} (文件未找到: {e})")
                        self.plugin_log("ERROR", f"加载插件 {module_name} 失败: {e}")
                        if unique_module_name in sys.modules:
                            del sys.modules[unique_module_name]
                    except ImportError as e:
                        self.failed_plugins.append(f"{module_name} (导入错误: {e})")
                        self.plugin_log("ERROR", f"加载插件 {module_name} 失败: {e}")
                        if unique_module_name in sys.modules:
                            del sys.modules[unique_module_name]
                    except Exception as e:
                        self.failed_plugins.append(f"{module_name} (错误: {str(e)})")
                        self.plugin_log("ERROR", f"加载插件 {module_name} 失败: \n{traceback.format_exc()}")
                        if unique_module_name in sys.modules:
                            del sys.modules[unique_module_name]

                else:
                    self.plugin_log("WARNING", f"目录 {filename} 中缺少 setup.py 文件")
                    self.failed_plugins.append(f"{filename} (缺少 setup.py 文件)")

            # 处理文件形式插件
            elif filename.endswith((".py", ".pyw")):
                module_name = filename[:-3] if filename.endswith(".py") else filename[:-4]

                if filename.startswith("d_"):
                    self.disabled_plugins.append(str(module_name)[3:])
                    self.plugin_log("WARNING", f"跳过已禁用插件: {module_name}")
                    continue

                unique_module_name = f"{module_name}_{uuid.uuid4().hex}"

                try:
                    if unique_module_name in sys.modules:
                        self.plugin_log("WARNING", f"模块 {unique_module_name} 已经加载，跳过")
                        continue

                    spec = importlib.util.spec_from_file_location(
                        unique_module_name, 
                        os.path.join(self.PLUGIN_FOLDER, filename)
                    )
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[unique_module_name] = module
                    spec.loader.exec_module(module)

                    if hasattr(module, 'TRIGGHT_KEYWORD') and hasattr(module, 'on_message'):
                        if isinstance(module.TRIGGHT_KEYWORD, str):
                            self.plugins.append(module)
                            self.loaded_plugins.append(unique_module_name)
                            if hasattr(module, 'HELP_MESSAGE') and isinstance(module.HELP_MESSAGE, str):
                                self.plugins_help += f"\n       {module.HELP_MESSAGE}"

                            self.plugin_log("SUCCESS", f"已加载插件: {unique_module_name} (关键词: {module.TRIGGHT_KEYWORD})")
                        else:
                            self.failed_plugins.append(f"{module_name} (TRIGGHT_KEYWORD 必须是字符串)")
                            self.plugin_log("ERROR", f"{module_name} (TRIGGHT_KEYWORD 必须是字符串)")
                    else:
                        self.failed_plugins.append(f"{module_name} (缺少 TRIGGHT_KEYWORD 或 on_message)")
                        self.plugin_log("ERROR", f"{module_name} (缺少 TRIGGHT_KEYWORD 或 on_message)")

                except FileNotFoundError as e:
                    self.failed_plugins.append(f"{module_name} (文件未找到: {e})")
                    self.plugin_log("ERROR", f"加载插件 {module_name} 失败: {e}")
                    if unique_module_name in sys.modules:
                        del sys.modules[unique_module_name]
                except ImportError as e:
                    self.failed_plugins.append(f"{module_name} (导入错误: {e})")
                    self.plugin_log("ERROR", f"加载插件 {module_name} 失败: {e}")
                    if unique_module_name in sys.modules:
                        del sys.modules[unique_module_name]
                except Exception as e:
                    self.failed_plugins.append(f"{module_name} (错误: {str(e)})")
                    self.plugin_log("ERROR", f"加载插件 {module_name} 失败: \n{traceback.format_exc()}")
                    if unique_module_name in sys.modules:
                        del sys.modules[unique_module_name]

            else:
                self.plugin_log("WARNING", f"跳过非插件文件或目录: {filename}")

        self.plugin_log("SUCCESS", f"成功加载 {len(self.loaded_plugins)} 个插件")
        if self.failed_plugins:
            self.plugin_log("WARNING", f"加载失败的插件: {len(self.failed_plugins)} 个")
        return self.plugins
    
    def start_watching(self):
        if self.watcher is None:
            self.watcher = PluginWatcher(self.PLUGIN_FOLDER, self.load_plugins)
            self.watcher.start()
            self.plugin_log("INFO", "已启动插件文件监控 (HJB正在注视你（？）)")
    
    def stop_watching(self):
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None
            self.plugin_log("INFO", "已停止插件文件监控")

plugin_system = PluginSystem()
plugins = plugin_system.load_plugins()#在任何操作执行之前加载插件
plugin_system.start_watching()


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
                print(f"\n插件 {plugin_module.__name__} 执行出错，是因为: \n{traceback.format_exc()}")
                if not isAny:
                    has_plugin = True
    
    return has_plugin

def replace_scheme_with_http(url: str) -> str:
    parsed_url = urlparse(url)
    if parsed_url.scheme == 'https':
        parsed_url = parsed_url._replace(scheme='http')
    return urlunparse(parsed_url)

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
            continue
        
        with open("timing_message.ini", "r", encoding="utf-8") as f:
            send_time = f.read()

        send_time = send_time.split("\n")
        send_time = send_time[0].split("⊕")

        now = datetime.datetime.now()
        print(f"Current: {now.hour:02}:{now.minute:02}, target: {send_time}")
        if f"{now.hour:02}:{now.minute:02}" == send_time[0]:
            print("send timing messages")
            asyncio.run(send_msg_all_groups(send_time[1], actions))

        time.sleep(60 - now.second)
        
async def send_msg_all_groups(text, actions: Listener.Actions):
    echo = await actions.custom.get_group_list()
    result = Manager.Ret.fetch(echo)
    blacklist = load_blacklist()  # 必须在发送消息前加载黑名单
    print(f"sys: 群发 {result.data.raw}")
    for group in result.data.raw:
        group_id = str(group['group_id'])  # 将group_id转为字符串类型,不然来个error会溶血
        if group_id not in blacklist:  # 检查群组 ID 是否在黑名单中,在就别给lz发
            await actions.send(group_id=group['group_id'], message=Manager.Message(Segments.Text(text)))
            time.sleep(random.random()*3)
        else:
            print(f"群聊 {group_id} 在黑名单内，取消发送")


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
    print(f'''————————————————
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

@Listener.reg
@Logic.ErrorHandler().handle_async
async def handler(event: Events.Event, actions: Listener.Actions) -> None:
    global in_timing, bot_name, bot_name_en, reminder, config, ONE_SLOGAN, CONFUSED_WORD, stop_working, Wait_for_add_in
    global Super_User, Manage_User, ROOT_User
    ADMINS = Super_User + ROOT_User + Manage_User
    SUPERS = Super_User + ROOT_User
    event.time_str = f"{datetime.datetime.now().hour:02}:{datetime.datetime.now().minute:02}:{datetime.datetime.now().second:02}"
    
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
            print("sys: 触发停止运行事件")
            return

    if not in_timing:
        Read_Settings()
        in_timing = True
        thread = threading.Thread(target=timing_message, args=(actions,))
        thread.start()
        
    # 执行永久加载插件
    local_vars = globals().copy()
    local_vars.update(locals().copy())
    if await execute_plugins(True, **local_vars):
        return  # 只传递 event 作为位置参数
    
    if isinstance(event, Events.NotifyEvent): # 优先判断自定义事件
        if str(event.sub_type) == "poke" and event.group_id and int(event.target_id) == int(event.self_id): # 被戳一戳
            print(f"({event.user_id}) POKED")
            try:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(random.choice(config.others["poke_rejection_phrases"]))))
            except KeyError:
                print("不接受戳一戳")
                
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
Welcome! {bot_name} was restarted successfully. Now you can send {reminder}帮助 to know more.''')))

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
        s, user_nick = await get_user_info(event.user_id, Manager, actions)
        if s:
            user_nick = f"@{user_nick['nickname']} "
        else:
            user_nick = "有人又"

        text = f'''{user_nick}离开了{bot_name}的大家庭，{bot_name}好伤心o(TヘTo)……
大家一定要记得多来陪{bot_name}玩玩ヾ(•ω•`)o'''
        print(f"group: {event.user_id} 已离开群聊 {event.group_id}")
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(text)))

    elif isinstance(event, Events.GroupAddInviteEvent):
      keywords: list = config.others["Auto_approval"]
      cleaned_text = event.comment.strip().lower()

      for keyword in keywords:
        processed_keyword = keyword.strip().lower()
        if processed_keyword in cleaned_text: 
            try:
                user = event.user_id
                print(f"group: {await get_user_nickname(user, Manager, actions)} 的入群回答 {processed_keyword} 符合正确答案，已准许入群 {event.group_id}")
                await actions.set_group_add_request(flag=event.flag, sub_type=event.sub_type, approve=True, reason="")
                Wait_for_add_in = True
                welcome = f'''{await get_user_nickname(user, Manager, actions)} 的答案正确，欢迎加入{bot_name}的大家庭！o(*≧▽≦)ツ
随时和{bot_name}交流，只需在问题的前面加上 {reminder} 就可以啦！( •̀ ω •́ )✧
@{bot_name} 可以看看{bot_name}会做什么有趣的事情哦~o((>ω< ))o
祝你在{bot_name}的大家庭里生活愉快！♪(≧∀≦)ゞ☆'''  
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(f"http://q2.qlogo.cn/headimg_dl?dst_uin={user}&spec=640"), Segments.Text(welcome)))
                break
            except:
                traceback.print_exc()
          
    elif isinstance(event, Events.FriendAddEvent):
        print("sys: 同意好友")
        await actions.custom.set_friend_add_request(flag=event.flag,approve=True,remark="")
    elif isinstance(event, Events.PrivateMessageEvent):
        if "添加AI接口" in str(event.message):
            if str(event.user_id) in ADMINS:
                aiurl = str(event.message)[str(event.message).find("添加AI接口 ") + len("添加AI接口 "):].strip()
                match = re.match(r"^(https?://[^\s]+)\s+(.+)\s+(.+)\s+(.+)$", aiurl)
                if match:
                    url = match.group(1)
                    remark = match.group(2)
                    api_key = match.group(3)
                    model_name = match.group(4)
                    fhz = add_api(url, remark, api_key, model_name)
                    await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(fhz)))
                else:
                    await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(f"添加AI接口命令格式错误。请使用：{reminder}添加AI接口 <URL> <备注> <API Key> <模型名称> (URL必须以http://或https://开头)")))
        elif f"删除AI接口 " in str(event.message):
            if str(event.user_id) in ADMINS:
                aiurl = str(event.message)[str(event.message).find("删除AI接口 ") + len("删除AI接口 "):].strip()
                fhz = delete_api(aiurl)
                await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(fhz)))
        elif f"{reminder}查看AI接口" == str(event.message):
            fhz = list_api()
            await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(fhz)))
        elif f"设置AI接口 " in str(event.message):
                aiurl = str(event.message)[str(event.message).find("设置AI接口 ") + len("设置AI接口 "):].strip()
                fhz = set_api(aiurl)
                await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(fhz)))
    elif isinstance(event, Events.GroupMessageEvent):
        global user_lists
        global sys_prompt
        global second_start
        global EnableNetwork
        global generating
        global CONFIG_FILE, PRESET_DIR, NORMAL_PRESET
        global model, cmc, emoji_plus_one_off

        s, event_user = await get_user_info(event.user_id, Manager, actions)
        if s:
            event_user = event_user['nickname']
        else:
            event_user = str(event.user_id)
                    
        # 初始化预设
        sys_prompt = presets_tool.gen_presets(event.user_id, bot_name, event_user)
        presets = presets_tool.read_presets()
        
        if len(event.message) <= 0:
            return  # 只在函数中有效
        
        user_message = str(event.message)
        order = ""

        if "ping" == user_message:
            print(str(event.user_id))
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("pong! 爆炸！v(◦'ωˉ◦)~♡ ")))
            
        elif f"{bot_name}真棒" in user_message and str(reminder) not in user_message:
            try:
                compliments: list = config.others["compliment"]
                m = str(compliments[random.randint(0, len(compliments))])
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(m)))
            except:
                print("不接受夸赞")        

        global emoji_send_count
        if has_emoji(user_message) and not emoji_plus_one_off:
            if emoji_send_count is None or datetime.datetime.now() - emoji_send_count > datetime.timedelta(seconds=15):
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(user_message)))
                emoji_send_count = datetime.datetime.now()
            else:
                print(f"emoji +1 延迟 {abs(datetime.datetime.now() - emoji_send_count)} s")
        
        if user_message.startswith(reminder):
            order_i = user_message.find(reminder)
            if order_i != -1:
                order = user_message[order_i + len(reminder):].strip()
                print(f"({event_user}) ORDER: {repr(order)}")

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
                plugin_system = PluginSystem()
                plugin_system.stop_watching()
                plugins = plugin_system.load_plugins()
                plugin_system.start_watching()

                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
外部后端已重载已完成。发送 {reminder}插件视角 以查看更多信息。''')))
                
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
                    os.rename(found_path, new_path)

                plugin_system = PluginSystem()
                plugin_system.stop_watching()
                plugins = plugin_system.load_plugins()
                plugin_system.start_watching()

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
                    os.rename(found_path, original_path)

                plugin_system = PluginSystem()
                plugin_system.stop_watching()
                plugins = plugin_system.load_plugins()
                plugin_system.start_watching()

                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
插件 {plugin_name} 已经成功启用''')))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))

        elif "默认4" == order:
            EnableNetwork = "Net"
            print(f"sys: AI Mode change to ChatGPT-4")
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("嗯……我好像升级了！o((>ω< ))o")))
        elif "深度" == order:
            EnableNetwork = "Ds"
            print(f"sys: AI Mode change to DeepSeek")
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("服务器……繁忙？ε٩(๑> ₃ <)۶з")))
        elif "自定义AI" == order:
            EnableNetwork = "diy"
            print(f"sys: AI Mode change to DIY")
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("已切换到超级管理员自定义的ai！")))
        elif "默认3.5" == order:
            EnableNetwork = "Normal"
            print(f"sys: AI Mode change to ChatGPT-3.5")
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("切换到大模型中运行ο(=•ω＜=)ρ⌒☆")))
        elif "读图" == order:
            EnableNetwork = "Pixmap"
            print(f"sys: AI Mode change to Gemini")
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"{bot_name}打开了新视界！o(*≧▽≦)ツ")))

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
                    print(f"try to get_user {Toset}")
                    _, nikename = await get_user_info(Toset, Manager, actions)
                    print(str(nikename))
                    if len(nikename) == 0:
                        r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: {Toset} 不是一个有效的用户。'''
                    else:
                        nikename = nikename['nickname']
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
                    print(f"try to get_user {Toset}")
                    _, nikename = await get_user_info(Toset, Manager, actions)
                    print(str(nikename))
                    if len(nikename) == 0:
                        r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: {Toset} 不是一个有效的用户'''
                    else:
                        nikename = nikename['nickname']
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
                manage_users = await asyncio.gather(*[get_user_nickname(uid, Manager, actions) for uid in Manage_User])
                super_users = await asyncio.gather(*[get_user_nickname(uid, Manager, actions) for uid in Super_User])
                root_users = await asyncio.gather(*[get_user_nickname(uid, Manager, actions) for uid in ROOT_User])
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
                    (f"{reminder}冷静 (@QQ+时间)", "冷静用户一段时间"),
                    (f"{reminder}取消冷静 (@QQ)", "解除用户冷静"),
                    (f"{reminder}送飞机票 (@QQ)", "将用户移出群聊"),
                    ("撤回【引用消息】", "撤回指定消息"),
                    (f"{reminder}群发黑名单", "管理群发消息时不会发送到的群聊"),
                    (f"{reminder}角色扮演", "管理角色预设"),
                    (f"{reminder}更改TTS状态", "切换语音回复功能（默认启用）"),
                    (f"{reminder}表情复述", "切换是否开启表情复述功能（默认启用）")
                ]
                
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
                    *command_lines,
                    "你的每一步操作，与用户息息相关。"
                ])
                
            else:
                content = help_message()
                
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(content)))

        elif (isinstance(event.message[0], Segments.At) and 
              int(event.message[0].qq) == event.self_id): 

            if (all(isinstance(item, (Segments.At, Segments.Text)) for item in event.message) and 
                [str(s) for s in event.message if isinstance(s, Segments.Text) and not str(s).strip()]):

                content = help_message()
            else:
                content = f'''你要询问什么呢？嘻嘻(●'◡'●)
和我聊天不需要@我哟(＾Ｕ＾)ノ~
直接在你想对{bot_name}想说的话前面加上 {reminder} 就行啦'''

            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(content)))

        elif "关于" == order:
            global version_name
            about = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
构建信息：
版本：{version_name}
由 Lagrange.OneBot 驱动
基于 HypeR_bot 框架制作
————————————————————
第三方API
1. Mirokoi API
2. Lolicon API
2. LoliAPI API
4. ChatGPT 3.5
5. ChatGPT 4o-mini
6. Google gemini-2.0
7. GPT-SoVITS
8. EdgeTTS
————————————————————
© 2019~2025 思锐工作室 保留所有权利'''

            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(about)))

        elif "群发黑名单" == order:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(f'''{bot_name} {bot_name_en} - 群发黑名单管理控制面板
————————————————————
{reminder}列出黑名单 —> 显示所有黑名单群组
{reminder}删除黑名单 +群号 —> 允许群发消息到该群
{reminder}添加黑名单 +群号 —> 禁止群发消息到该群

如果想要关闭群发功能，请联系服务器管理员删除 `timing_message.ini` 文件。\n在关闭群发后，使用 -修改 功能即可重新启用。''')))
            
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
{preset_list}

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
                    print(f"Removed {preset_path}")
                    os.remove(preset_path)

                # 从配置中删除预设
                del presets[preset_id_to_delete]
                
                presets_tool.write_presets(presets)
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
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"谢谢喵，{bot_name}睡觉去了 ヾ(＠ ˘ω˘ ＠)ノ💤")))
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
                del cmc
                cmc = ContextManager()
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
                        timing_settings = f"{tm[:5]}⊕{tm[6::]}"
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
            
        elif "群发" in order:
            if str(event.user_id) in ADMINS:
                words = order.split(" ")
                if len(words) < 2:
                    r = f'''群发格式错误 Σ( ° △ °|||)︴
举个🌰子：{reminder}群发 {bot_name}有更新新功能啦！ —> 在所有群聊中发送消息 “{bot_name}有更新新功能啦！”'''
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(r)))
                else:
                    words.pop(0)
                    word = " ".join(words)
                    r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 启动群发消息:
“{word}”'''
                    await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin))) #管理员操作通知ROOT用户
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(f'''已启动群发消息 “{word}”''')))
                    await send_msg_all_groups(word, actions)
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
                for i in event.message:
                    if isinstance(i, Segments.At):
                        print("At in loading...")
                        userid114 = numbers[0]  
                        time114 = 0
                        await actions.set_group_ban(group_id=event.group_id,user_id=userid114,duration=time114)
     
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
                                print("At in loading...")
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
                                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"管理员：你的格式有误。\n格式：{reminder}冷静 @anyone (seconds of duration)\n参考：{reminder}冷静 @Harcic#8042 128")))
                            else:
                                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"管理员：已冷静，时长 {time114} 秒。")))
                    
                except Exception as e:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"管理员：你的格式有误。\n格式：{reminder}冷静 @anyone (seconds of duration)\n参考：{reminder}冷静 @Harcic#8042 128")))
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
                                await actions.set_group_special_title(group_id=event.group_id, user_id=userid114, title=title114)
                                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("已设置！")))
                            except Exception as set_title_error:
                                print(f"设置头衔失败: {set_title_error}")
                                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"设置头衔失败：{set_title_error}")))

                    else:   
                        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("指令格式有误，请使用 用户ID 头衔 的格式。")))

                except Exception as e: 
                    print(f"处理分配头衔指令时出错: {e}")
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("格式有误或发生未知错误！")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
                
        elif f"分配头衔 " in order:
            titletext = order[order.find("分配头衔 ") + len("分配头衔 "):].strip()
            if len(titletext) > 6:
                await actions.send(group_id=event.group_id,message=Manager.Message(Segments.Text("头衔不能超过6个字！")))
            else:
                if str(event.user_id) in SUPERS:
                    await actions.set_group_special_title(group_id=event.group_id,user_id=event.user_id,title=titletext)
                    await actions.send(group_id=event.group_id,message=Manager.Message(Segments.Text("已设置！")))
                else:
                    if self_service_titles:
                        await actions.set_group_special_title(group_id=event.group_id,user_id=event.user_id,special_title=titletext,duration=-1)
                        await actions.send(group_id=event.group_id,message=Manager.Message(Segments.Text("已设置！")))
                    else:
                        await actions.send(group_id=event.group_id,message=Manager.Message(Segments.Text("当前功能未开放,请联系管理员(高级用户 或者 根用户)开放权限！")))
        elif "添加AI接口" in order:
            if str(event.user_id) in ADMINS:
                aiurl = order[order.find("添加AI接口 ") + len("添加AI接口 "):].strip()
                match = re.match(r"^(https?://[^\s]+)\s+(.+)\s+(.+)\s+(.+)$", aiurl)
                if match:
                    url = match.group(1)
                    remark = match.group(2)
                    api_key = match.group(3)
                    model_name = match.group(4)
                    fhz = add_api(url, remark, api_key, model_name)
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(fhz)))
                else:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"添加AI接口命令格式错误。请使用：{reminder}添加AI接口 <URL> <备注> <API Key> <模型名称> (URL必须以http://或https://开头)")))
        elif f"删除AI接口 " in order:
            if str(event.user_id) in ADMINS:
                aiurl = order[order.find("删除AI接口 ") + len("删除AI接口 "):].strip()
                fhz = delete_api(aiurl)
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(fhz)))
        elif f"{reminder}查看AI接口" == user_message:
            fhz = list_api()
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(fhz)))
        elif f"设置AI接口 " in order:
                aiurl = order[order.find("设置AI接口 ") + len("设置AI接口 "):].strip()
                fhz = set_api(aiurl)
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(fhz)))
        # elif "6" == user_message:
        #         await actions.send(group_id=event.group_id,message=Manager.Message(Segments.Image(os.path.abspath("./stcn6.png"))))
        #         await actions.set_group_ban(group_id=event.group_id,user_id=event.user_id,duration=600)
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
                del cmc # 注销
                cmc = ContextManager()
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
                print(f"处理插件时发生错误: {e}")
                return
            
            # 3. 全都匹配不到，进入AI回复
            MAX_MESSAGE_LENGTH = 3
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
                    print(f"AI: URL位置 {replace_scheme_with_http(url)}")
                    container.append(Parts.File.upload_from_url(replace_scheme_with_http(url)))
                    print("AI: 有图")

            async def handle_message_stream(response_stream, is_openai=True):
                nonlocal result, sended, enable_forward_msg_num
                for partial, r_type in response_stream:
                    if is_openai:
                        if r_type != 'message':
                            user_lists = partial
                            continue

                    message = Segments.Text(str(partial))
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
                    result += str(partial) + '\n'

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
                match EnableNetwork:
                    case "Pixmap":
                        new = await build_message_content()
                        model = genai.GenerativeModel(
                            model_name="gemini-2.0-flash-thinking-exp-01-21",
                            generation_config=generation_config,
                            system_instruction=sys_prompt or None,
                        )
                        response_stream = cmc.get_context(event.user_id, event.group_id).gen_content(Roles.User(*new))
                        await handle_message_stream(response_stream, False)

                    case "Normal" | "Net":
                        model_name = "gpt-3.5-turbo-16k" if EnableNetwork == "Normal" else "gpt-4o-mini"
                        msg = ""
                        await process_reply_message()
                        msg += order
                        search = SearchOnline(
                            sys_prompt, msg, user_lists, event.user_id, 
                            model_name, bot_name, 
                            config.others["openai_key"]
                        )
                        await handle_message_stream(search.Response())

                    case "Ds":
                        msg = ""
                        await process_reply_message()
                        msg += order
                        search = deepseek(
                            sys_prompt, msg, user_lists, event.user_id,
                            "deepseek-chat", bot_name,
                            config.others["deepseek_key"]
                        )
                        await handle_message_stream(search.Response())
                    case "diy":
                        current_url, current_remark, current_api_key, current_model_name = get_current_api()
                        if current_remark == None:
                            await actions.send(group_id=event.group_id,message=Manager.Message(Segments.Text("请先选择AI接口")))
                            return None
                        else:
                            msg = ""
                            await process_reply_message()
                            msg += order
                            search = OtherAI(
                                sys_prompt, msg, user_lists, event.user_id,
                                current_model_name, bot_name,
                                current_api_key,current_url
                            )
                            await handle_message_stream(search.Response())
                result = result.rstrip()
                await finalize_messages()
                
                if not sended:
                    await actions.send(
                        group_id=event.group_id,
                        message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(result))
                    )
                    
                if gptsovitsoff == False:
                            apiurl = 'http://127.0.0.1:9880'
                            params = {
                                'text': result,
                                'text_language': 'zh'
                            }
                            logger.log(f"请求GPT-SOVITS接口，URL：{apiurl}，参数：{params}",level=Logger.levels.DEBUG)
                            try:
                                response = requests.get(apiurl, params=params, stream=True)
                                logger.log(f"收到GPT-SOVITS接口响应，状态码：{response.status_code}",level=Logger.levels.DEBUG) 

                                if response.status_code == 200:
                                    with open('output_audio.wav', 'wb') as file:
                                        for chunk in response.iter_content(chunk_size=1024):
                                            if chunk:
                                                file.write(chunk)
                                    print("OK")
                                    logger.log("成功请求GPT-SOVITS接口", level=Logger.levels.DEBUG)
                                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Record(os.path.abspath(r"./output_audio.wav"))))
                                else:
                                    logger.log(f"请求GPT-SOVITS接口失败，状态码：{response.status_code}，内容：{response.text}",level=Logger.levels.ERROR)
                                    raise

                            except Exception:
                                logger.log("请求接口失败,更换edgetts",level=Logger.levels.DEBUG)
                                communicate_completed = await amain(result, "zh-CN-XiaoyiNeural")
                                if communicate_completed:
                                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Record(os.path.abspath(r"./responseVoice.wav"))))
                                    os.remove(r"./responseVoice.wav")

            except UnboundLocalError:
                raise
            except TimeoutError:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id),Segments.Text(f"哎呀，你问的问题太复杂了，{bot_name}想不出来了 ┭┮﹏┭┮")))
            except Exception as e:
                print(traceback.format_exc())
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id),Segments.Text(f"{type(e)}\n{url}\n{bot_name}发生错误，不能回复你的消息了，请稍候再试吧 ε(┬┬﹏┬┬)3")))
      
def help_message() -> str:
    global EnableNetwork, bot_name, reminder, plugins_help
    return f'''如何与{bot_name}交流( •̀ ω •́ )✧
    注：对话前必须加上 {reminder} 噢！~
       {reminder}(任意问题，必填) —> {bot_name}回复
       {reminder}读图{"（当前）" if EnableNetwork == "Pixmap" else ""} —> {bot_name}可以回复您发送的图片✅
       {reminder}默认4{"（当前）" if EnableNetwork == "Net" else ""} —> {bot_name}更富有创造力的回复通道 🌟
       {reminder}默认3.5{"（当前）" if EnableNetwork == "Normal" else ""} —> {bot_name}的快速回复通道🎈
       {reminder}深度{"（当前）" if EnableNetwork == "Ds" else ""} —> 更加人性化和深度地回复问题✨{PluginSystem.plugins_help}
       {reminder}插件视角 —> 看看{bot_name}又收集了哪些好好用的工具🔮
       {reminder}角色扮演 —> {bot_name}切换不同的角色互动噢！~
快来聊天吧(*≧︶≦)'''

Listener.run()