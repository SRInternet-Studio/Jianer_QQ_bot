from Hyper import Configurator
import asyncio
import time
import json
import os
import random
from datetime import datetime
from Hyper import Manager, Segments

Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())

TRIGGHT_KEYWORD = "Any"  
HELP_MESSAGE = f"超我/赞我 —> 给你的QQ名片点赞10次"

class LikeManager:
    def __init__(self):
        self.data_file = "like_data.json"
        self.user_data = {}
        self.load_data()
    
    def load_data(self):
        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                self.user_data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.user_data = {}
    
    def save_data(self):
        with open(self.data_file, "w", encoding="utf-8") as f:
            json.dump(self.user_data, f, ensure_ascii=False, indent=2)
    
    def can_like_today(self, user_id):
        user_id = str(user_id)
        today = datetime.now().strftime("%Y-%m-%d")
        
        if user_id not in self.user_data:
            self.user_data[user_id] = {"last_date": today, "count": 0}
            return True
        
        if self.user_data[user_id].get("last_date") != today:
            self.user_data[user_id] = {"last_date": today, "count": 0}
            return True
        
        return self.user_data[user_id].get("count", 0) < 10
    
    def get_remaining_likes(self, user_id):
        user_id = str(user_id)
        today = datetime.now().strftime("%Y-%m-%d")
        
        if user_id not in self.user_data or self.user_data[user_id].get("last_date") != today:
            return 10
        
        return 10 - self.user_data[user_id].get("count", 0)
    
    def record_like(self, user_id, times=1):
        user_id = str(user_id)
        today = datetime.now().strftime("%Y-%m-%d")
        
        if user_id not in self.user_data or self.user_data[user_id].get("last_date") != today:
            self.user_data[user_id] = {"last_date": today, "count": times}
        else:
            self.user_data[user_id]["count"] = self.user_data[user_id].get("count", 0) + times
        
        self.save_data()
    
    def get_like_info(self, user_id):
        user_id = str(user_id)
        today = datetime.now().strftime("%Y-%m-%d")
        
        if user_id not in self.user_data or self.user_data[user_id].get("last_date") != today:
            return "你今天还没有被点过赞哦！今日还可点赞10次~"
        
        count = self.user_data[user_id].get("count", 0)
        return f"你今天已被点赞 {count} 次！\n剩余可点赞次数: {10 - count}次"

like_manager = LikeManager()

async def on_message(event, actions, Manager, Segments):
    if not hasattr(event, "message") or not hasattr(event, "user_id"):
        return False
    
    msg = str(event.message).strip()
    reminder = Configurator.cm.get_cfg().others["reminder"]
    bot_name = Configurator.cm.get_cfg().others["bot_name"]
    
    if msg == "赞我":
        user_id = event.user_id
        
        if not like_manager.can_like_today(user_id):
            await actions.send(
                group_id=event.group_id if hasattr(event, "group_id") else None,
                user_id=user_id if not hasattr(event, "group_id") else None,
                message=Manager.Message(Segments.Text("今天已经给你点过10次赞啦，明天再来吧~ (๑•́ ₃ •̀๑)"))
            )
            return True
        
        try:
            for i in range(55):
                await actions.custom.send_like(user_id=user_id, times=1)
                delay = random.uniform(0.1, 0.5)
                await asyncio.sleep(delay)
            
            like_manager.record_like(user_id, 10)
            
            remaining = like_manager.get_remaining_likes(user_id)
            success_msg = f"成功给你的名片点赞10次啦！{bot_name}最喜欢你啦！记得回赞哦！(◍•ᴗ•◍)❤"
            if remaining > 0:
                success_msg += f"\n今日还可点赞{remaining}次"
            else:
                success_msg += "\n今日点赞已达上限啦~"
            
            await actions.send(
                group_id=event.group_id if hasattr(event, "group_id") else None,
                user_id=user_id if not hasattr(event, "group_id") else None,
                message=Manager.Message(Segments.Text(success_msg))
            )
            
            if hasattr(event, "group_id"):
                await actions.send(
                    group_id=event.group_id,
                    message=Manager.Message(
                        Segments.At(user_id),
                        Segments.Text(f"你的名片已获得{bot_name}的10次点赞！(≧▽≦)/")
                    )
                )
        except Exception as e:
            print(f"点赞失败: {e}")
            await actions.send(
                group_id=event.group_id if hasattr(event, "group_id") else None,
                user_id=user_id if not hasattr(event, "group_id") else None,
                message=Manager.Message(Segments.Text(f"点赞失败啦...可能是机器人没有权限(｡•́︿•̀｡) 错误: {str(e)}"))
            )
        
        return True
    
    elif msg in ["超我", "超湿我"]:
        user_id = event.user_id
        
        if not like_manager.can_like_today(user_id):
            await actions.send(
                group_id=event.group_id if hasattr(event, "group_id") else None,
                user_id=user_id if not hasattr(event, "group_id") else None,
                message=Manager.Message(Segments.Text("今天已经超你10次啦，明天再来吧~ (๑•́ ₃ •̀๑)"))
            )
            return True
        
        try:
            for i in range(55):
                await actions.custom.send_like(user_id=user_id, times=1)
                delay = random.uniform(0.1, 0.5)
                await asyncio.sleep(delay)
            
            like_manager.record_like(user_id, 10)
            
            remaining = like_manager.get_remaining_likes(user_id)
            success_msg = "已经为你超了10下哦，记得回捏~ (◍•ᴗ•◍)❤"
            if remaining > 0:
                success_msg += f"\n今日还可超{remaining}次"
            else:
                success_msg += "\n今日超已达上限啦~"
            
            await actions.send(
                group_id=event.group_id if hasattr(event, "group_id") else None,
                user_id=user_id if not hasattr(event, "group_id") else None,
                message=Manager.Message(Segments.Text(success_msg))
            )
            
            if hasattr(event, "group_id"):
                await actions.send(
                    group_id=event.group_id,
                    message=Manager.Message(
                        Segments.At(user_id),
                        Segments.Text(f"你的名片已被{bot_name}超了10下！(≧▽≦)/")
                    )
                )
        except Exception as e:
            print(f"超操作失败: {e}")
            await actions.send(
                group_id=event.group_id if hasattr(event, "group_id") else None,
                user_id=user_id if not hasattr(event, "group_id") else None,
                message=Manager.Message(Segments.Text(f"超操作失败啦...可能是机器人没有权限(｡•́︿•̀｡) 错误: {str(e)}"))
            )
        return True
    
    elif msg == f"{reminder}点赞信息":
        user_id = event.user_id
        info = like_manager.get_like_info(user_id)
        
        await actions.send(
            group_id=event.group_id if hasattr(event, "group_id") else None,
            user_id=user_id if not hasattr(event, "group_id") else None,
            message=Manager.Message(Segments.Text(info))
        )
        return True
    
    elif msg == f"{reminder}超信息":
        user_id = event.user_id
        info = like_manager.get_like_info(user_id)
        info = info.replace("点赞", "超").replace("赞", "超")
        
        await actions.send(
            group_id=event.group_id if hasattr(event, "group_id") else None,
            user_id=user_id if not hasattr(event, "group_id") else None,
            message=Manager.Message(Segments.Text(info))
        )
        return True
    
    return False

print("[QQ名片点赞] 加载成功")
# print("触发词: 赞我, 超我, 超湿我")
# print("功能: 每次给用户QQ名片点赞10次")