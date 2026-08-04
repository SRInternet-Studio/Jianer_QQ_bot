import json
from datetime import datetime

from jianer.plugins import PluginMetadata
from jianer.plugins.builtin.alconna import Command, Target, UniMessage

from bot import plugin_state


__plugin_meta__ = PluginMetadata(
    name="jianerbot-plugin-like",
    description="Give the sender QQ profile likes.",
    usage="赞我 / 超我 —> 给你的QQ名片点赞10次\n{reminder}点赞信息 / {reminder}超信息 —> 查看今日点赞次数",
    requires={"jianerbot-plugin-alconna"},
)

DAILY_LIMIT = 10


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


_REMINDER = str(plugin_state.get_runtime().get("reminder", ""))
EARLY_COMMANDS = frozenset(
    {
        "赞我",
        "超我",
        "超湿我",
        f"{_REMINDER}点赞信息",
        f"{_REMINDER}超信息",
    }
)


@Command("赞我").handle()
async def _handle_like(event, actions):
    return await _send_like(
        event,
        actions,
        str(plugin_state.get_runtime().get("bot_name", "")),
        action="赞",
    )


@Command("超我").handle()
@Command("超湿我").handle()
async def _handle_super_like(event, actions):
    return await _send_like(
        event,
        actions,
        str(plugin_state.get_runtime().get("bot_name", "")),
        action="超",
    )


@Command(f"{_REMINDER}点赞信息").handle()
async def _handle_like_info(event, actions):
    await _send_text(actions, event, like_manager.get_like_info(event.user_id))
    return True


@Command(f"{_REMINDER}超信息").handle()
async def _handle_super_like_info(event, actions):
    info = like_manager.get_like_info(event.user_id)
    await _send_text(
        actions,
        event,
        info.replace("点赞", "超").replace("赞", "超"),
    )
    return True


async def _send_like(event, actions, bot_name: str, *, action: str) -> bool:
    user_id = event.user_id
    protocol = str(
        getattr(actions, "protocol", None)
        or getattr(event, "protocol", None)
        or ""
    ).strip().lower()
    if protocol != "onebot":
        await _send_text(
            actions,
            event,
            f"当前{protocol or '未知'}协议不支持QQ名片点赞，已停止操作。",
        )
        return True

    if not like_manager.can_like_today(user_id):
        verb = "点赞" if action == "赞" else "超"
        await _send_text(actions, event, f"今天已经{verb}过10次啦，明天再来吧~ (｡•́︿•̀｡)")
        return True

    try:
        await actions.custom.send_like(user_id=user_id, times=DAILY_LIMIT)

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
            await UniMessage.send(
                UniMessage.at(user_id),
                UniMessage.text(group_msg),
                target=Target.group(event.group_id),
                actions=actions,
            )
    except Exception as exc:
        print(f"{action}操作失败: {exc}")
        await _send_text(actions, event, f"{action}操作失败啦...可能是机器人没有权限(｡•́︿•̀｡) 错误: {exc}")

    return True


async def _send_text(actions, event, text: str):
    return await UniMessage.send(
        UniMessage.text(text),
        target=Target.from_event(event),
        actions=actions,
    )
