"""Native broadcast, broadcast blacklist, and scheduled messages."""

from __future__ import annotations

import asyncio
import datetime
import os
import random

from jianer import events
from jianer import segments as Segments
from jianer.plugins import PluginMetadata
from jianer.plugins.builtin.alconna import Target, UniMessage

from bot import plugin_state
from bot import plugin_support as support
from bot.utils import load_blacklist


__plugin_meta__ = PluginMetadata(
    name="jianerbot-plugin-broadcast",
    description="原生群发、群发黑名单和定时消息。",
    usage=(
        "{reminder}群发 (内容) —> 向所有群发送消息\n"
        "{reminder}群发黑名单 —> 查看群发黑名单面板\n"
        "{reminder}列出黑名单 / {reminder}添加黑名单 / {reminder}删除黑名单\n"
        "{reminder}修改 HH:MM (内容) —> 设置每日定时群发"
    ),
    requires={"jianerbot-plugin-alconna"},
)

_REMINDER = str(plugin_state.get_runtime().get("reminder", "~"))
_BOT_NAME = str(plugin_state.get_runtime().get("bot_name", "简儿"))
_BOT_NAME_EN = str(plugin_state.get_runtime().get("bot_name_en", "Jianer"))
_SLOGAN = str(plugin_state.get_runtime().get("one_slogan", ""))
_BLACKLIST_FILE = "blacklist.sr"

_timing_task: asyncio.Task | None = None
_timing_actions = None


def _runtime() -> dict:
    return plugin_state.get_runtime()


def _banner() -> str:
    name = f"{_BOT_NAME} {_BOT_NAME_EN}"
    if _SLOGAN:
        name = f"{name} - {_SLOGAN}"
    return f"{name}\n————————————————————\n"


def _confused() -> str:
    template = str(
        dict(getattr(plugin_state.get_config(), "others", {}) or {}).get(
            "confused_words",
            "不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱",
        )
    )
    return template.format(bot_name=_BOT_NAME)


async def _send(event, actions, text: str, *, reply: bool = False):
    await support.send_text(event, actions, text, reply=reply)


async def _send_all_groups(actions, payload: UniMessage, event=None) -> None:
    groups = await support.group_list(actions)
    if not groups:
        if event is not None and support.is_feishu_protocol(actions):
            await _send(event, actions, support.UNSUPPORTED_GROUP_LIST)
        return
    blacklist = load_blacklist()
    for group in groups:
        group_id = str(group.get("group_id", ""))
        if not group_id or group_id in blacklist:
            continue
        await UniMessage(payload).send(
            target=Target.group(group_id),
            actions=actions,
        )
        await asyncio.sleep(random.random() * 3)


async def _broadcast(event, actions, order: str) -> None:
    if not support.is_admin(event):
        await _send(event, actions, _confused())
        return
    words = order.split(" ")
    if len(words) < 2 and len(getattr(event, "message", ()) or ()) == 1:
        await _send(
            event,
            actions,
            "群发格式错误 Σ( ° △ °|||)︴\n"
            f"举个🌰子：{_REMINDER}群发 {_BOT_NAME}有更新新功能啦！ —> "
            f"在所有群聊中发送消息 “{_BOT_NAME}有更新新功能啦！”",
            reply=True,
        )
        return
    message = UniMessage()
    segments = list(getattr(event, "message", ()) or ())
    if segments and isinstance(segments[0], Segments.Text):
        new_text = str(segments[0])
        new_text = new_text.replace(f"{_REMINDER}群发 ", "", 1)
        new_text = new_text.replace(f"{_REMINDER}群发", "", 1)
        message += UniMessage.text(new_text)
        for segment in segments[1:]:
            message.append(segment)
    else:
        for segment in segments:
            message.append(segment)
    await support.send_root_message(
        UniMessage.text(
            f"用户 {await support.nickname(event.user_id, actions, getattr(event, 'sender', None))} "
            f"在 {getattr(event, 'time_str', '')} 启动群发消息：\n"
        ) + message,
        actions=actions,
    )
    await support.send_message(
        event,
        actions,
        UniMessage.text("已启动群发消息：\n") + message,
        reply=True,
    )
    await _send_all_groups(actions, message, event)


async def _modify_timing(event, actions, order: str) -> None:
    if not support.is_admin(event):
        await _send(event, actions, _confused())
        return
    try:
        value = order[len("修改 "):].strip()
        if not value[:5].replace(":", "").isdigit() or value[2:3] != ":":
            raise ValueError
        hour, minute = int(value[:2]), int(value[3:5])
        if hour > 23 or minute > 59:
            raise ValueError
        content = value[6:].strip()
        with open("timing_message.ini", "w", encoding="utf-8") as handle:
            handle.write(f"{value[:5]}⊕{content}")
        nick = await support.nickname(
            event.user_id,
            actions,
            getattr(event, "sender", None),
        )
        await support.send_root_text(
            f"用户 {nick} 在 {getattr(event, 'time_str', '')} "
            f"将机器人的定时群发消息修改为时间：{value[:5]} \n内容：{content}",
            actions=actions,
        )
        await _send(event, actions, f"{_BOT_NAME}设置成功！(*≧▽≦) ", reply=True)
    except Exception:
        await _send(
            event,
            actions,
            f"{_BOT_NAME}不能识别给定的时间是什么 Σ( ° △ °|||)︴\n"
            f"举个🌰子：{_REMINDER}修改 00:00 早安 —> 即可让{_BOT_NAME}在0点0分准时问候早安噢⌯oᴗo⌯",
            reply=True,
        )


async def _blacklist_add(event, actions, order: str) -> None:
    if not support.is_admin(event):
        await _send(event, actions, _confused())
        return
    target = order[len("添加黑名单 "):].strip()
    blacklist = load_blacklist()
    if target in blacklist:
        await _send(event, actions, f"黑名单添加失败,是因为{target}已在黑名单", reply=True)
        return
    blacklist.add(target)
    try:
        with open(_BLACKLIST_FILE, "w", encoding="utf-8") as handle:
            for item in blacklist:
                handle.write(item + "\n")
        await support.send_root_text(
            f"用户 {await support.nickname(event.user_id, actions, getattr(event, 'sender', None))} "
            f"在 {getattr(event, 'time_str', '')} 将群 {target} 添加到禁止群发黑名单",
            actions=actions,
        )
        await _send(event, actions, f"黑名单添加成功\n现在的群发黑名单: {blacklist}", reply=True)
    except Exception as exc:
        await _send(event, actions, f"黑名单添加失败, 是因为\n{exc}", reply=True)


async def _blacklist_remove(event, actions, order: str) -> None:
    if not support.is_admin(event):
        await _send(event, actions, _confused())
        return
    target = order[len("删除黑名单 "):].strip()
    blacklist = load_blacklist()
    if target not in blacklist:
        await _send(event, actions, f"黑名单删除失败, 是因为群{target}不在黑名单", reply=True)
        return
    blacklist.remove(target)
    try:
        with open(_BLACKLIST_FILE, "w", encoding="utf-8") as handle:
            for item in blacklist:
                handle.write(item + "\n")
        await support.send_root_text(
            f"用户 {await support.nickname(event.user_id, actions, getattr(event, 'sender', None))} "
            f"在 {getattr(event, 'time_str', '')} 将群 {target} 从禁止群发黑名单中删除",
            actions=actions,
        )
        await _send(event, actions, f"黑名单删除成功\n现在黑名单: {blacklist}", reply=True)
    except Exception as exc:
        await _send(event, actions, f"黑名单删除失败, 是因为\n{exc}", reply=True)


async def _send_blacklist_panel(event, actions) -> None:
    await _send(
        event,
        actions,
        f"""{_BOT_NAME} {_BOT_NAME_EN} - 群发黑名单管理控制面板
————————————————————
{_REMINDER}列出黑名单 —> 显示所有黑名单群组
{_REMINDER}删除黑名单 +群号 —> 允许群发消息到该群
{_REMINDER}添加黑名单 +群号 —> 禁止群发消息到该群
""",
        reply=True,
    )


async def _timing_loop() -> None:
    global _timing_actions
    while True:
        if not os.path.isfile("timing_message.ini"):
            await asyncio.sleep(60)
            continue
        try:
            with open("timing_message.ini", "r", encoding="utf-8") as handle:
                content = handle.read().strip()
            if "⊕" not in content:
                await asyncio.sleep(60)
                continue
            first, rest = content.split("⊕", 1)
            time_part = first.strip()
            message_text = rest.strip()
            now = datetime.datetime.now()
            if f"{now.hour:02}:{now.minute:02}" == time_part and _timing_actions:
                await _send_all_groups(
                    _timing_actions,
                    UniMessage.text(message_text),
                )
            await asyncio.sleep(60 - now.second)
        except Exception:
            logger = plugin_state.get_logger()
            if logger is not None:
                logger.exception("定时群发失败")
            await asyncio.sleep(60)


async def _start_timing(actions) -> None:
    global _timing_task, _timing_actions
    _timing_actions = actions
    if _timing_task is not None and not _timing_task.done():
        return
    _timing_task = asyncio.create_task(_timing_loop())


async def _on_listener_start(event, actions):
    await _start_timing(actions)


async def on_message_observe(event, actions):
    await _start_timing(actions)


async def on_message(event, actions):
    if getattr(event, "group_id", None) is None:
        return False
    text = str(
        getattr(event, "msg_str", getattr(event, "message", ""))
    ).strip()
    reminder = str(_runtime().get("reminder", _REMINDER))
    order = ""
    index = text.find(reminder)
    if index != -1:
        order = text[index + len(reminder):].strip()

    if text == f"{reminder}群发黑名单":
        await _send_blacklist_panel(event, actions)
        return True
    if order == "群发" or order.startswith("群发 "):
        await _broadcast(event, actions, order)
        return True
    if order == "列出黑名单":
        if not support.is_admin(event):
            await _send(event, actions, _confused())
        else:
            await _send(event, actions, f"黑名单列表加载完成: {load_blacklist()}", reply=True)
        return True
    if order.startswith("添加黑名单 "):
        await _blacklist_add(event, actions, order)
        return True
    if order.startswith("删除黑名单 "):
        await _blacklist_remove(event, actions, order)
        return True
    if order.startswith("修改 "):
        await _modify_timing(event, actions, order)
        return True
    return False


def setup(client, manager):
    client.subscribe(_on_listener_start, events.HyperListenerStartNotify)


async def shutdown(client, manager):
    global _timing_task
    task, _timing_task = _timing_task, None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
