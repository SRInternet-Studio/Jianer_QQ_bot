"""Alias lookup, local additions, voting, and push controls."""

import re
from textwrap import dedent

from jianer.plugins.builtin.alconna import Command

from .. import adapter
from ..config import log
from ..constants import SONGS_PER_PAGE
from ..core.clients.yuzuchan.client import YuzuChaNAPI
from ..core.clients.yuzuchan.models import Alias as ServerAlias
from ..core.image.tools import text_to_bytes_io
from ..core.service import alias, mai, update_local_alias
from ..message import MessageSegment
from .common import (
    command_argument,
    is_group,
    is_private,
    reject_unless_group_admin,
    reject_unless_superuser,
    require_data,
    require_resources,
)


@Command("更新别名库").handle()
async def handle_update_alias(event=None, actions=None):
    if await reject_unless_superuser(event, actions):
        return True
    if not is_private(event):
        await adapter.send(event, actions, "请私聊 BOT 使用该命令。")
        return True
    try:
        await mai.get_music_alias()
        message = "手动更新别名库成功"
        log.info(message)
    except Exception:
        message = "手动更新别名库失败"
        log.exception(message)
    await adapter.send(event, actions, message)
    return True


async def _local_alias(argument, event, actions, command):
    if not await require_data(event, actions):
        return True
    args = command_argument(event, command, argument).split()
    if len(args) != 2:
        await adapter.send(event, actions, "参数错误")
        return True
    song_id, alias_name = args
    if not song_id.isdigit():
        await adapter.send(event, actions, "请输入正确的ID")
        return True
    numeric_id = int(song_id)
    if not mai.total_list.by_id(numeric_id):
        await adapter.send(event, actions, f"未找到ID「{song_id}」的曲目")
        return True
    server_alias = await YuzuChaNAPI().get_aliases(song_id=numeric_id)
    if isinstance(server_alias, ServerAlias) and alias_name.lower() in server_alias.alias:
        await adapter.send(event, actions, f"该曲目的别名「{alias_name}」已存在别名服务器")
        return True
    local_aliases = mai.total_alias_list.by_id(numeric_id)
    if local_aliases and alias_name.lower() in local_aliases[0].alias:
        await adapter.send(event, actions, "本地别名库已存在该别名")
        return True
    saved = await update_local_alias(numeric_id, alias_name)
    message = (
        f"已成功为ID「{song_id}」添加别名「{alias_name}」到本地别名库"
        if saved
        else "添加本地别名失败"
    )
    await adapter.send(event, actions, message)
    return True


for _local_command in ("添加本地别名", "添加本地别称"):
    Command(_local_command).handle()(
        lambda event=None, actions=None, _command=_local_command: _local_alias(
            "", event, actions, _command
        )
    )
    Command(f"{_local_command} <argument>").handle()(
        lambda argument="", event=None, actions=None, _command=_local_command: _local_alias(
            argument, event, actions, _command
        )
    )


async def _apply_alias(argument, event, actions, command):
    if not is_group(event):
        return False
    if not await require_data(event, actions):
        return True
    args = command_argument(event, command, argument).split()
    if len(args) < 2 or not args[0].isdigit():
        await adapter.send(event, actions, "参数错误，请输入正确的曲目 ID 和别名。")
        return True
    song_id, alias_name = args[0], " ".join(args[1:])
    if not mai.total_list.by_id(int(song_id)):
        await adapter.send(event, actions, f"未找到ID「{song_id}」的曲目")
        return True
    try:
        api = YuzuChaNAPI()
        existing = await api.get_aliases(song_id=song_id)
        if isinstance(existing, ServerAlias) and alias_name.lower() in existing.alias:
            await adapter.send(event, actions, f"该曲目的别名「{alias_name}」已存在别名服务器")
            return True
        result = await api.post_alias(song_id, alias_name, event.user_id, event.group_id)
        message = result.message
    except Exception as exc:
        log.exception("提交别名申请失败")
        message = str(exc)
    await adapter.send(event, actions, message)
    return True


for _apply_command in ("添加别名", "申请别名", "增加别名", "增添别名", "添加别称"):
    Command(_apply_command).handle()(
        lambda event=None, actions=None, _command=_apply_command: _apply_alias(
            "", event, actions, _command
        )
    )
    Command(f"{_apply_command} <argument>").handle()(
        lambda argument="", event=None, actions=None, _command=_apply_command: _apply_alias(
            argument, event, actions, _command
        )
    )


async def _agree_alias(argument, event, actions, command):
    if not is_group(event):
        return False
    if not await require_data(event, actions):
        return True
    try:
        tag = command_argument(event, command, argument).upper()
        message = (await YuzuChaNAPI().post_agree_user(tag, event.user_id)).message
    except Exception as exc:
        log.exception("别名投票失败")
        message = str(exc)
    await adapter.send(event, actions, message)
    return True


for _agree_command in ("同意别名", "同意别称"):
    Command(_agree_command).handle()(
        lambda event=None, actions=None, _command=_agree_command: _agree_alias(
            "", event, actions, _command
        )
    )
    Command(f"{_agree_command} <argument>").handle()(
        lambda argument="", event=None, actions=None, _command=_agree_command: _agree_alias(
            argument, event, actions, _command
        )
    )


async def _alias_status(argument, event, actions, command):
    if not await require_resources(event, actions):
        return True
    try:
        raw_page = command_argument(event, command, argument)
        status = await YuzuChaNAPI().get_status()
        if not status:
            await adapter.send(event, actions, "未查询到正在进行的别名投票")
            return True
        total_pages = (len(status) + SONGS_PER_PAGE - 1) // SONGS_PER_PAGE
        page = max(min(int(raw_page), total_pages), 1) if raw_page.isdigit() else 1
        rows = []
        for number, item in enumerate(status):
            if (page - 1) * SONGS_PER_PAGE <= number < page * SONGS_PER_PAGE:
                apply_alias = item.apply_alias[:15] + "..." if len(item.apply_alias) > 15 else item.apply_alias
                rows.append(
                    dedent(
                        f"""\
                        - {item.tag}：
                        - ID：{item.song_id}
                        - 别名：{apply_alias}
                        - 票数：{item.agree_votes}/{item.votes}"""
                    )
                )
        rows.append(f"第「{page}」页，共「{total_pages}」页")
        message = MessageSegment.image(text_to_bytes_io("\n".join(rows)))
    except Exception as exc:
        log.exception("查询别名投票失败")
        message = str(exc)
    await adapter.send(event, actions, message)
    return True


for _status_command in ("当前投票", "当前别名投票", "当前别称投票"):
    Command(_status_command).handle()(
        lambda event=None, actions=None, _command=_status_command: _alias_status(
            "", event, actions, _command
        )
    )
    Command(f"{_status_command} <argument>").handle()(
        lambda argument="", event=None, actions=None, _command=_status_command: _alias_status(
            argument, event, actions, _command
        )
    )


async def _show_aliases(event, actions, find_id, name):
    aliases = None
    if find_id and name.isdigit():
        aliases = mai.total_alias_list.by_id(int(name))
    else:
        aliases = mai.total_alias_list.by_alias(name)
        if not aliases and name.isdigit():
            aliases = mai.total_alias_list.by_id(int(name))
    if not aliases:
        await adapter.send(
            event,
            actions,
            "未找到此歌曲\n可以使用「添加别名」给该乐曲添加别名",
        )
        return
    if len(aliases) != 1:
        message = "\n======\n".join(
            f"ID：{item.song_id}\n" + "\n".join(item.alias) for item in aliases
        )
        await adapter.send(event, actions, f"找到{len(aliases)}个相同别名的曲目：\n{message}")
        return
    real_aliases = [
        item for item in aliases[0].alias if item.lower() != aliases[0].song_name.lower()
    ]
    if not real_aliases:
        await adapter.send(event, actions, "该曲目没有别名")
        return
    await adapter.send(
        event,
        actions,
        f"该曲目有以下别名：\nID：{aliases[0].song_id}\n" + "\n".join(real_aliases),
    )


async def handle_alias_patterns(event, actions):
    text = str(getattr(event, "msg_str", "") or "").strip()
    match = re.fullmatch(r"(id(?=[\s0-9]))?\s?(.+)\s?有什么别[名称]", text, re.IGNORECASE)
    if match:
        if not await require_data(event, actions):
            return True
        await _show_aliases(event, actions, bool(match.group(1)), match.group(2).strip())
        return True

    match = re.fullmatch(r"(开启|关闭)别名推送", text)
    if match:
        if not is_group(event):
            return False
        if await reject_unless_group_admin(event, actions):
            return True
        message = await alias.on(event.group_id) if match.group(1) == "开启" else await alias.off(event.group_id)
        await adapter.send(event, actions, message)
        return True

    match = re.fullmatch(r"全局(开启|关闭)别名推送", text)
    if match:
        if await reject_unless_superuser(event, actions):
            return True
        groups = await adapter.get_group_list(actions)
        group_ids = [int(item["group_id"]) for item in groups]
        enabled = match.group(1) == "开启"
        await alias.alias_global_change(enabled, group_ids)
        await adapter.send(
            event,
            actions,
            "已全局开启maimai别名推送" if enabled else "已全局关闭maimai别名推送",
        )
        return True
    return False
