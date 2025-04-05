import faulthandler
import json
faulthandler.enable()

import asyncio
import os
import importlib.util
import sys
import inspect
import random
import uuid
import re
import emoji
import time
import traceback
import requests
from Hyper import Configurator
import subprocess
import datetime 
import threading

# import framework
os.chdir(os.path.dirname(os.path.abspath(sys.argv[0])))
Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())
bot_name = Configurator.cm.get_cfg().others["bot_name"] #星·简
bot_name_en = Configurator.cm.get_cfg().others["bot_name_en"] #Shining girl
from Hyper import Listener, Events, Logger, Manager, Segments
from Hyper.Utils import Logic
from Hyper.Events import *

#import Tools functions
from Tools.GoogleAI import genai, Context, Parts, Roles
from Tools.SearchOnline import network_gpt as SearchOnline
from Tools.deepseek import dsr114 as deepseek
from Tools.tools import *
import prerequisites.prerequisite as presets_tool
print(title())

from urllib.parse import urlparse, urlunparse

config = Configurator.cm.get_cfg()
logger = Logger.Logger()
logger.set_level(config.log_level)
version_name = "3.0 - Next Preview Ultra"
cooldowns = {}
cooldowns1 = {}
second_start = time.time()
EnableNetwork = "Normal"
user_lists = {}
in_timing = False
generating = False
emoji_send_count: datetime = None
gptsovitsoff = False
self_service_titles = False
ONE_SLOGAN = Configurator.cm.get_cfg().others["slogan"]

class Tools:
    pass

generation_config = {
    "temperature": 1,
    "top_p": 0.95,
    "top_k": 64,
    "max_output_tokens": 8192,
    "response_mime_type": "text/plain",
}

sys_prompt = f''''''

model = genai.GenerativeModel()

key = Configurator.cm.get_cfg().others["gemini_key"]
reminder: str = Configurator.cm.get_cfg().others["reminder"]
genai.configure(api_key=key)

tools = []
ROOT_User: list = Configurator.cm.get_cfg().others["ROOT_User"]
Super_User: list = []
Manage_User: list = []

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

def replace_scheme_with_http(url: str) -> str:
    parsed_url = urlparse(url)
    if parsed_url.scheme == 'https':
        parsed_url = parsed_url._replace(scheme='http')
    return urlunparse(parsed_url)

# 插件加载器 NEXT 3
def load_plugins():
    global loaded_plugins, disabled_plugins, failed_plugins, plugins_help, reminder, bot_name, PLUGIN_FOLDER
    plugins = []
    plugins_help = ""

    loaded_plugins.clear()
    disabled_plugins.clear()
    failed_plugins.clear()

    for filename in os.listdir(PLUGIN_FOLDER):
        module_name = filename  # Folder name as module name
        print(f"check file or directory: {filename}")

        if filename == "__pycache__":
            print("Directory __pycache__ not load.")
            continue

        # 检查是否禁用
        if filename.startswith("d_"):
            disabled_plugins.append(module_name)
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
                    print(f"Loaded setup.py from folder plugin: {module_name}")

                    # Verify plugin
                    if hasattr(module, 'TRIGGHT_KEYWORD') and hasattr(module, 'on_message'):
                        if isinstance(module.TRIGGHT_KEYWORD, str):
                            plugins.append(module)  # Add module
                            loaded_plugins.append(unique_module_name) 
                            if hasattr(module, 'HELP_MESSAGE'):
                                if isinstance(module.HELP_MESSAGE, str):
                                    plugins_help += f"\n       {module.HELP_MESSAGE}"

                            print(f"已加载插件: {unique_module_name} (关键词: {module.TRIGGHT_KEYWORD})")
                        else:
                            failed_plugins.append(f"{unique_module_name} (TRIGGHT_KEYWORD 必须是字符串)")
                    else:
                        failed_plugins.append(f"{unique_module_name} (缺少 TRIGGHT_KEYWORD：触发标识符 或 on_message：触发函数后端)")

                except FileNotFoundError as e:
                    failed_plugins.append(f"{unique_module_name} (文件未找到: {e})")
                    print(f"加载插件 {unique_module_name} 失败，是因为: {e}")
                    if unique_module_name in sys.modules:
                        del sys.modules[unique_module_name]
                except ImportError as e:
                    failed_plugins.append(f"{unique_module_name} (导入错误: {e})")
                    print(f"加载插件 {unique_module_name} 失败，是因为: {e}")
                    if unique_module_name in sys.modules:
                        del sys.modules[unique_module_name]
                except Exception as e:
                    failed_plugins.append(f"{unique_module_name} (其他错误: {str(e)})")
                    print(f"加载插件 {unique_module_name} 失败，是因为: {e}")
                    if unique_module_name in sys.modules:
                        del sys.modules[unique_module_name]  # Cleanup

            else:
                print(f"目录 {filename} 中缺少 setup.py 文件")
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
                    print(f"模块 {unique_module_name} 已经加载，跳过")
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
                                    plugins_help += f"\n       {module.HELP_MESSAGE}"

                        print(f"已加载插件: {unique_module_name} (关键词: {module.TRIGGHT_KEYWORD})")
                    else:
                        failed_plugins.append(f"{unique_module_name} (TRIGGHT_KEYWORD 必须是字符串)")
                else:
                    failed_plugins.append(f"{unique_module_name} (缺少 TRIGGHT_KEYWORD：触发标识符 或 on_message：触发函数后端)")

            except FileNotFoundError as e:
                failed_plugins.append(f"{unique_module_name} (文件未找到: {e})")
                print(f"加载插件 {unique_module_name} 失败，原因是: {e}")
                if unique_module_name in sys.modules:
                    del sys.modules[unique_module_name]
            except ImportError as e:
                failed_plugins.append(f"{unique_module_name} (导入错误: {e})")
                print(f"加载插件 {unique_module_name} 失败，原因是: {e}")
                if unique_module_name in sys.modules:
                    del sys.modules[unique_module_name]
            except Exception as e:
                failed_plugins.append(f"{unique_module_name} (其他错误: {str(e)})")
                print(f"加载插件 {unique_module_name} 失败，是因为: {e}")
                if unique_module_name in sys.modules:
                    del sys.modules[unique_module_name]  # Cleanup

        else:
            print(f"跳过非插件文件或目录: {filename}")

    print(f"成功加载 {len(loaded_plugins)} 个插件")
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
                print(f"插件 {plugin_module.__name__} 执行出错，是因为: {e}")
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
            
cmc = ContextManager() # Gemini 的上下文管理器
             
def has_emoji(s: str) -> bool: # emoji +1 功能
    # 判断找到的 emoji 数量是否为 1 并且字符串的长度大于等于 1
    return emoji.emoji_count(s) == 1 and len(s) == 1

def timing_message(actions: Listener.Actions):

    while True:
        echo = asyncio.run(actions.custom.get_group_list())
        result = Manager.Ret.fetch(echo)

        if not os.path.isfile("timing_message.ini"):
            continue
        
        with open("timing_message.ini", "r", encoding="utf-8") as f:
            send_time = f.read()

        send_time = send_time.split("\n")
        send_time = send_time[0].split("⊕")
        print(send_time)

        now = datetime.datetime.now()
        print(f"now {now.hour:02}:{now.minute:02}")
        if f"{now.hour:02}:{now.minute:02}" == send_time[0]:
            print("send timing messages")
            blacklist = load_blacklist()  # 必须在发送消息前加载黑名单
            for group in result.data.raw:
                group_id = str(group['group_id'])  # 将group_id转为字符串类型,不然来个error会溶血
                if group_id not in blacklist:  # 检查群组 ID 是否在黑名单中,在就别给lz发
                    asyncio.run(actions.send(group_id=group['group_id'], message=Manager.Message(Segments.Text(send_time[1]))))
                    time.sleep(random.random()*3)
                else:
                   print(f"群聊{group_id}在黑名单内，取消发送")

        time.sleep(60 - now.second)

def Read_Settings():
    global Super_User, Manage_User
    with open("Super_User.ini", "r") as f:
        Super_User = f.read().split("\n")
        f.close()
    with open("Manage_User.ini", "r") as f:
        Manage_User = f.read().split("\n")
        f.close()


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
    global in_timing, bot_name, bot_name_en, reminder, ONE_SLOGAN

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

    if isinstance(event, Events.HyperListenerStartNotify):
        if os.path.exists("restart.temp"):
            with open("restart.temp", "r" ,encoding="utf-7") as f:
                group_id = f.read()
                f.close()
            os.remove("restart.temp")
            await actions.send(group_id=group_id, message=Manager.Message(Segments.Text(f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
Welcome! {bot_name} was restarted successfully. Now you can send {reminder}帮助 to know more.''')))

    if isinstance(event, Events.GroupMemberIncreaseEvent):
        user = event.user_id
        welcome = f''' 加入{bot_name}的大家庭，{bot_name}是你最忠实可爱的女朋友噢o(*≧▽≦)ツ
随时和{bot_name}交流，你只需要在问题的前面加上 {reminder} 就可以啦！( •̀ ω •́ )✧
{bot_name}是你最二次元的好朋友，经常@{bot_name} 看看{bot_name}又学会做什么新事情啦~o((>ω< ))o
祝你在{bot_name}的大家庭里生活愉快！♪(≧∀≦)ゞ☆'''
        
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(f"http://q2.qlogo.cn/headimg_dl?dst_uin={user}&spec=640"), Segments.Text("欢迎"), Segments.At(user), Segments.Text(welcome)))
        
    if isinstance(event, Events.GroupMemberDecreaseEvent):
        #print((await actions.get_stranger_info(event.user_id)).data.raw)
        user_nick = ""
        try:
            user_nick = f"@{(await actions.get_stranger_info(event.user_id)).data.raw["nickname"]} "
        except:
            user_nick = "有人又"

        text = f'''{user_nick}离开了{bot_name}的大家庭，{bot_name}好伤心o(TヘTo)……
大家一定要记得多来陪{bot_name}玩玩ヾ(•ω•`)o'''
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(text)))

    if isinstance(event, Events.GroupAddInviteEvent):
      keywords: list = Configurator.cm.get_cfg().others["Auto_approval"]
      cleaned_text = event.comment.strip().lower()

      for keyword6 in keywords:
          processed_keyword = keyword6.strip().lower()
          all_chars_present = True
          for char in processed_keyword:
              if char not in cleaned_text:
                  all_chars_present = False
                  break
          if all_chars_present:
              await actions.set_group_add_request(flag=event.flag, sub_type=event.sub_type, approve=True, reason="")
              await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"用户 {event.user_id} 的答案正确,已自动批准,题目数据为 {event.comment} ")))
              break
    if isinstance(event, Events.FriendAddEvent):
        print("同意好友")
        await actions.set_friend_add_request(flag=event.flag,approve=True,remark="")
        
    def execute_command(command):
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=True, shell=True)
            # capture_output=True 捕获输出(stdout/stderr)
            # text=True  解码为文本字符串,可以返回text
            # check=True  当返回非零退出码时引发 CalledProcessError 异常，开不开差不多（）
            # shell=True  允许使用 shell 的特性，不建议开

            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            }

        except subprocess.CalledProcessError as e:
            return {
                "stdout": e.stdout,
                "stderr": e.stderr,
                "returncode": e.returncode
            }
        except Exception as e:
            return {
                "stdout": None,
                "stderr": str(e),
                "returncode": -1
            }      
            
    if isinstance(event, Events.GroupMessageEvent):
        user_message = str(event.message)
        order = ""
        global user_lists
        global sys_prompt
        global second_start
        global EnableNetwork
        global generating
        global Super_User, Manage_User, ROOT_User
        global CONFIG_FILE, PRESET_DIR, NORMAL_PRESET
        global model, cmc

        event_user = (await actions.get_stranger_info(event.user_id)).data.raw
        event_user = event_user['nickname']
        print(event_user)
                    
        # 初始化预设
        sys_prompt = presets_tool.gen_presets(event.user_id, bot_name, event_user)
        presets = presets_tool.read_presets()

        if "ping" == user_message:
            print(str(event.user_id))
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("pong! 爆炸！v(◦'ωˉ◦)~♡ ")))
        elif f"{bot_name}真棒" in user_message and str(reminder) not in user_message:
            try:
                compliments: list = Configurator.cm.get_cfg().others["compliment"]
                m = str(compliments[random.randint(0, len(compliments))])
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(m)))
            except:
                print("不接受夸赞")        

        global emoji_send_count
        if has_emoji(user_message):
            if emoji_send_count is None or datetime.datetime.now() - emoji_send_count > datetime.timedelta(seconds=15):
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(user_message)))
                emoji_send_count = datetime.datetime.now()
            else:
                print(f"emoji +1 延迟 {abs(datetime.datetime.now() - emoji_send_count)} s")
        
        if user_message.startswith(reminder):
            order_i = user_message.find(reminder)
            if order_i != -1:
                order = user_message[order_i + len(reminder):].strip()
                print("收到命令 " + order)
        elif user_message.startswith(reminder):
            order_i = user_message.find(reminder)
            if order_i != -1:
                order = user_message[order_i + len(reminder):].strip()
                print("收到命令 " + order)

        if f"{reminder}重启" == user_message:
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"正在重启{bot_name}－O－……")))

                try:
                    with open("restart.temp", "w" ,encoding="utf-7") as f:
                        f.write(str(event.group_id))
                        f.close()
                except:
                    pass

                Listener.restart()
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))
        
        elif f"{reminder}重载插件" == user_message:
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
                global plugins
                plugins = load_plugins()

                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
外部后端已重载已完成。发送 {reminder}插件视角 以查看更多信息。''')))
                
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))
        elif f"{reminder}禁用插件 " in user_message:
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
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

                plugins = load_plugins()

                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
插件 {plugin_name} 已经成功启用''')))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))

        elif f"{reminder}启用插件 " in user_message:
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
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

                plugins = load_plugins() # 自动重载插件

                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
插件 {plugin_name} 已经成功启用''')))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))

        elif "runcommand " in order:
            blacklist_file = "blacklist.sr"
            if str(event.user_id) in Manage_User or str(event.user_id) in Super_User or str(event.user_id) in ROOT_User:
                order = order.removeprefix("runcommand").strip()
                order_lower = order.lower()
                print(f"检查并执行：{order_lower}")

                if re.search(r"rm", order) or re.search(r"vi", order) or re.search(r"vim", order) or re.search(r"tsab", order):
                    print("命令中包含危险字符")
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"""命令执行结果:
    ❌ ERROR 危险命令，已屏蔽。\nℹ️ INFO None.""")))      
                                 
                else:
                    command114514 = execute_command(order)
                    if command114514["returncode"] == 0:
                        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"""命令执行结果:
    ℹ️ INFO 执行成功
    ℹ️ INFO {command114514["stdout"]}.""")))
                    elif command114514["stderr"]:
                                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"""命令执行结果:
    ❌ ERROR 执行失败,代码命令可能有误
    ℹ️ INFO {command114514["stderr"]}.""")))
                    else:
                        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"""命令执行结果:
    ❌ ERROR 执行失败,代码命令可能有误
    ℹ️ INFO {command114514["stderr"]}.
    ❌ERROR 返回码:{command114514['returncode']}.""")))
                        
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))

        elif "默认4" == order:
            EnableNetwork = "Net"
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("嗯……我好像升级了！o((>ω< ))o")))
        elif "深度" == order:
            EnableNetwork = "Ds"
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("服务器……繁忙？ε٩(๑> ₃ <)۶з")))
        elif "默认3.5" == order:
            EnableNetwork = "Normal"
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("切换到大模型中运行ο(=•ω＜=)ρ⌒☆")))
        elif "读图" == order:
            EnableNetwork = "Pixmap"
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"{bot_name}打开了新视界！o(*≧▽≦)ツ")))

        elif "列出黑名单" == order:
          if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
            try:
                with open("blacklist.sr", "r", encoding="utf-8") as f:
                    blacklist1 = set(line.strip() for line in f) 
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名单列表加载完成: {blacklist1}")))
            except FileNotFoundError:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("黑名单列表加载失败,原因:没有文件")))
            except UnicodeDecodeError:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("黑名单列表加载失败,原因:解码失败")))
          else:
              await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))
        elif "添加黑名单 " in order:
            blacklist_file = "blacklist.sr"
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
                Toset2 = order[order.find("添加黑名单 ") + len("添加黑名单 "):].strip()
                blacklist114 = load_blacklist() # 加载现有的黑名单,防止已修改沒更新
                if Toset2 not in blacklist114:
                    blacklist114.add(Toset2) 
                    try:
                        with open(blacklist_file, "w", encoding="utf-8") as f:
                         for item in blacklist114:
                            f.write(item + "\n")  # 防止之前的丟失555，并添加换行符
                        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名单添加成功\n现在的黑名单: {blacklist114}")))
            
                    except Exception as e:
                       await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名单添加失败, 是因为\n{e}")))
                else:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名单添加失败,是因为{Toset2}已在黑名单")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))
        elif "删除黑名单 " in order:
            blacklist_file = "blacklist.sr"
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
                Toset1 = order[order.find("删除黑名单 ") + len("删除黑名单 "):].strip()
                blacklist117 = load_blacklist() # 加载现有的黑名单,防止已修改沒更新
                if Toset1 in blacklist117:
                    blacklist117.remove(Toset1) 
                    try:
                        with open(blacklist_file, "w", encoding="utf-8") as f:
                         for item in blacklist117:
                            f.write(item + "\n")  # 防止之前的丟失555，并添加换行符
                        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名单删除成功\n现在黑名单: {blacklist117}")))
                    except Exception as e:
                       await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名单删除失败, 是因为\n{e}")))
                else:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名单删除失败, 是因为群{Toset1}不在黑名单")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))
            
        elif "删除管理 " in order:
            r = ""
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User:
                Toset = order[order.find("删除管理 ") + len("删除管理 "):].strip()
                s = Super_User
                m = Manage_User
                if Toset in ROOT_User:
                    r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败：指定的用户是 ROOT_User 且组 ROOT_User 为只读。'''
                else:
                    if Toset in s:
                        s.remove(Toset)
                    if Toset in m:
                        m.remove(Toset)

                    if Write_Settings(s, m):
                        r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
成功: @{Toset} 现在是一个普通用户了。
现在发送 {reminder}帮助 了解你拥有的权限。'''
                    else:
                        r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败：设置文件不可写。'''
            else:
                r  = f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱"

            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
            
        elif "管理 " in order:
            r = ""
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User:
                if "管理 M " in order:
                    
                    Toset = order[order.find("管理 M ") + len("管理 M "):].strip()
                    print(f"try to get_user {Toset}")
                    nikename = (await actions.get_stranger_info(Toset, no_cache=True)).data.raw
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
Now use {reminder}帮助 to know what permissions you have now.'''
                            else:
                                r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: 设置文件不可写。'''
                        elif Toset in ROOT_User:
                            r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败：指定的用户是 ROOT_User 且组 ROOT_User 为只读。'''
                        else:
                            m.append(Toset)
                            if Write_zSettings(s, m):
                                r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
成功: {nikename}(@{Toset}) 已加入管理组 Manage_User 。
现在发送 {reminder}帮助 了解你拥有的权限。'''
                            else:
                                r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: 设置文件不可写'''
          
                       
                elif "管理 S " in order:
                    Toset = order[order.find("管理 S ") + len("管理 S "):].strip()
                    print(f"try to get_user {Toset}")
                    nikename = (await actions.get_stranger_info(Toset, no_cache=True)).data.raw
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
                            else:
                                r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败：设置文件不可写。'''
                        elif Toset in Super_User:
                            r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
成功: {nikename}(@{Toset}) 已加入管理组 Super_User 。'''
                        elif Toset in ROOT_User:
                            r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败：指定的用户是 ROOT_User 且组 ROOT_User 为只读。'''
                        else:
                            s.append(Toset)
                            if Write_Settings(s, m):
                                r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
成功: {nikename}(@{Toset}) 已加入管理组 Super_User 。
现在发送 {reminder}帮助 了解你拥有的权限。'''
                            else:
                                r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败：设置文件不可写。'''

                else:
                    r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败：只能设置 Manage_User 或 Super_User 。'''
            else:
                r  = f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱"

            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
        elif "让我访问" in order:
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
                r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
Manage_User: {Manage_User}
Super_User: {Super_User}
ROOT_User: {ROOT_User}
If you are a Super_User or ROOT_User, you can manage these users. Use {reminder}帮助 to know more.'''
            else:
                r  = f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱"
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))

        elif "插件视角" in order:
            status = f'''{bot_name} {bot_name_en} - 插件视角
————————————————————
✅ 已加载插件 ({len(loaded_plugins)}):
{chr(10).join(f"{i+1}. {str(plugin).rsplit('_', 1)[0]}" for i, plugin in enumerate(loaded_plugins)) if loaded_plugins else "无"}

❌ 已禁用插件 ({len(disabled_plugins)}):
{chr(10).join(f"{i+1}. {re.search(r"_(.*)\.", str(plugin)).group(1)}" for i, plugin in enumerate(disabled_plugins) if re.search(r"_(.*)\.", str(plugin)).group(1)) if disabled_plugins else "无"}

⚠️ 加载失败 ({len(failed_plugins)}):
{chr(10).join(f"{i+1}. {re.search(r"_(.*)\.", str(plugin)).group(1)}" for i, plugin in enumerate(failed_plugins) if re.search(r"_(.*)\.", str(plugin)).group(1)) if failed_plugins else "无"}'''
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(status)))

        elif "帮助" == order:
            if str(event.user_id) in ROOT_User or str(event.user_id) in Super_User:
                content = f'''管理我们的{bot_name}
————————————————————
你拥有管理{bot_name}的权限。若要查看普通帮助，请@{bot_name}
    1. {reminder}让我访问 —> 检索用有权限的用户
    2. {reminder}管理 M (QQ号，必填) —> 为用户添加 Manage_User 权限
    3. {reminder}管理 S (QQ号，必填) —> 为用户添加 Super_User 权限
    4. {reminder}删除管理 (QQ号，必填) —> 删除这个用户的全部权限
    5. {reminder}冷静 (@QQ+空格+时间(以秒为单位)，必填) —> 冷静用户一段时间
    6. {reminder}取消冷静 (@QQ，必填) —> 解除该用户冷静
    7. {reminder}送飞机票 (@QQ，必填) —> 将该用户送出聊群
    8. 撤回 (引用一条消息) —> 撤回该消息
    9. {reminder}注销 —> 删除所有用户的上下文
    10. {reminder}修改 (hh:mm) (内容，必填) —> 改变定时消息时间与内容
    11. {reminder}感知 —> 查看运行状态
    12. {reminder}核验 (QQ号，必填) —> 检索QQ账号信息
    13. {reminder}重启 —> 关闭所有线程和进程，关闭{bot_name}。然后重新启动{bot_name}。
    14. {reminder}启用插件（插件名称，必填） —> 启用加载特定插件
    15. {reminder}禁用插件（插件名称，必填） —> 忽略加载特定插件
    16. {reminder}重载插件 —> 退出所有插件，重新从本地拉取、效验和加载
    17. {reminder}添加黑名单 +空格 +群号 —> 将该群加入群发黑名单
    18. {reminder}删除黑名单 +空格 +群号 —> 将该群移除群发黑名单
    19. {reminder}列出黑名单 —> 列出黑名单中的所有群
    20. {reminder}角色扮演 —> 管理现有预设，或添加新的角色预设
    21. {reminder}更改TTS状态 —> 设置是否启用语音回复（默认启用）
    22. {reminder}退出本群 —> 退出本群
你的每一步操作，与用户息息相关。'''
            elif str(event.user_id) in Manage_User:
                content = f'''管理我们的{bot_name}
————————————————————
你拥有管理{bot_name}的权限。若要查看普通帮助，请@{bot_name}
    1. {reminder}让我访问 —> 检索用有权限的用户
    2. {reminder}注销 —> 删除所有用户的上下文
    3. {reminder}修改 (hh:mm) (内容，必填) —> 改变定时消息时间与内容
    4. {reminder}感知 —> 查看运行状态
    5. {reminder}核验 (QQ号，必填) —> 检索QQ账号信息
    6. {reminder}重启 —> 关闭所有线程和进程，关闭{bot_name}。然后重新启动{bot_name}
    7. {reminder}启用插件（插件名称，必填） —> 启用加载特定插件
    8. {reminder}禁用插件（插件名称，必填） —> 忽略加载特定插件
    9. {reminder}重载插件 —> 退出所有插件，重新从本地拉取、效验和加载
    10. {reminder}冷静 (@QQ+空格+时间(以秒为单位)，必填) —> 冷静用户一段时间
    11. {reminder}取消冷静 (@QQ，必填) —> 解除该用户冷静
    12. {reminder}送飞机票 (@QQ，必填) —> 将该用户送出聊群
    13. 撤回 (引用一条消息) —> 撤回该消息
    14. {reminder}添加黑名单 +空格 +群号 —> 将该群加入群发黑名单
    15. {reminder}删除黑名单 +空格 +群号 —> 将该群移除群发黑名单
    16. {reminder}列出黑名单 —> 列出黑名单中的所有群
    17. {reminder}角色扮演 —> 管理现有预设，或添加新的角色预设
    18. {reminder}更改TTS状态 —> 设置是否启用语音回复（默认启用）
    你的每一步操作，与用户息息相关。'''
            else:
                content = help_message()
                
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(content)))

        elif (isinstance(event.message[0], Segments.At) and 
              int(event.message[0].qq) == event.self_id): 

            if (all(isinstance(item, (Segments.At, Segments.Text)) for item in event.message) and 
                [str(s) for s in event.message if isinstance(s, Segments.Text) and not str(s).strip()]):

                content = help_message()
            else:
                content = '''你要询问什么呢？嘻嘻(●'◡'●)
和我聊天不需要@我哟(＾Ｕ＾)ノ~
直接在你想对{bot.name}想说的话前面加上 {reminder} 就行啦'''

            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(content)))

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

            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(prerequisites_info)))

        elif f"添加预设 " in order:
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
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
                r  = f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱"
            
        elif f"删除预设 " in order:
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
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
"已删除预设: {name}"""
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(prerequisites_info)))

            else:
                r  = f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱"
                
        elif "核验 " in order:
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
                uid = order[order.find("核验 ") + len("核验 "):].strip()
                print(f"try to get_user {uid}")
                nikename = (await actions.get_stranger_info(uid)).data.raw
                print(f"获取 {nikename} 成功")
                if len(nikename) == 0:
                    r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: {uid} 不是一个有效的用户'''
                else:
                    items = [f"{key}: {value}" for key, value in nikename.items()]
                    result = "\n".join(items)
                    r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
{result}'''
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))

        elif f"{reminder}感知" in str(event.message):
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
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
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))
            
        elif f"{reminder}注销" in str(event.message):
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
                del cmc
                cmc = ContextManager()
                user_lists.clear()
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"卸下包袱，{bot_name}更轻松了~ (/≧▽≦)/")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))
      
        elif f"{reminder}生成" == str(event.message):
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image("https://gchat.qpic.cn/gchatpic_new/0/0-0-615ECBFE6A1B895F3D2B21544109FE1F/0")))
        elif "修改 " in order:
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
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
                except Exception as e:
                    r = f'''{str(type(e))}
{bot_name}设置失败了…… (╥﹏╥)'''
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))
            
        elif f"{reminder}生草" == str(event.message):
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
           if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
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
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))
                
        elif "冷静" in order:
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
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
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))
          
        elif "送飞机票" in order:
          if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
                for i in event.message:
                    print(type(i))
                    print(str(i))
                    if isinstance(i, Segments.At):
                        print("At in loading...")
                        await actions.set_group_kick(group_id=event.group_id,user_id=i.qq)
          else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))  
        
        elif f"{reminder}退出本群" == user_message:
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"呜呜呜，各位再见了……")))
                await asyncio.sleep(3)
                await actions.custom.set_group_leave(group_id=event.group_id,is_dismiss=True)
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))
        elif "撤回" == user_message:
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
              if isinstance(event.message[0], Segments.Reply):
                try:
                  await actions.del_message(event.message[0].id)
                except:
                    pass
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))
        elif f"{reminder}更改TTS状态" == user_message:
            global gptsovitsoff
            if gptsovitsoff: 
                gptsovitsoff = False
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"开启TTS成功！")))
            else:
                gptsovitsoff = True
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"关闭TTS成功！")))
        elif f"{reminder}更改分配头衔开放状态" == user_message:
            global self_service_titles
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User:
                if self_service_titles:
                    self_service_titles = False
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"分配头衔功能已取消开放！")))
                else:
                    self_service_titles = True
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"分配头衔功能已开放！")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))
        elif "给他人分配头衔" in order:
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User:
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
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))
        elif f"分配头衔 " in order:
            titletext = order[order.find("分配头衔 ") + len("分配头衔 "):].strip()
            if len(titletext) > 6:
                await actions.send(group_id=event.group_id,message=Manager.Message(Segments.Text("头衔不能超过6个字！")))
            else:
                if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User:
                    await actions.set_group_special_title(group_id=event.group_id,user_id=event.user_id,title=titletext)
                    await actions.send(group_id=event.group_id,message=Manager.Message(Segments.Text("已设置！")))
                else:
                    if self_service_titles:
                        await actions.set_group_special_title(group_id=event.group_id,user_id=event.user_id,special_title=titletext,duration=-1)
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
            if len(order) >= 2: # 不响应小于两个字的废话，纯浪费token
                url = ""
                try:
                    match EnableNetwork:
                        case "Pixmap":
                            model = genai.GenerativeModel(
                                model_name="gemini-2.0-flash-thinking-exp-01-21", #gemini-2.0-flash-exp
                                generation_config=generation_config,
                                system_instruction=sys_prompt or None,
                            )

                            new = []
                            if isinstance(event.message[0], Segments.Reply):
                                print("有消息反馈")
                                content = await actions.get_msg(event.message[0].id)
                                message = gen_message({"message": content.data["message"]})
                                print("有引用消息")
                                for i in message:
                                    if isinstance(i, Segments.Text):
                                        new.append(Parts.Text(i.text.replace(reminder, "", 1)))
                                    elif isinstance(i, Segments.Image):
                                        if i.file.startswith("http"):
                                            url = i.file
                                        else:
                                            url = i.url
                                        new.append(Parts.File.upload_from_url(replace_scheme_with_http(url)))
                                        print("有图")

                            for i in event.message:
                                if isinstance(i, Segments.Text):
                                    new.append(Parts.Text(i.text.replace(reminder, "", 1)))
                                elif isinstance(i, Segments.Image):
                                    if i.file.startswith("http"):
                                        url = i.file
                                    else:
                                        url = i.url
                                    print(f"URL位置{replace_scheme_with_http(url)}")
                                    new.append(Parts.File.upload_from_url(replace_scheme_with_http(url)))
                                    print("有图")
            
                            new = Roles.User(*new)
                            result = cmc.get_context(event.user_id, event.group_id).gen_content(new).rstrip("\n")                 
                        case "Normal":
                            msg = ""
                            if isinstance(event.message[0], Segments.Reply):
                                content = await actions.get_msg(event.message[0].id)
                                message = gen_message({"message": content.data["message"]})
                                for i in message:
                                    if isinstance(i, Segments.Text):
                                        msg += f"{i.text} "

                            msg += order
                            search = SearchOnline(sys_prompt, msg, user_lists, event.user_id, "gpt-3.5-turbo-16k", bot_name, Configurator.cm.get_cfg().others["openai_key"])
                            ulist, result = search.Response()
                            user_lists = ulist
                        case "Ds":
                            msg = ""
                            if isinstance(event.message[0], Segments.Reply):
                                content = await actions.get_msg(event.message[0].id)
                                message = gen_message({"message": content.data["message"]})
                                for i in message:
                                    if isinstance(i, Segments.Text):
                                        msg += f"{i.text} "

                            msg += order
                            search = deepseek(sys_prompt, msg, user_lists, event.user_id, "deepseek-chat", bot_name, Configurator.cm.get_cfg().others["deepseek_key"])
                            ulist, result = search.Response()
                            user_lists = ulist
                        case "Net":
                            msg = ""
                            if isinstance(event.message[0], Segments.Reply):
                                content = await actions.get_msg(event.message[0].id)
                                message = gen_message({"message": content.data["message"]})
                                for i in message:
                                    if isinstance(i, Segments.Text):
                                        msg += f"{i.text} "

                            msg += order
                            search = SearchOnline(sys_prompt, msg, user_lists, event.user_id, "gpt-4o-mini", bot_name, Configurator.cm.get_cfg().others["openai_key"])
                            ulist, result = search.Response()
                            user_lists = ulist
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id),Segments.Text(result)))
                    
                    if gptsovitsoff == False:
                            """EdgeTTS 语音回复"""
                            TTSettings: dict = {}
                            if Configurator.cm.get_cfg().others["TTS"]:
                                if isinstance(Configurator.cm.get_cfg().others["TTS"], dict):             
                                    TTSettings = Configurator.cm.get_cfg().others["TTS"]
                                else:             
                                    TTSettings = dict(Configurator.cm.get_cfg().others["TTS"])
                            
                            communicate_completed: bool = False
                            if TTSettings != {}:
                                communicate_completed = await amain(result, TTSettings["voiceColor"], TTSettings["rate"], TTSettings["volume"], TTSettings["pitch"])
                            else:
                                print("EdgeTTS 配置文件不完整，或未配置，使用默认音色。")
                                communicate_completed = await amain(result, "zh-CN-XiaoyiNeural", "+0%", "+0%", "+0Hz")

                            if communicate_completed:
                                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Record(os.path.abspath(r"./responseVoice.wav"))))
                                os.remove(r"./responseVoice.wav")

                except UnboundLocalError:
                    raise
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id),Segments.Text(f"请稍等，{bot_name}在思考 🤔")))
                except TimeoutError:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id),Segments.Text(f"哎呀，你问的问题太复杂了，{bot_name}想不出来了 ┭┮﹏┭┮")))
                except Exception as e:
                    print(traceback.format_exc())
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id),Segments.Text(f"{type(e)}\n{url}\n{bot_name}发生错误，不能回复你的消息了，请稍候再试吧 ε(┬┬﹏┬┬)3")))
      
def help_message() -> str:
    global EnableNetwork, bot_name, reminder, plugins_help

    p = " "
    n = " "
    r = " "
    s = " "
    match EnableNetwork:
        case "Pixmap":
            p = "（当前）"
        case "Normal":
            r = "（当前）"
        case "Net":
            n = "（当前）"
        case "Ds":
            s = "（当前）"

    return f'''如何与{bot_name}交流( •̀ ω •́ )✧
    注：对话前必须加上 {reminder} 噢！~
       {reminder}(任意问题，必填) —> {bot_name}回复
       {reminder}读图{p}—> {bot_name}可以回复您发送的图片✅
       {reminder}默认4{n}—> {bot_name}更富有创造力的回复通道 🌟
       {reminder}默认3.5{r}—> {bot_name}的快速回复通道🎈
       {reminder}深度{s}—> 更加人性化和深度地回复问题✨{plugins_help}
       {reminder}插件视角 —> 看看{bot_name}又收集了哪些好好用的工具🔮
       {reminder}角色扮演 —> {bot_name}切换不同的角色互动噢！~
快来聊天吧(*≧︶≦)'''



Listener.run()
