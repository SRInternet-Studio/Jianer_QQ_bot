#!/bin/python 
import faulthandler
faulthandler.enable()

import asyncio
import datetime
import os
import random
import re
import base64
import urllib.parse
import emoji
import time
import traceback
from openai import OpenAI
import requests, aiohttp
from Hyper import Configurator
import platform
import psutil
import GPUtil
import subprocess
from typing import Set
from PIL import Image
import io
import threading
import paramiko

# import framework
Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())
bot_name = Configurator.cm.get_cfg().others["bot_name"] #星·简
bot_name_en = Configurator.cm.get_cfg().others["bot_name_en"] #Shining girl
from Hyper import Listener, Events, Logger, Manager, Segments
from Hyper.Utils import Logic
from Hyper.Events import *

#import moudles
from GoogleAI import genai, Context, Parts, Roles
# from google.generativeai.types import FunctonDeclaration
from SearchOnline import network_gpt as SearchOnline
from prerequisites import prerequisite
import Quote
                            
config = Configurator.cm.get_cfg()
logger = Logger.Logger()
logger.set_level(config.log_level)
version_name = "2.0"
cooldowns = {}
cooldowns1 = {}
second_start = time.time()
EnableNetwork = "Pixmap"
user_lists = {}
in_timing = False
emoji_send_count: datetime = None

generating = False

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
sisters: list = []
jhq: list = []

def load_blacklist():
    try:
        with open("blacklist.sr", "r", encoding="utf-8") as f:
            blacklist115 = set(line.strip() for line in f)  # 使用集合方便快速查找,不然容易溶血
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


cmc = ContextManager()
             
def has_emoji(s: str) -> bool:
    # 判断找到的 emoji 数量是否为 1 并且字符串的长度大于等于 1
    return emoji.emoji_count(s) == 1 and len(s) == 1

def timing_message(actions: Listener.Actions):

    while True:
        echo = asyncio.run(actions.custom.get_group_list())
        result = Manager.Ret.fetch(echo)

        with open("timing_message.ini", "r", encoding="utf-8") as f:
            send_time = f.read()

        send_time = send_time.split("\n")
        send_time = send_time[0].split("⊕")
        print(send_time)

        now = datetime.datetime.now()
        print(f"now {now.hour:02}:{now.minute:02}")
        if f"{now.hour:02}:{now.minute:02}" == send_time[0]:
            print("send timing messages")
            blacklist = load_blacklist()  # 在发送消息前加载黑名单,防止返回一个sb空集合
            for group in result.data.raw:
                group_id = str(group['group_id'])  # 将group_id转为字符串类型,不然来个error会溶血
                if group_id not in blacklist:  # 检查群组 ID 是否在黑名单中,在就别给lz发
                    asyncio.run(actions.send(group_id=group['group_id'], message=Manager.Message(Segments.Text(send_time[1]))))
                    time.sleep(random.random()*3)
                else:
                   print(f"群聊{group_id} TM在黑名单,发NM555")

        time.sleep(60 - now.second)

def Read_Settings():
    global Super_User, Manage_User, sisters, jhq
    with open("Super_User.ini", "r") as f:
        Super_User = f.read().split("\n")
        f.close()
    with open("Manage_User.ini", "r") as f:
        Manage_User = f.read().split("\n")
        f.close()
    with open("sisters.ini", "r") as f:
        sisters = f.read().split("\n")
        f.close()
    with open("jhq.ini", "r") as f:
        jhq = f.read().split("\n")
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

    global in_timing, bot_name, bot_name_en, reminder
    if not in_timing:
        Read_Settings()
        in_timing = True
        thread = threading.Thread(target=timing_message, args=(actions,))
        thread.start()

    if isinstance(event, Events.HyperListenerStartNotify):
        if os.path.exists("restart.temp"):
            with open("restart.temp", "r" ,encoding="utf-7") as f:
                group_id = f.read()
                f.close()
            os.remove("restart.temp")
            await actions.send(group_id=group_id, message=Manager.Message(Segments.Text(f'''{bot_name} {bot_name_en} - 简单 可爱 个性 全知
————————————————————
Welcome! {bot_name} was restarted successfully. Now you can send {reminder}帮助 to know more.''')))

    if isinstance(event, Events.GroupMemberIncreaseEvent):
        user = event.user_id
        welcome = f''' 加入{bot_name}的大家庭，{bot_name}是你最忠实可爱的女朋友噢o(*≧▽≦)ツ
随时和{bot_name}交流，你只需要在问题的前面加上 {reminder} 就可以啦！( •̀ ω •́ )✧
{bot_name}是你最二次元的好朋友，经常@{bot_name} 看看{bot_name}又学会做什么新事情啦~o((>ω< ))o
祝你在{bot_name}的大家庭里生活愉快！♪(≧∀≦)ゞ☆'''
        
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(f"http://q2.qlogo.cn/headimg_dl?dst_uin={user}&spec=640"), Segments.Text("欢迎"), Segments.At(user), Segments.Text(welcome)))


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

    def execute_command(command):
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=True, shell=True)
            # capture_output=True 捕获输出(stdout/stderr)
            # text=True  解码为文本字符串,可以返回text
            # check=True  当返回非零退出码时引发 CalledProcessError 异常,开不开差不多（）
            # shell=True  允许使用 shell 的特性，不建议开,不然容易溶血

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
        global Super_User, Manage_User, ROOT_User, sisters,jhq
        global model

        event_user = (await actions.get_stranger_info(event.user_id)).data.raw
        event_user = event_user['nickname']
        print(event_user)

        # match str(event.message):
        #     case "ping":
        #         await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("pong")))
        #     case "/生图 Pixiv":
        #         await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image("https://pixiv.t.sr-studio.top/img-original/img/2023/01/24/03/53/38/104766095_p0.png")))   
        print(event.user_id)
        if str(event.user_id) in jhq:
            print("My Kids")
            sys_prompt = prerequisite(bot_name, event_user).mother()
        else:    
            if str(event.user_id) in sisters:
                print("My little sister")
                sys_prompt = prerequisite(bot_name, event_user).sister()
            else:
                sys_prompt = prerequisite(bot_name, event_user).girl_friend()

        if "ping" == user_message:
            print(str(event.user_id))
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("pong! 爆炸！v(◦'ωˉ◦)~♡ ")))

        elif f"{bot_name}真棒" in user_message:
            i = random.randint(1,3)
            match i:
                case 1:
                    m = "啊！老……老公，别怎么说啦，人……人家好害羞的啦，人家还会努力的(*ᴗ͈ˬᴗ͈)ꕤ*.ﾟ"
                case 2:
                    m = "啊~老公~你不要这么夸人家啦~〃∀〃"
                case 3:
                    m = "唔……谢……谢谢老公啦🥰~"
                    
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(m)))

        # not_allowed_word = ["小塑塑真棒", "小塑塑棒不棒"]
        # for item in not_allowed_word:
        #     contains = []
        #     for p in range(len(item)):
        #         if item[p] in user_message:
        #             contains.append("1")
        #     if len(contains) >= len(item):
        #         try:
        #             await actions.del_message(event.message_id)
        #         except:
        #             pass
        #         break

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
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"Restarting in progress……")))

                try:
                    with open("restart.temp", "w" ,encoding="utf-7") as f:
                        f.write(str(event.group_id))
                        f.close()
                except:
                    pass

                Listener.restart()
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))
        

        elif "runcommand " in order:
            blacklist_file = "blacklist.sr"
            
            if str(event.user_id) in Manage_User or str(event.user_id) in Super_User or str(event.user_id) in ROOT_User:
                order = order.removeprefix("runcommand").strip()
                order_lower = order.lower()
                print(order_lower)

                # 定义危险命令
                dangerous_commands = ["rm", "vi", "vim", "tsab", "del", "rmdir", "format", "shutdown", "shutdown.exe"]

                # 检查危险命令
                if any(dangerous_cmd in order_lower for dangerous_cmd in dangerous_commands):
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("❌ ERROR 危险命令，已屏蔽。\nℹ️ INFO None.")))
                    return

                match order_lower:
                    case cmd if re.match(r"^scheduled sends.*", cmd):
                        print("使用命令定时")
                        try:
                            send_time = order_lower[order_lower.find("scheduled sends ") + len("scheduled sends "):].strip()
                            if not re.match(r'^([01][0-9]|2[0-3]):([0-5][0-9])$', send_time[:5]):
                                r = f'''命令执行结果:
❌ERROR {bot_name}不能识别给定的时间是什么 Σ( ° △ °|||)︴
ℹ️ INFO 举个🌰子：{reminder}runcommand scheduled sends 00:00 早安 —> 即可让{bot_name}在0点0分准时问候早安噢⌯oᴗo⌯'''
                                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
                            else:
                                timing_settings = f"{send_time[:5]}⊕{send_time[6::]}"
                                with open("timing_message.ini", "w", encoding="utf-8") as f:
                                    f.write(timing_settings)
                                r = f'''命令执行结果:
ℹ️ INFO {bot_name}设置成功！(*≧▽≦) '''
                                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
                        except Exception as e:
                            r = f'''命令执行结果:
❌ERROR {str(type(e))}
❌ERROR {bot_name}设置失败了…… (╥﹏╥)'''
                            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))

                    case "restart":
                        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"""命令执行结果:
⚠️ WARN 正在退出(Ctrl+C) 
ℹ️ INFO 重新启动监听器....""")))
                        try:
                            with open("restart.temp", "w", encoding="utf-7") as f:
                                f.write(str(event.group_id))
                        except Exception as e:
                            print(f"Error saving restart info: {e}")
                        Listener.restart()
                        
                    case "message clear":
                        global cmc
                        del cmc
                        cmc = ContextManager()
                        user_lists.clear()
                        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("命令执行结果:\nℹ️ INFO 清除完成")))

                    case cmd if re.match(r"^set_group_ban.*", cmd):
                        start_index = order_lower.find("set_group_ban")
                        if start_index != -1:
                            result = order[start_index + len("set_group_ban"):].strip()
                            user_and_duration = re.findall(r'\d+', result)
                            if len(user_and_duration) == 2:
                                print("At in loading...")
                                user_id = user_and_duration[0]  
                                ban_duration = user_and_duration[1]
                                await actions.set_group_ban(group_id=event.group_id, user_id=user_id, duration=ban_duration)
                                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"命令执行结果:\nℹ️ INFO 将{user_id}在{event.group_id}中禁言{ban_duration}秒\nℹ️ INFO None.")))

                    case cmd if re.match(r"^set_group_kick.*", cmd):
                        start_index = order.find("set_group_kick")
                        if start_index != -1:
                            result = order[start_index + len("set_group_kick"):].strip()
                            user_id = re.search(r'\d+', result).group()
                            await actions.set_group_kick(group_id=event.group_id, user_id=user_id)
                            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"命令执行结果:\nℹ️ INFO 将{user_id}从{event.group_id}中踢出\nℹ️ INFO None.")))

                    case cmd if re.match(r"^scheduled_sends_black add.*", cmd):
                        black_add_target = order[order.find("scheduled_sends_black add ") + len("scheduled_sends_black add "):].strip()
                        print(black_add_target)

                        def load_blacklist():
                            try:
                                with open(blacklist_file, "r", encoding="utf-8") as f:
                                    return set(line.strip() for line in f)
                            except FileNotFoundError:
                                return set() 

                        blacklist_content = load_blacklist()
                        if black_add_target not in blacklist_content:
                            blacklist_content.add(black_add_target)
                            try:
                                with open(blacklist_file, "w", encoding="utf-8") as f:
                                    for item in blacklist_content:
                                        f.write(item + "\n")  
                                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"命令执行结果:\nℹ️ INFO 黑名單添加成功, 現列表:{', '.join(blacklist_content)}")))
                            except Exception as e:
                                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"命令执行结果:\n❌ ERROR 黑名單添加失败, 原因:{e}")))
                        else:
                            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"命令执行结果:\n❌ ERROR 黑名單添加失败, 原因:群{black_add_target}已在群发黑名單！")))

                    case cmd if re.match(r"^scheduled_sends_black del.*", cmd):
                        black_del_target = order[order.find("scheduled_sends_black del ") + len("scheduled_sends_black del "):].strip()
                        blacklist_content = load_blacklist()
                        if black_del_target in blacklist_content:
                            blacklist_content.remove(black_del_target)
                            try:
                                with open(blacklist_file, "w", encoding="utf-8") as f:
                                    for item in blacklist_content:
                                        f.write(item + "\n") 
                                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"命令执行结果:\nℹ️ INFO 黑名單删除成功, 現列表:{', '.join(blacklist_content)}")))
                            except Exception as e:
                                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"命令执行结果:\n❌ ERROR 黑名單删除失败, 原因:{e}")))
                        else:
                            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"命令执行结果:\n❌ ERROR 黑名單删除失败, 原因:群{black_del_target}不在群发黑名單！")))

                    case cmd if re.match(r"^scheduled_sends_black list.*", cmd):
                        blacklist_content = load_blacklist()
                        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名单列表加载完成: {', '.join(blacklist_content)}")))

                    case _:
                        # 执行用户的命令
                        command_result = execute_command(order)
                        if command_result["returncode"] == 0:
                            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"命令执行结果:\nℹ️ INFO 执行成功\nℹ️ INFO {command_result['stdout']}.")))
                            if command_result["stderr"]:
                                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"命令执行结果:\n❌ ERROR 执行失败, 代码命令可能有误\nℹ️ INFO {command_result['stderr']}.")))
                        else:
                            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"命令执行结果:\n❌ ERROR 执行失败, 代码命令可能有误\nℹ️ INFO {command_result['stderr']}.\n❌ ERROR 返回码:{command_result['returncode']}.")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))  
                              
        elif "默认4" in order:
            EnableNetwork = "Net"
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("嗯……我好像升级了！o((>ω< ))o")))
        elif "默认3.5" in order:
            EnableNetwork = "Normal"
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("切换到大模型中运行ο(=•ω＜=)ρ⌒☆")))
        elif "读图" in order:
            EnableNetwork = "Pixmap"
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"{bot_name}打开了新视界！o(*≧▽≦)ツ")))
        elif "列出黑名单" in order:
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
            def load_blacklist():
                try:
                    with open(blacklist_file, "r", encoding="utf-8") as f:
                        blacklist115 = set(line.strip() for line in f)  # 使用集合方便快速查找,不然容易溶血
                    return blacklist115
                except FileNotFoundError:
                    return set() 
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
                Toset2 = order[order.find("添加黑名单 ") + len("添加黑名单 "):].strip()
                blacklist114 = load_blacklist() # 加载现有的黑名单,防止已修改沒更新
                if Toset2 not in blacklist114:
                    blacklist114.add(Toset2) 
                    try:
                        with open(blacklist_file, "w", encoding="utf-8") as f:
                         for item in blacklist114:
                            f.write(item + "\n")  # 防止之前的丟失555，并添加换行符
                        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名單添加成功,現列表:{blacklist114}")))
            
                    except Exception as e:
                       await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名單添加失败,原因:{e}")))
                else:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名單添加失败,原因:群{Toset2}已在黑名單！")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))
        elif "删除黑名单 " in order:
            blacklist_file = "blacklist.sr"
            def load_blacklist():
                try:
                    with open(blacklist_file, "r", encoding="utf-8") as f:
                        blacklist116 = set(line.strip() for line in f)  # 使用集合方便快速查找,不然容易溶血
                    return blacklist116
                except FileNotFoundError:
                    return set() 
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
                Toset1 = order[order.find("删除黑名单 ") + len("删除黑名单 "):].strip()
                blacklist117 = load_blacklist() # 加载现有的黑名单,防止已修改沒更新
                if Toset1 in blacklist117:
                    blacklist117.remove(Toset1) 
                    try:
                        with open(blacklist_file, "w", encoding="utf-8") as f:
                         for item in blacklist117:
                            f.write(item + "\n")  # 防止之前的丟失555，并添加换行符
                        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名單刪除成功,現列表:{blacklist117}")))
                    except Exception as e:
                       await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名單刪除失败,原因:{e}")))
                else:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名單刪除失败,原因:群{Toset1}不在黑名單！")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))
        
        elif "删除管理 " in order:
            r = ""
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User:
                Toset = order[order.find("删除管理 ") + len("删除管理 "):].strip()
                s = Super_User
                m = Manage_User
                if Toset in ROOT_User:
                    r = f'''{bot_name} {bot_name_en} - 简单 可爱 个性 全知
————————————————————
Failed: The specified user is a ROOT_User and group ROOT_User is read only.'''
                else:
                    if Toset in s:
                        s.remove(Toset)
                    if Toset in m:
                        m.remove(Toset)

                    if Write_Settings(s, m):
                        r = f'''{bot_name} {bot_name_en} - 简单 可爱 个性 全知
————————————————————
Succeeded: @{Toset} is a Common User now.
Now use {reminder}帮助 to know what permissions you have now.'''
                    else:
                        r = f'''{bot_name} {bot_name_en} - 简单 可爱 个性 全知
————————————————————
Failed: Settings files are not writeable.'''
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
                        r = f'''{bot_name} {bot_name_en} - 简单 可爱 个性 全知
————————————————————
Failed: {Toset} is not a valid user.'''
                    else:
                        nikename = nikename['nickname']
                        m = Manage_User
                        s = Super_User
                        if Toset in Manage_User:
                            r = f'''{bot_name} {bot_name_en} - 简单 可爱 个性 全知
————————————————————
Succeeded: {nikename}(@{Toset}) has become a Manage_User.'''
                        elif Toset in Super_User:
                            s.remove(Toset)
                            m.append(Toset)
                            if Write_Settings(s, m):
                                r = f'''{bot_name} {bot_name_en} - 简单 可爱 个性 全知
————————————————————
Succeeded: {nikename}(@{Toset}) has become a Manage_User.
Now use {reminder}帮助 to know what permissions you have now.'''
                            else:
                                r = f'''{bot_name} {bot_name_en} - 简单 可爱 个性 全知
————————————————————
Failed: Settings files are not writeable.'''
                        elif Toset in ROOT_User:
                            r = f'''{bot_name} {bot_name_en} - 简单 可爱 个性 全知
————————————————————
Failed: The specified user is a ROOT_User and group ROOT_User is read only.'''
                        else:
                            m.append(Toset)
                            if Write_Settings(s, m):
                                r = f'''{bot_name} {bot_name_en} - 简单 可爱 个性 全知
————————————————————
Succeeded: {nikename}(@{Toset}) has become a Manage_User.
Now use {reminder}帮助 to know what permissions you have now.'''
                            else:
                                r = f'''{bot_name} {bot_name_en} - 简单 可爱 个性 全知
————————————————————
Failed: Settings files are not writeable.'''
          
                       
                elif "管理 S " in order:
                    Toset = order[order.find("管理 S ") + len("管理 S "):].strip()
                    print(f"try to get_user {Toset}")
                    nikename = (await actions.get_stranger_info(Toset, no_cache=True)).data.raw
                    print(str(nikename))
                    if len(nikename) == 0:
                        r = f'''{bot_name} {bot_name_en} - 简单 可爱 个性 全知
————————————————————
Failed: {Toset} is not a valid user.'''
                    else:
                        nikename = nikename['nickname']
                        m = Manage_User
                        s = Super_User
                        if Toset in Manage_User:
                            m.remove(Toset)
                            s.append(Toset)
                            if Write_Settings(s, m):
                                r = f'''{bot_name} {bot_name_en} - 简单 可爱 个性 全知
————————————————————
Succeeded: {nikename}(@{Toset}) has become a Super_User.
Now use {reminder}帮助 to know what permissions you have now.'''
                            else:
                                r = f'''{bot_name} {bot_name_en} - 简单 可爱 个性 全知
————————————————————
Failed: Settings files are not writeable.'''
                        elif Toset in Super_User:
                            r = f'''{bot_name} {bot_name_en} - 简单 可爱 个性 全知
————————————————————
Succeeded: {nikename}(@{Toset}) has become a Super_User.'''
                        elif Toset in ROOT_User:
                            r = f'''{bot_name} {bot_name_en} - 简单 可爱 个性 全知
————————————————————
Failed: The specified user is a ROOT_User and group ROOT_User is read only.'''
                        else:
                            s.append(Toset)
                            if Write_Settings(s, m):
                                r = f'''{bot_name} {bot_name_en} - 简单 可爱 个性 全知
————————————————————
Succeeded: {nikename}(@{Toset}) has become a Super_User.
Now use {reminder}帮助 to know what permissions you have now.'''
                            else:
                                r = f'''{bot_name} {bot_name_en} - 简单 可爱 个性 全知
————————————————————
Failed: Settings files are not writeable.'''

                else:
                    r = f'''{bot_name} {bot_name_en} - 简单 可爱 个性 全知
————————————————————
Failed: Only Manage_User or Super_User could be set.'''

            else:
                r  = f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱"

            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
        elif "让我访问" in order:
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
                r = f'''{bot_name} {bot_name_en} - 简单 可爱 个性 全知
————————————————————
sisters: {sisters}
————————————————————
Manage_User: {Manage_User}
Super_User: {Super_User}
ROOT_User: {ROOT_User}
If you are a Super_User or ROOT_User, you can manage these users. Use {reminder}帮助 to know more.'''
            else:
                r  = f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱"
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
        elif "帮助" in order:
            if str(event.user_id) in ROOT_User or str(event.user_id) in Super_User:
                content = f'''管理我们的{bot_name}
————————————————————
你拥有管理{bot_name}的权限。若要查看普通帮助，请@{bot_name}
    1. {reminder}让我访问 —> 检索用有权限的用户
    2. {reminder}管理 M (QQ号，必填) —> 为用户添加 Manage_User 权限
    3. {reminder}管理 S (QQ号，必填) —> 为用户添加 Super_User 权限
    4. {reminder}删除管理 (QQ号，必填) —> 删除这个用户的全部权限
    5. {reminder}禁言 (@QQ+空格+时间(以秒为单位)，必填) —> 禁言用户一段时间
    6. {reminder}解禁 (@QQ，必填) —> 解除该用户禁言
    7. {reminder}踢出 (@QQ，必填) —> 将该用户踢出聊群
    8. 撤回 (引用一条消息) —> 撤回该消息
    9. {reminder}注销 —> 删除所有用户的上下文
    10. {reminder}修改 (hh:mm) (内容，必填) —> 改变定时消息时间与内容
    11. {reminder}感知 —> 查看运行状态
    12. {reminder}核验 (QQ号，必填) —> 检索QQ账号信息
    13. {reminder}重启 —> 关闭所有线程和进程，关闭{bot_name}。然后重新启动{bot_name}。
    14. {reminder}添加黑名单 +空格 + 群号 —> 将该群加入群发黑名单
    15. {reminder}删除黑名单 +空格 + 群号 —> 将该群移除群发黑名单
    16. {reminder}列出黑名单 —> 列出黑名单中的所有群
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
    7. {reminder}禁言 (@QQ+空格+时间(以秒为单位)，必填) —> 禁言用户一段时间
    8. {reminder}解禁 (@QQ，必填) —> 解除该用户禁言
    9. {reminder}踢出 (@QQ，必填) —> 将该用户踢出聊群
    10. 撤回 (引用一条消息) —> 撤回该消息
    11. {reminder}添加黑名单 +空格 + 群号 —> 将该群加入群发黑名单
    12. {reminder}删除黑名单 +空格 + 群号 —> 将该群移除群发黑名单
    13. {reminder}列出黑名单 —> 列出黑名单中的所有群
    你的每一步操作，与用户息息相关。'''
            else:
                p = " "
                n = " "
                r = " "
                match EnableNetwork:
                    case "Pixmap":
                        p = "（当前）"
                    case "Normal":
                        r = "（当前）"
                    case "Net":
                        n = "（当前）"

                content = f'''如何与{bot_name}交流( •̀ ω •́ )✧
    注：对话前必须加上 {reminder} 噢！~
    1. {reminder}(任意问题，必填) —> {bot_name}回复
    2. {reminder}名言【引用一条消息】 —> {bot_name}将消息载入史册
    3. {reminder}读图{p}—> {bot_name}可以查看您发送的图片
    4. {reminder}默认4{n}—> {bot_name}的快速回复通道✧
    5. {reminder}默认3.5{r}—> {bot_name}的快速回复通道✧
    6. {reminder}大头照 【@一个用户】—> {bot_name}给他拍张大头照
    7. {reminder}生图 Pixiv (标签，必填，用&分割) —> {bot_name}浏览P站
    8. {reminder}生图 ACG (任意类型，必填) —> {bot_name}制作精美二次元壁纸
    9. {reminder}做我姐姐吧 / {reminder}当我女朋友（默认）/ {reminder}做我mm吧 —> {bot_name}切换不同的角色互动噢！~
快来聊天吧(*≧︶≦)'''
                
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(content)))
        elif (isinstance(event.message[0], Segments.At) and int(event.message[0].qq) == event.self_id): 
            p = " "
            n = " "
            r = " "
            match EnableNetwork:
                case "Pixmap":
                    p = "（当前）"
                case "Normal":
                    r = "（当前）"
                case "Net":
                    n = "（当前）"

            content = f'''如何与{bot_name}交流( •̀ ω •́ )✧
    注：对话前必须加上 {reminder} 噢！~
    1. {reminder}(任意问题，必填) —> {bot_name}回复
    2. {reminder}名言【引用一条消息】 —> {bot_name}将消息载入史册
    3. {reminder}读图{p}—> {bot_name}可以查看您发送的图片
    4. {reminder}默认4{n}—> {bot_name}的快速回复通道✧
    5. {reminder}默认3.5{r}—> {bot_name}的快速回复通道✧
    6. {reminder}大头照 【@一个用户】—> {bot_name}给他拍张大头照
    7. {reminder}生图 Pixiv (标签，必填，用&分割) —> {bot_name}浏览P站
    8. {reminder}生图 ACG (任意类型，必填) —> {bot_name}制作精美二次元壁纸
    9. {reminder}做我姐姐吧 / {reminder}当我女朋友（默认）/ {reminder}做我mm吧 —> {bot_name}切换不同的角色互动噢！~
快来聊天吧(*≧︶≦)'''
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(content)))
            
        elif "关于" in order:
            global version_name
            about = f'''{bot_name} {bot_name_en} - 简单 可爱 个性 全知
————————————————————
Build Information
Version：{version_name}
Powered by NapCat.OneBot
Rebuilt from HypeR
————————————————————
Third-party API
1. Mirokoi API
2. Lolicon API
2. LoliAPI API
4. ChatGPT 3.5-turbo-16k
5. ChatGPT 4o-mini
6. Google gemini-2.0-flash-thinking-exp-01-21
————————————————————
Copyright
Made by SR Studio
2019~2025 All rights reserved'''
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(about)))

        elif "当我女朋友" in order:
            st = sisters
            if str(event.user_id) in st:
                st.remove(str(event.user_id))

            st = [item for item in st if item]

            sts = ""
            for item in range(len(st)):
                sts += st[item]
                if item != len(st) - 1:
                    sts += "\n"
            jh = jhq
            if str(event.user_id) in jh:
             jh.remove(str(event.user_id))

            jh = [item for item in jh if item]

            jhs = ""
            for item in range(len(jh)):
                jhs += jh[item]
                if item != len(jh) - 1:
                    jhs += "\n"
            try:
                with open("sisters.ini", "w") as f:
                    f.write(sts)
                    f.close()

                sisters = st
                with open("jhq.ini", "w") as f:
                    f.write(jhs)
                    f.close()

                jhq = jh
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("老公~你回来啦~(*≧︶≦)")))
            except Exception as e:
                print(traceback.format_exc)
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"可是{bot_name}还想继续做你的姐姐，这样我就可以保护你了！(๑•̀ㅂ•́)و✧")))
        
        elif "做我姐姐吧" in order:
            st = sisters
            if str(event.user_id) not in st:
                st.append(str(event.user_id))

            st = [item for item in st if item]

            sts = ""
            for item in range(len(st)):
                sts += st[item]
                if item != len(st) - 1:
                    sts += "\n"
            jh = jhq
            if str(event.user_id) in jh:
             jh.remove(str(event.user_id))

            jh = [item for item in jh if item]

            jhs = ""
            for item in range(len(jh)):
                jhs += jh[item]
                if item != len(jh) - 1:
                    jhs += "\n"
            try:
                with open("sisters.ini", "w") as f:
                    f.write(sts)
                    f.close()

                sisters = st
                with open("jhq.ini", "w") as f:
                    f.write(jhs)
                    f.close()

                jhq = jh
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("你好呀！妹妹！~o(*≧▽≦)ツ")))
            except Exception as e:
                print(traceback.format_exc)
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"呜呜呜……{bot_name}还想继续做你的女朋友，依赖你 (*/ω＼*)")))
                
        elif "做我mm吧" in order:
            st = sisters
            if str(event.user_id) in st:
                st.remove(str(event.user_id))

            st = [item for item in st if item]

            sts = ""
            for item in range(len(st)):
                sts += st[item]
                if item != len(st) - 1:
                    sts += "\n"
            jh = jhq
            if str(event.user_id) not in jh:
             jh.append(str(event.user_id))

            jh = [item for item in jh if item]

            jhs = ""
            for item in range(len(jh)):
                jhs += jh[item]
                if item != len(jh) - 1:
                    jh += "\n"
           
            try:
                with open("sisters.ini", "w") as f:
                    f.write(sts)
                    f.close()

                sisters = st
           
                with open("jhq.ini", "w") as f:
                    f.write(jhs)
                    f.close()

                jhq = jh
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("你好呀！血小板！~o(*≧▽≦)ツ")))
            except Exception as e:
                print(traceback.format_exc)
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"呜呜呜……{bot_name}还想继续做你的女朋友，依赖你 (*/ω＼*)")))

        elif "核验 " in order:
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
                uid = order[order.find("核验 ") + len("核验 "):].strip()
                print(f"try to get_user {uid}")
                nikename = (await actions.get_stranger_info(uid)).data.raw
                print(f"get {nikename} successfully")
                if len(nikename) == 0:
                    r = f'''{bot_name} {bot_name_en} - 简单 可爱 个性 全知
————————————————————
Failed: {uid} is not a valid user.'''
                else:
                    items = [f"{key}: {value}" for key, value in nikename.items()]
                    result = "\n".join(items)
                    r = f'''{bot_name} {bot_name_en} - 简单 可爱 个性 全知
————————————————————
{result}'''
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))

        elif f"{reminder}感知" in str(event.message):
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
                system_info = get_system_info()
                feel = f'''{bot_name} {bot_name_en} - 简单 可爱 个性 全知
————————————————————
System Now
Running {seconds_to_hms(round(time.time() - second_start, 2))}
Syetem Version：{system_info["version_info"]}
Architecture：{system_info["architecture"]}
CPU Usage：{str(system_info["cpu_usage"]) + "%"}
Memory Usage：{str(system_info["memory_usage_percentage"]) + "%"}'''
                for i, usage in enumerate(system_info["gpu_usage"]):
                    feel = feel + f"\nGPU {i} Usage：{usage * 100:.2f}%"
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(feel)))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))
            
        elif f"{reminder}注销" in str(event.message):
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
             #   global cmc
                del cmc
                cmc = ContextManager()
                user_lists.clear()
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"卸下包袱，{bot_name}更轻松了~ (/≧▽≦)/")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))
                
            
        elif f"{reminder}名言" in str(event.message):
            print("获取名言")
            imageurl = None

            if isinstance(event.message[0], Segments.Reply):
                print("有消息反馈")
                msg_id = event.message[0].id
                content = await actions.get_msg(msg_id)
                message = content.data["message"]
                message = gen_message({"message": message})
                print("有引用消息")
                for i in message:
                    print(type(i))
                    print(str(i))
                    if isinstance(i, Segments.Image):
                        print("应该有图")
                        if i.file.startswith("http"):
                            imageurl = i.file
                        else:
                            imageurl = i.url

                quoteimage = await Quote.handle(event.message, actions, imageurl)
                print("制作名言")
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), quoteimage))
                os.remove("./temps/quote.png")
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text("在记录一条名言之前先引用一条消息噢 ☆ヾ(≧▽≦*)o")))
                
        elif f"{reminder}生成" in str(event.message):
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
            
        elif f"{reminder}生草" in str(event.message):
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("🌿")))


        elif "生图 ACG " in order or "zzzz...涩图...嘿嘿..." in user_message:

            if "生图 ACG " not in order and "zzzz...涩图...嘿嘿..." in user_message:
              order = "生图 ACG 随机"
            start_index = order.find("生图 ACG ") 
            if start_index != -1:
                   result = order[start_index + len("生图 ACG "):].strip()
                   api = ""
                   user_id = event.user_id
                   current_time = time.time()
                   if user_id in cooldowns and current_time - cooldowns[user_id] < 18:
                        if not (str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User):
                            time_remaining = 18 - (current_time - cooldowns[user_id])
                            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"18秒个人cd，请等待 {time_remaining:.1f} 秒后重试")))
                            return
                   else:
                        selfID = await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"{bot_name}正在制作超级好看的二次元壁纸 ヾ(≧▽≦*)o")))

                        if "随机" in result:
                            api = "https://api.iw233.cn/api.php?sort=random"
                            print("0")
                        elif "精选" in result:
                            api = "https://api.iw233.cn/api.php?sort=top"
                            print("1")
                        elif "白毛" in result:
                            api = "https://api.iw233.cn/api.php?sort=yin"
                            print("2")
                        elif "星空" in result:
                            api = "https://api.iw233.cn/api.php?sort=xing"
                            print("3")
                        elif "兽娘" in result:
                            api = "https://api.iw233.cn/api.php?sort=cat"
                            print("4")
                        elif "电脑壁纸" in result:
                            api = "https://api.iw233.cn/api.php?sort=pc"
                            print("5")
                        elif "手机壁纸" in result:
                            api = "https://api.iw233.cn/api.php?sort=mp"
                            print("6")
                        elif "头像" in result:
                            api = "https://www.loliapi.com/acg/pp/"
                            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(api), Segments.Text(f"{result}生成 结束！✧*。٩(>ω<*)و✧*。")))
                            await actions.del_message(selfID.data.message_id)
                            cooldowns[user_id] = current_time
                            print("7")
                            return

                        if api == "":
                            h = f'''{bot_name}可生成精美 ACG 壁纸噢~ヾ(≧∪≦*)ノ〃
1. 按内容生成，发送
{reminder}生图 ACG 随机/精选/白毛/星空/兽娘/头像
2. 按尺寸生成，发送
{reminder}生图 ACG 电脑壁纸/手机壁纸
举个🍐子：{reminder}生图 ACG 白毛 -> {bot_name}生成白毛二次元壁纸
快来试试吧Ｏ(≧▽≦)Ｏ '''
                            await actions.del_message(selfID.data.message_id)
                            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(h)))
                        else:
                                parameters = {
                                        "type": "json",
                                        'num': "1",
                                        }

                                response = requests.get(api, params=parameters)
                                print(parameters)
                                outputurl = response.json()
                                output = outputurl["pic"][0]
                                print(output)

                                image_id = await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(output), Segments.Text(f"{result}生成 结束！✧*。٩(>ω<*)و✧*。")))
                                await actions.del_message(selfID.data.message_id)
                                cooldowns[user_id] = current_time

 
        elif "生图 Pixiv " in order:
            start_index = order.find("生图 Pixiv ")
            selfID = await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"{bot_name}正在从 Pixiv 生成 ヾ(≧▽≦*)o")))
            # await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image("https://pixiv.t.sr-studio.top/img-original/img/2023/01/24/03/53/38/104766095_p0.png")))
            
            if start_index != -1:
                
                if not generating:
                    user_id = event.user_id
                    current_time = time.time()

                    if user_id in cooldowns1 and current_time - cooldowns1[user_id] < 18:
                        time_remaining1 = 18 - (current_time - cooldowns1[user_id])
                        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"18秒个人cd，请等待 {time_remaining1:.1f} 秒后重试")))
                        return
                    else:

                     generating = True
                     result = order[start_index + len("生图 Pixiv "):].strip()
                    # await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"取参数 {result}")))
                     url_setted = "https://api.lolicon.app/setu/v2?num=1&r18=0&excludeAI=false&proxy=pixiv.t.sr-studio.top"

                     tags = result.split("&")
                     for TagIndex in range(len(tags)):
                        url_setted = url_setted + "&tag=" + tags[TagIndex]

                     print(url_setted)

                     try:
                         async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False), timeout=aiohttp.ClientTimeout(10)) as session:
                             async with session.get(url=url_setted) as response:  # 设置超时时间为7秒
                                 request = await response.json()
                     except Exception as e:
                         request = "Failed\n" + traceback.format_exc()

                     print("请求成功")

                     if "Failed" in request:
                         print(request)
                         emessage = f'''{bot_name}无法访问接口了，请稍后重试 ε(┬┬﹏┬┬)3'''
                         await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(emessage)))
                         
                     else:
                         data_normal = request['data']
                         if len(data_normal) < 1:
                             emessage = f'''你给{bot_name}的标签太严格啦！（生气），换几个标签试试吧 ＞﹏＜'''
                             await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(emessage)))
                         else:
                             data = data_normal[0]
                             info = f'''标题：{data['title']}
Pixiv ID：{data['pid']}
作者：{data['author']}
作者ID：{data['uid']}
AI参与：{'是' if data['aiType'] == 1 else '否'}
创作时间：{datetime.datetime.fromtimestamp(data['uploadDate'] / 1000).strftime('%Y-%m-%d')}
标签：{data['tags']}
源图：{data['urls']['original'].replace("pixiv.t.sr-studio.top", "i.pximg.net")}'''
                             url = str(data['urls']['original'])
                             print(url)
                             CanSend = True

                            # try:
                            #     print("saving")
                            #     async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False), timeout=aiohttp.ClientTimeout(7)) as session:
                            #         async with session.get(url) as response:  # 设置超时时间为7秒
                            #             content = response.content
                            #             image = await content.read()
                                
                            #     new_image: bytes = deal_image(image)

                            #     print("ToLocal")
                            #     with open(".\\PixivGenerated.png", 'wb') as f:
                            #         f.write(new_image)
                            #     # dlr = Logic.Downloader(url, ".\\PixivGenerated.png")
                            #     # await dlr.download()

                            #     CanSend = verfiy_pixiv(".\\PixivGenerated.png")
                            # except Exception as e:
                            #     print(traceback.format_exc())
                            #     CanSend = False

                            # try:
                            #     async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False), timeout=aiohttp.ClientTimeout(7)) as session:
                            #         async with session.get(url) as response:
                            #             raw_body = await response.read()  # 读取原始字节
                            #             result = chardet.detect(raw_body)  # 检测编码
                            #             encoding = result['encoding']
                            #             url_text = raw_body.decode(encoding)

                            #     if "404" in url_text:
                            #         await actions.del_message(selfID.data.message_id)
                            #         await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"{bot_name}获取图片失败了，请再试一次 {{{(>_<)}}}")))
                            #         CanSend = False
                            # except Exception as e:
                            #     CanSend = True

                             if CanSend:
                                 if "R-18" not in data['tags'] and "R-18G" not in data['tags'] and "即将脱落的胸罩" not in data['tags']:
                                    #image_id = await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(url)))
                                     await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(url), Segments.Text(info))) #Segments.Reply(image_id.data.message_id)
                                     await actions.del_message(selfID.data.message_id)
                                     cooldowns1[user_id] = current_time
                                    # get_returned = await actions.get_msg(image_id.data.message_id)
                                    # print(get_returned.data)
                                 else:
                                     await actions.del_message(selfID.data.message_id)
                                     await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"你要的图片实在太涩啦！{bot_name}都不敢看了 (⓿_⓿)")))
                             else:
                                 await actions.del_message(selfID.data.message_id)
                                 await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"{bot_name}生图失败了，再试一次吧（哭）(○´･д･)ﾉ")))
                            
                     generating = False

                else:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("前面还有一张图在生成呢，请稍候再试吧 (*/ω＼*)")))                       

            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"没有参数。")))

        elif "enc解密" in order:
          try:
            start_index = order.find("enc解密")
            if start_index != -1:
                encoded_part = order[start_index + len("enc解密"):].strip()

                if not encoded_part:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("您没有发送密文")))
                    return

         
                base64_decoded = base64.b64decode(encoded_part).decode('utf-8')

             
                url_decoded = urllib.parse.unquote(base64_decoded)

                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"解密结果: {str(url_decoded)}")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("没有参数。")))
          except Exception as e:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"解密失败: {str(e)}")))

        elif "大头照" in order:
            uin = ""

            for i in event.message:
                    print(type(i))
                    print(str(i))
                    if isinstance(i, Segments.At):
                        print("At in loading...")
                        uin = i.qq

            if uin == "":
                uin = event.user_id
                
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(f"http://q2.qlogo.cn/headimg_dl?dst_uin={uin}&spec=640")))
        
        elif "禁言" in order:
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
                try:
                    start_index = order.find("禁言")
                    if start_index != -1:
                        result = order[start_index + len("禁言"):].strip()
                        numbers = re.findall(r'\d+', result)
                        complete = False
                        for i in event.message:
                            if isinstance(i, Segments.At):
                                print("At in loading...")
                                userid114 = numbers[0]  
                                time114 = numbers[1]
                                await actions.set_group_ban(group_id=event.group_id, user_id=userid114, duration=time114)
                                complete = True
                                break 
                        
                        if not complete:
                            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"管理员：你的格式有误。\n格式：{reminder}禁言 @anyone (seconds of duration)\n参考：{reminder}禁言 @Harcic#8042 128")))
                        else:
                            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"管理员：已禁言，时长 {time114} 秒。")))
                
                except Exception as e:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"管理员：你的格式有误。\n格式：{reminder}禁言 @anyone (seconds of duration)\n参考：{reminder}禁言 @Harcic#8042 128")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))
                    
        elif "解禁" in order:
           if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
            start_index = order.find("解禁")
            if start_index != -1:
             result = order[start_index + len("解禁"):].strip()
             numbers = re.findall(r'\d+', result)
             for i in event.message:
                   if isinstance(i, Segments.At):
                        print("At in loading...")
                        userid114 = numbers[0]  
                        time114 = 0
                        await actions.set_group_ban(group_id=event.group_id,user_id=userid114,duration=time114)
     
           else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")))
          
        elif "踢出" in order:
          if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
                for i in event.message:
                    print(type(i))
                    print(str(i))
                    if isinstance(i, Segments.At):
                        print("At in loading...")
                        await actions.set_group_kick(group_id=event.group_id,user_id=i.qq)
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

        else:
            if len(order) >= 2:
                url = ""
                try:
                    match EnableNetwork:
                        case "Pixmap":
                            # search_tool = FunctionDeclaration(
                            #     name="google_search_retrieval",
                            #     description="利用 Google 搜索来检索信息",
                            #     parameters={
                            #         "type": "object",
                            #         "properties": {
                            #             "query": {
                            #                 "type": "string",
                            #                 "description": str(user_message),
                            #             }
                            #         },
                            #     },
                            # )

                            model = genai.GenerativeModel(
                                model_name="gemini-2.0-flash-thinking-exp-01-21", #gemini-2.0-flash-exp
                                generation_config=generation_config,
                                system_instruction=sys_prompt or None,
                                # tools=[search_tool]
                                #tools="code_execution
                            )

                            new = []
                            
                            if isinstance(event.message[0], Segments.Reply):
                                print("有消息反馈")
                                msg_id = event.message[0].id
                                content = await actions.get_msg(msg_id)
                                message = content.data["message"]
                                message = gen_message({"message": message})
                                print("有引用消息")
                                for i in message:
                                    if isinstance(i, Segments.Text):
                                        new.append(Parts.Text(i.text.replace(reminder, "", 1)))
                                    elif isinstance(i, Segments.Image):
                                        if i.file.startswith("http"):
                                            url = i.file
                                        else:
                                            url = i.url
                                        new.append(Parts.File.upload_from_url(url))
                                        print("有图")

                            for i in event.message:
                                if isinstance(i, Segments.Text):
                                    new.append(Parts.Text(i.text.replace(reminder, "", 1)))
                                elif isinstance(i, Segments.Image):
                                    if i.file.startswith("http"):
                                        url = i.file
                                    else:
                                        url = i.url
                                    new.append(Parts.File.upload_from_url(url))
                                    print("有图")
            
                            new = Roles.User(*new)
                            result = cmc.get_context(event.user_id, event.group_id).gen_content(new).rstrip("\n")

                           
                        case "Normal":
                            search = SearchOnline(sys_prompt, order, user_lists, event.user_id, "gpt-3.5-turbo-16k", bot_name, Configurator.cm.get_cfg().others["openai_key"])
                            ulist, result = search.Response()
                            user_lists = ulist

                        case "Net":
                            search = SearchOnline(sys_prompt, order, user_lists, event.user_id, "gpt-4o-mini", bot_name, Configurator.cm.get_cfg().others["openai_key"])
                            ulist, result = search.Response()
                            user_lists = ulist

                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id),Segments.Text(result)))
                    
                except UnboundLocalError:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id),Segments.Text(f"请稍等，{bot_name}在思考 🤔")))
                except TimeoutError:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id),Segments.Text(f"哎呀，你问的问题太复杂了，{bot_name}想不出来了 ┭┮﹏┭┮")))
                except Exception as e:
                    print(traceback.format_exc())
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id),Segments.Text(f"{type(e)}\n{url}\n{bot_name}发生错误，不能回复你的消息了，请稍候再试吧 ε(┬┬﹏┬┬)3")))

                
def seconds_to_hms(total_seconds):
    hours = total_seconds // 3600
    remaining_seconds = total_seconds % 3600
    minutes = remaining_seconds // 60
    seconds = remaining_seconds % 60
    return f"{hours}h, {minutes}m, {seconds}s"

def verfiy_pixiv(file_path):
    try:
        img = Image.open(file_path)
        img.verify()  # 验证图像
        img.close()
        return True
    except (IOError, SyntaxError) as e:
        print(f"Error: {e}")
        return False

def get_system_info():
    # 系统
    version_info = platform.platform()
    architecture = platform.architecture()
    cpu_count = psutil.cpu_count(logical=True)
    cpu_usage = psutil.cpu_percent(interval=1)

    # 内存
    virtual_memory = psutil.virtual_memory()
    total_memory = virtual_memory.total
    used_memory = virtual_memory.used
    memory_usage_percentage = virtual_memory.percent

    # GPU信息（是否有）
    gpus = GPUtil.getGPUs()
    if gpus:
        gpu_count = len(gpus)
        gpu_usage = [gpu.load for gpu in gpus]
    else:
        gpu_count = 0
        gpu_usage = []

    return {
        "version_info": version_info,
        "architecture": architecture,
        "cpu_count": cpu_count,
        "cpu_usage": cpu_usage,
        "total_memory": total_memory,
        "used_memory": used_memory,
        "memory_usage_percentage": memory_usage_percentage,
        "gpu_count": gpu_count,
        "gpu_usage": gpu_usage,
    }


def deal_image(i):
    img = Image.open(io.BytesIO(i))

    # 压缩图像
    buffer = io.BytesIO()
    quality = 100  # 从100开始，逐渐降低质量直到小于10MB
    max_size = 10 * 1024 * 1024  # 10MB

    # 循环压缩图像，直到达到指定大小
    while True:
        buffer.seek(0)
        img.save(buffer, format='JPEG', quality=quality)
        if buffer.tell() < max_size or quality <= 10:  # 停止条件
            break
        quality -= 5  # 每次减少质量
        
    # 最终的压缩图像存储在buffer中
    return buffer.getvalue()

Listener.run()