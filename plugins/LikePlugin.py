import asyncio
import json
import random
from datetime import datetime

from jianer import common as Manager, segments as Segments
from jianer.plugins import PluginMetadata

from bot import plugin_state


__plugin_meta__ = PluginMetadata(
    name="jianerbot-plugin-like",
    description="Give the sender QQ profile likes.",
    usage="赞我 / 超我 —> 给你的QQ名片点赞10次\n{reminder}点赞信息 / {reminder}超信息 —> 查看今日点赞次数",
    requires={"jianerbot-plugin-alconna"},
)

DAILY_LIMIT = 10
LIKE_API_ATTEMPTS = 55


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

        return self.user_data[user_id].get("count", 0) < DAILY_LIMIT

    def get_remaining_likes(self, user_id):
        user_id = str(user_id)
        today = datetime.now().strftime("%Y-%m-%d")

        if user_id not in self.user_data or self.user_data[user_id].get("last_date") != today:
            return DAILY_LIMIT

        return DAILY_LIMIT - self.user_data[user_id].get("count", 0)

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
        return f"你今天已被点赞 {count} 次！\n剩余可点赞次数: {DAILY_LIMIT - count}次"


like_manager = LikeManager()


async def dispatch(event, actions):
    if plugin_state.current_stage() != "always":
        return False
    if not hasattr(event, "message") or not hasattr(event, "user_id"):
        return False

    msg = str(event.message).strip()
    runtime = plugin_state.get_runtime()
    reminder = runtime.get("reminder", "")
    bot_name = runtime.get("bot_name", "")

    if msg == "赞我":
        return await _send_like(event, actions, bot_name, action="赞")

    if msg in {"超我", "超湿我"}:
        return await _send_like(event, actions, bot_name, action="超")

    if msg == f"{reminder}点赞信息":
        await _send_text(actions, event, like_manager.get_like_info(event.user_id))
        return True

    if msg == f"{reminder}超信息":
        info = like_manager.get_like_info(event.user_id)
        await _send_text(actions, event, info.replace("点赞", "超").replace("赞", "超"))
        return True

    return False


async def _send_like(event, actions, bot_name: str, *, action: str) -> bool:
    user_id = event.user_id
    if not like_manager.can_like_today(user_id):
        verb = "点赞" if action == "赞" else "超"
        await _send_text(actions, event, f"今天已经{verb}过10次啦，明天再来吧~ (｡•́︿•̀｡)")
        return True

    try:
        for _ in range(LIKE_API_ATTEMPTS):
            await actions.custom.send_like(user_id=user_id, times=1)
            await asyncio.sleep(random.uniform(0.1, 0.5))

        like_manager.record_like(user_id, DAILY_LIMIT)
        remaining = like_manager.get_remaining_likes(user_id)
        if action == "赞":
            success_msg = f"成功给你的名片点赞10次啦！{bot_name}最喜欢你啦！记得回赞哦！(◍•ᴗ•◍)❤"
            success_msg += f"\n今日还可点赞{remaining}次" if remaining > 0 else "\n今日点赞已达上限啦~"
            group_msg = f"你的名片已获得{bot_name}的10次点赞！(≧▽≦)/"
        else:
            success_msg = "已经为你超了10下哦，记得回捏~ (◍•ᴗ•◍)❤"
            success_msg += f"\n今日还可超{remaining}次" if remaining > 0 else "\n今日超已达上限啦~"
            group_msg = f"你的名片已被{bot_name}超了10下！(≧▽≦)/"

        await _send_text(actions, event, success_msg)
        if getattr(event, "group_id", None) is not None:
            await actions.send(
                group_id=event.group_id,
                message=Manager.Message(Segments.At(user_id), Segments.Text(group_msg)),
            )
    except Exception as exc:
        print(f"{action}操作失败: {exc}")
        await _send_text(actions, event, f"{action}操作失败啦...可能是机器人没有权限(｡•́︿•̀｡) 错误: {exc}")

    return True


async def _send_text(actions, event, text: str):
    target = {"message": Manager.Message(Segments.Text(text))}
    if getattr(event, "group_id", None) is not None:
        target["group_id"] = event.group_id
    else:
        target["user_id"] = event.user_id
    return await actions.send(**target)
