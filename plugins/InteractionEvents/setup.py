"""Native interaction and lifecycle events for JianerCore."""

from __future__ import annotations

import datetime
import os
import random

from jianer import events
from jianer.plugins import PluginMetadata
from jianer.plugins.builtin.alconna import Command, UniMessage

from bot import plugin_state
from bot import plugin_support as support


__plugin_meta__ = PluginMetadata(
    name="jianerbot-plugin-interaction-events",
    description="原生互动、戳一戳、入群/退群与重启通知。",
    usage=(
        "ping —> 群聊回复 pong\n"
        "{reminder}表情复述 —> 切换单 emoji 复述\n"
        "{reminder}生草 —> 发送 🌿\n"
        "{reminder}生成 —> 发送本地占位图\n"
        "戳一戳机器人 —> 回戳并回复一句\n"
        "入群/退群 —> 欢迎或告别\n"
        "入群邀请 —> 按 Auto_approval 关键词审批"
    ),
    requires={"jianerbot-plugin-alconna"},
)

_REMINDER = str(plugin_state.get_runtime().get("reminder", "~"))
_BOT_NAME = str(plugin_state.get_runtime().get("bot_name", "简儿"))
_BOT_NAME_EN = str(plugin_state.get_runtime().get("bot_name_en", "Jianer"))
_SLOGAN = str(plugin_state.get_runtime().get("one_slogan", ""))


def _runtime() -> dict:
    return plugin_state.get_runtime()


def _config():
    return plugin_state.get_config()


def _others() -> dict:
    return dict(getattr(_config(), "others", {}) or {})


def _ensure_runtime_defaults() -> None:
    runtime = _runtime()
    runtime.setdefault("emoji_repeat_enabled", True)
    runtime.setdefault("emoji_send_count", None)
    runtime.setdefault("wait_for_add_in", False)


@Command("ping").handle()
async def _ping(event, actions):
    if getattr(event, "group_id", None) is None:
        return False
    await support.send_text(event, actions, "pong! 爆炸！v(◦'ωˉ◦)~♡ ")
    return True


@Command(f"{_REMINDER}表情复述").handle()
async def _toggle_emoji_repeat(event, actions):
    if getattr(event, "group_id", None) is None:
        return False
    _ensure_runtime_defaults()
    runtime = _runtime()
    runtime["emoji_repeat_enabled"] = not bool(
        runtime.get("emoji_repeat_enabled", True)
    )
    enabled = runtime["emoji_repeat_enabled"]
    await support.send_text(
        event,
        actions,
        "开启表情复述成功！" if enabled else "关闭表情复述成功！",
    )
    return True


@Command(f"{_REMINDER}生草").handle()
async def _grass(event, actions):
    if getattr(event, "group_id", None) is None:
        return False
    await support.send_text(event, actions, "🌿")
    return True


@Command(f"{_REMINDER}生成").handle()
async def _placeholder(event, actions):
    if getattr(event, "group_id", None) is None:
        return False
    await support.send_message(
        event,
        actions,
        UniMessage.image(os.path.abspath("./assets/sc114.png")),
    )
    return True


async def on_message(event, actions):
    if getattr(event, "group_id", None) is None:
        return False
    _ensure_runtime_defaults()
    text = str(
        getattr(event, "msg_str", getattr(event, "message", ""))
    ).strip()
    reminder = str(_runtime().get("reminder", _REMINDER))
    bot_name = str(_runtime().get("bot_name", _BOT_NAME))

    if "zzzz...涩图...嘿嘿..." in text:
        acg = plugin_state.get_plugin_module("jianerbot-plugin-generate-acg")
        generate_acg = getattr(acg, "generate_acg", None)
        if callable(generate_acg):
            await generate_acg("随机", event, actions)
        else:
            await support.send_text(
                event,
                actions,
                f"{bot_name}需要 GenerateFromACG 插件才能生成好看的涩图哦 "
                "(੭ु ˃̶͈̀ ω ˂̶͈́)੭ु⁾⁾",
            )
        return True

    if f"{bot_name}真棒" in text and reminder not in text:
        try:
            compliments = list(_others().get("compliment", ["谢谢夸奖 (◍•ᴗ•◍)❤"]))
            await support.send_text(
                event,
                actions,
                str(random.choice(compliments)),
            )
        except Exception:
            logger = plugin_state.get_logger()
            if logger is not None:
                logger.warning("不接受夸赞")
        return True

    if _runtime().get("emoji_repeat_enabled", True) and support.has_emoji(text):
        now = datetime.datetime.now()
        last = _runtime().get("emoji_send_count")
        if (
            last is None
            or now - last > datetime.timedelta(seconds=15)
        ):
            await support.send_text(event, actions, text)
            _runtime()["emoji_send_count"] = now
        else:
            logger = plugin_state.get_logger()
            if logger is not None:
                logger.debug(f"emoji +1 延迟 {abs(now - last)} s")
        return False

    return False


async def _on_notify(event, actions):
    if not isinstance(event, events.NotifyEvent):
        return
    if str(getattr(event, "sub_type", "")) != "poke":
        return
    if str(getattr(event, "target_id", "")) != str(getattr(event, "self_id", "")):
        return
    logger = plugin_state.get_logger()
    if logger is not None:
        logger.info(f"({event.user_id}) POKED")
    try:
        if not await support.poke(actions, event):
            return
        phrases = list(_others()["poke_rejection_phrases"])
        await support.send_text(event, actions, str(random.choice(phrases)))
    except KeyError:
        if logger is not None:
            logger.warning("不接受戳一戳")


async def _on_member_increase(event, actions):
    if int(getattr(event, "user_id", 0)) == int(getattr(event, "self_id", -1)):
        return
    runtime = _runtime()
    if runtime.get("wait_for_add_in", False):
        runtime["wait_for_add_in"] = False
        return
    user = event.user_id
    bot_name = str(runtime.get("bot_name", _BOT_NAME))
    reminder = str(runtime.get("reminder", _REMINDER))
    welcome = f""" 加入{bot_name}的大家庭，{bot_name}是你最忠实可爱的女朋友噢o(*≧▽≦)ツ
随时和{bot_name}交流，你只需要在问题的前面加上 {reminder} 就可以啦！( •̀ ω •́ )✧
@{bot_name} 可以看看{bot_name}会做什么有趣的事情哦~o((>ω< ))o
祝你在{bot_name}的大家庭里生活愉快！♪(≧∀≦)ゞ☆"""
    message = (
        UniMessage.image(f"http://q2.qlogo.cn/headimg_dl?dst_uin={user}&spec=640")
        + UniMessage.text("欢迎")
        + UniMessage.at(user)
        + UniMessage.text(welcome)
    )
    await support.send_message(event, actions, message)


async def _on_member_decrease(event, actions):
    bot_name = str(_runtime().get("bot_name", _BOT_NAME))
    user_nick = await support.nickname(event.user_id, actions)
    if user_nick:
        user_nick = f"@{user_nick} "
    else:
        user_nick = "有人又"
    text = f"""{user_nick}离开了{bot_name}的大家庭，{bot_name}好伤心o(TヘTo)……
大家一定要记得多来陪{bot_name}玩玩ヾ(•ω•`)o"""
    logger = plugin_state.get_logger()
    if logger is not None:
        logger.info(f"group: {event.user_id} 已离开群聊 {event.group_id}")
    await support.send_text(event, actions, text)


async def _on_group_add_invite(event, actions):
    keywords = list(_others().get("Auto_approval", []))
    cleaned_text = str(getattr(event, "comment", "") or "").strip().lower()
    for keyword in keywords:
        processed = str(keyword).strip().lower()
        if processed and processed in cleaned_text:
            try:
                user = event.user_id
                nick = await support.nickname(user, actions)
                logger = plugin_state.get_logger()
                if logger is not None:
                    logger.info(
                        f"group: {nick} 的入群回答 {processed} 符合正确答案，"
                        f"已准许入群 {event.group_id}"
                    )
                await actions.set_group_add_request(
                    flag=event.flag,
                    sub_type=event.sub_type,
                    approve=True,
                    reason="",
                )
                _runtime()["wait_for_add_in"] = True
                bot_name = str(_runtime().get("bot_name", _BOT_NAME))
                reminder = str(_runtime().get("reminder", _REMINDER))
                welcome = f"""{nick} 的答案正确，欢迎加入{bot_name}的大家庭！o(*≧▽≦)ツ
随时和{bot_name}交流，只需在问题的前面加上 {reminder} 就可以啦！( •̀ ω •́ )✧
@{bot_name} 可以看看{bot_name}会做什么有趣的事情哦~o((>ω< ))o
祝你在{bot_name}的大家庭里生活愉快！♪(≧∀≦)ゞ☆"""
                message = (
                    UniMessage.image(
                        f"http://q2.qlogo.cn/headimg_dl?dst_uin={user}&spec=640"
                    )
                    + UniMessage.text(welcome)
                )
                await support.send_message(event, actions, message)
                break
            except Exception:
                logger = plugin_state.get_logger()
                if logger is not None:
                    logger.exception("处理入群邀请失败")


async def _on_listener_start(event, actions):
    if not os.path.exists("restart.temp"):
        return
    with open("restart.temp", "r", encoding="utf-8") as handle:
        group_id = handle.read()
    os.remove("restart.temp")
    now = datetime.datetime.now().strftime("%H:%M:%S")
    await support.send_root_text(
        f"在 {now} QQ机器人已手动重启成功",
        actions=actions,
    )
    await support.send_message(
        _GroupTargetEvent(group_id),
        actions,
        UniMessage.text(
            f"{_runtime().get('bot_name', _BOT_NAME)} "
            f"{_runtime().get('bot_name_en', _BOT_NAME_EN)} - "
            f"{_runtime().get('one_slogan', _SLOGAN)}\n"
            "————————————————————\n"
            f"欢迎! {_runtime().get('bot_name', _BOT_NAME)} 已经重启成功！ "
            f"现在你可以发送 {_runtime().get('reminder', _REMINDER)}帮助 来知道更多。"
        ),
    )


class _GroupTargetEvent:
    """Minimal event shape for a system notification to a known group."""

    def __init__(self, group_id):
        self.group_id = group_id
        self.user_id = None


def setup(client, manager):
    _ensure_runtime_defaults()
    client.subscribe(_on_notify, events.NotifyEvent)
    client.subscribe(_on_member_increase, events.GroupMemberIncreaseEvent)
    client.subscribe(_on_member_decrease, events.GroupMemberDecreaseEvent)
    client.subscribe(_on_group_add_invite, events.GroupAddInviteEvent)
    client.subscribe(_on_listener_start, events.HyperListenerStartNotify)
