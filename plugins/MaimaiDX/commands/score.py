"""Score, B50, play-data, and score-line commands."""

import re
from textwrap import dedent

from jianer.plugins.builtin.alconna import Command

from .. import adapter
from ..config import log
from ..core.handler import draw_best50, draw_play_data, draw_song_galobal_data
from ..core.image.tools import text_to_bytes_io
from ..core.merge.models import ServiceName
from ..core.service import mai
from ..message import MessageSegment
from .common import (
    command_argument,
    mentioned_query_user_id,
    parse_qq_id,
    public_divingfish_user,
    require_data,
    require_resources,
    require_user,
)


async def _best50(argument, event, actions, *, all_perfect):
    if not await require_resources(event, actions):
        return True
    username = command_argument(event, "ap50" if all_perfect else "b50", argument)
    if all_perfect:
        user = await require_user(event, actions, check_auth=True)
        if user is None:
            return True
        if username:
            await adapter.send(
                event,
                actions,
                "AP50 不支持指定用户名，请直接使用「ap50」查询本人。",
            )
            return True
        if user.service == ServiceName.DIVINGFISH:
            await adapter.send(event, actions, "仅落雪查分器支持AP50指令")
            return True
    else:
        mentioned = mentioned_query_user_id(event)
        explicit_qq = parse_qq_id(username)
        if mentioned is not None:
            user = await public_divingfish_user(mentioned)
            username = ""
        elif explicit_qq is not None:
            user = await public_divingfish_user(explicit_qq)
            username = ""
        elif username:
            sender_qq = parse_qq_id(getattr(event, "user_id", None))
            if sender_qq is None:
                await adapter.send(event, actions, "无法识别当前发言者的 QQ 用户 ID。")
                return True
            user = await public_divingfish_user(sender_qq)
        else:
            user = await require_user(event, actions, check_auth=True)
            if user is None:
                return True
    result = await draw_best50(user, username=username or None, all_perfect=all_perfect)
    await adapter.send(event, actions, result)
    return True


@Command("b50").handle()
@Command("b50 <argument>").handle()
@Command("B50").handle()
@Command("B50 <argument>").handle()
async def handle_b50(argument: str = "", event=None, actions=None):
    return await _best50(argument, event, actions, all_perfect=False)


@Command("ap50").handle()
@Command("ap50 <argument>").handle()
@Command("AP50").handle()
@Command("AP50 <argument>").handle()
async def handle_ap50(argument: str = "", event=None, actions=None):
    return await _best50(argument, event, actions, all_perfect=True)


async def _info(argument, event, actions, command):
    if not await require_resources(event, actions):
        return True
    user = await require_user(event, actions, check_auth=True)
    if user is None:
        return True
    data = command_argument(event, command, argument)
    if not data:
        await adapter.send(event, actions, "请输入曲目id或曲名")
        return True
    if data.isdigit() and (song := mai.total_list.by_id(int(data))):
        pass
    elif song := mai.total_list.by_name(data):
        pass
    else:
        aliases = mai.total_alias_list.by_alias(data)
        if not aliases:
            await adapter.send(event, actions, "未找到曲目")
            return True
        if len(aliases) != 1:
            message = "找到相同别名的曲目，请使用以下ID查询：\n" + "\n".join(
                f"{item.song_id}：{item.alias[0]}" for item in aliases
            )
            await adapter.send(event, actions, message)
            return True
        song = mai.total_list.by_id(aliases[0].song_id)
        if song is None:
            await adapter.send(event, actions, "未找到曲目")
            return True
    await adapter.send(event, actions, await draw_play_data(user, song))
    return True


for _info_command in ("info", "Info", "INFO", "minfo", "Minfo", "MINFO"):
    Command(_info_command).handle()(
        lambda event=None, actions=None, _command=_info_command: _info(
            "", event, actions, _command
        )
    )
    Command(f"{_info_command} <argument>").handle()(
        lambda argument="", event=None, actions=None, _command=_info_command: _info(
            argument, event, actions, _command
        )
    )


async def _ginfo(argument, event, actions, command):
    if not await require_resources(event, actions):
        return True
    args = command_argument(event, command, argument)
    if not args:
        await adapter.send(event, actions, "请输入曲目id或曲名")
        return True
    if args[0] not in "绿黄红紫白":
        level_index = 3
    else:
        level_index = "绿黄红紫白".index(args[0])
        args = args[1:].strip()
        if not args:
            await adapter.send(event, actions, "请输入曲目id或曲名")
            return True
    if args.isdigit() and (song := mai.total_list.by_id(int(args))):
        pass
    elif song := mai.total_list.by_name(args):
        pass
    else:
        aliases = mai.total_alias_list.by_alias(args)
        if not aliases:
            await adapter.send(event, actions, "未找到曲目")
            return True
        if len(aliases) != 1:
            message = "找到相同别名的曲目，请使用以下ID查询：\n" + "\n".join(
                f"{item.song_id}：{item.alias[0]}" for item in aliases
            )
            await adapter.send(event, actions, message)
            return True
        song = mai.total_list.by_id(aliases[0].song_id)
    if song is None or level_index >= len(song.difficulties):
        await adapter.send(event, actions, "该乐曲没有这个等级")
        return True
    stats = song.difficulties[level_index].stats
    if stats is None:
        await adapter.send(event, actions, "该等级没有统计信息")
        return True
    message = await draw_song_galobal_data(song, level_index)
    message += MessageSegment.text(
        dedent(
            f"""\
            游玩次数：{round(stats.cnt)}
            拟合难度：{stats.fit_diff:.2f}
            平均达成率：{stats.avg:.2f}%
            平均 DX 分数：{stats.avg_dx:.1f}
            谱面成绩标准差：{stats.std_dev:.2f}"""
        )
    )
    await adapter.send(event, actions, message)
    return True


for _ginfo_command in ("ginfo", "Ginfo", "GINFO"):
    Command(_ginfo_command).handle()(
        lambda event=None, actions=None, _command=_ginfo_command: _ginfo(
            "", event, actions, _command
        )
    )
    Command(f"{_ginfo_command} <argument>").handle()(
        lambda argument="", event=None, actions=None, _command=_ginfo_command: _ginfo(
            argument, event, actions, _command
        )
    )


@Command("分数线").handle()
@Command("分数线 <argument>").handle()
async def handle_score_line(argument: str = "", event=None, actions=None):
    if not await require_data(event, actions):
        return True
    raw = command_argument(event, "分数线", argument)
    args = raw.split()
    if args and args[0] == "帮助":
        help_text = dedent(
            """\
            此功能为查找某首歌分数线设计。
            命令格式：分数线「难度+歌曲id」「分数线」
            例如：分数线 紫799 100
            命令将返回分数线允许的 TAP GREAT 容错，
            以及 BREAK 50落等价的 TAP GREAT 数。
                    GREAT / GOOD / MISS
            TAP         1 / 2.5  / 5
            HOLD        2 / 5    / 10
            SLIDE       3 / 7.5  / 15
            TOUCH       1 / 2.5  / 5
            BREAK       5 / 12.5 / 25 (外加200落)"""
        ).strip()
        if await require_resources(event, actions):
            await adapter.send(event, actions, MessageSegment.image(text_to_bytes_io(help_text)))
        return True
    try:
        result = re.search(r"([绿黄红紫白])\s?([0-9]+)", raw)
        if result is None:
            raise ValueError
        level_labels = ["绿", "黄", "红", "紫", "白"]
        level_names = ["Basic", "Advanced", "Expert", "Master", "Re:MASTER"]
        level_index = level_labels.index(result.group(1))
        chart_id = int(result.group(2))
        line = float(args[-1])
        song = mai.total_list.by_id(chart_id)
        if song is None or level_index >= len(song.difficulties):
            raise ValueError
        chart = song.difficulties[level_index]
        tap, slide, hold = int(chart.notes.tap), int(chart.notes.slide), int(chart.notes.hold)
        touch, brk = int(chart.notes.touch), int(chart.notes.brk)
        total_score = tap * 500 + slide * 1500 + hold * 1000 + touch * 500 + brk * 2500
        if brk <= 0:
            raise ValueError
        break_50_reduce = total_score * (0.01 / brk) / 4
        reduce = 101 - line
        if reduce <= 0 or reduce >= 101:
            raise ValueError
        message = (
            f"{song.song_name}「{level_names[level_index]}」\n"
            f"分数线「{line}%」\n允许的最多 TAP GREAT 数量为\n"
            f"「{(total_score * reduce / 10000):.2f}」"
            f"(每个-{10000 / total_score:.4f}%),\n"
            f"BREAK 50落(一共「{brk}」个)\n"
            f"等价于「{(break_50_reduce / 100):.3f}」个 TAP GREAT"
            f"(-{break_50_reduce / total_score * 100:.4f}%)"
        )
    except (AttributeError, IndexError, ValueError, ZeroDivisionError) as exc:
        log.warning(f"分数线参数错误: {type(exc).__name__}")
        message = "格式错误，输入“分数线 帮助”以查看帮助信息"
    await adapter.send(event, actions, message)
    return True
