"""Natural-language song, alias, ID, BPM, artist, and charter search."""

import re

from .. import adapter
from ..constants import SONGS_PER_PAGE
from ..core.clients.yuzuchan.client import YuzuChaNAPI
from ..core.clients.yuzuchan.models import AliasStatus, Songs, StatusEnum
from ..core.handler import draw_chart_info, draw_song_list
from ..core.merge.alias import yuzu_alias_to_alias
from ..core.service import mai
from ..message import MessageSegment
from .common import require_resources, require_user


def _is_float(value):
    try:
        float(value)
        return True
    except ValueError:
        return False


def _search_music(kind, raw_args):
    items = raw_args.split() if raw_args else []
    page = 1
    kind = kind.lower() if kind else None
    if kind is None:
        if not items:
            return [], page, None
        if len(items) >= 2 and items[-1].isdigit():
            title, page = " ".join(items[:-1]), int(items[-1])
        else:
            title = " ".join(items)
        return mai.total_list.filter(title=title), page, None
    if kind == "定数":
        if len(items) == 1 and _is_float(items[0]):
            low = high = float(items[0])
        elif len(items) == 2 and all(_is_float(item) for item in items):
            low, high = map(float, items)
        elif (
            len(items) == 3
            and _is_float(items[0])
            and _is_float(items[1])
            and items[2].isdigit()
        ):
            low, high, page = float(items[0]), float(items[1]), int(items[2])
        else:
            return [], page, (
                "定数查歌参数错误，格式：\n"
                "定数查歌「定数」「页数」\n"
                "定数查歌「最小定数」「最大定数」「页数」"
            )
        return mai.total_list.filter(level_value=(low, high)), page, None
    if kind == "bpm":
        if len(items) == 1 and _is_float(items[0]):
            result = mai.total_list.filter(bpm=float(items[0]))
        elif len(items) == 2 and all(_is_float(item) for item in items):
            if float(items[0]) > float(items[1]):
                result = mai.total_list.filter(bpm=float(items[0]))
                page = int(float(items[1]))
            else:
                result = mai.total_list.filter(bpm=(float(items[0]), float(items[1])))
        elif (
            len(items) == 3
            and _is_float(items[0])
            and _is_float(items[1])
            and items[2].isdigit()
        ):
            result = mai.total_list.filter(bpm=(float(items[0]), float(items[1])))
            page = int(items[2])
        else:
            return [], page, (
                "bpm查歌参数错误，格式：\n"
                "bpm查歌「bpm」「页数」\n"
                "bpm查歌「最小bpm」「最大bpm」「页数」"
            )
        return result, page, None
    if kind in {"曲师", "谱师"}:
        if not items:
            return [], page, f"{kind}查歌参数错误，请输入名称和可选页数。"
        if len(items) >= 2 and items[-1].isdigit():
            name, page = " ".join(items[:-1]), int(items[-1])
        else:
            name = " ".join(items)
        if kind == "曲师":
            result = mai.total_list.filter(artist=name)
        else:
            result = mai.total_list.filter(charter=name, all_diff=False)
        return result, page, None
    return [], page, "指令错误"


async def _render_search(event, actions, songs, page, user):
    if not songs:
        await adapter.send(
            event,
            actions,
            "没有找到这样的乐曲。\n※ 如果是别名请使用「XXX是什么歌」查询。",
        )
    elif len(songs) == 1:
        await adapter.send(event, actions, await draw_chart_info(songs[0], user))
    elif len(songs) <= 5:
        await adapter.send(
            event,
            actions,
            "".join(f"{f'「{song.song_id}」':<7} {song.song_name}\n" for song in songs).rstrip(),
        )
    else:
        await adapter.send(event, actions, draw_song_list(songs, page))


async def _alias_search(event, actions, name, page, user):
    error_message = (
        f"未找到别名为「{name}」的歌曲\n"
        "※ 可以使用「添加别名」给该乐曲添加别名\n"
        "※ 如果是歌名的一部分，请使用「查歌」查询。"
    )
    alias_data = mai.total_alias_list.by_alias(name)
    if not alias_data:
        try:
            obj = await YuzuChaNAPI().get_songs(name)
        except Exception:
            obj = None
        if isinstance(obj, Songs):
            if obj.type == StatusEnum.ONGOING and isinstance(obj.data[0], AliasStatus):
                message = f"未找到别名为「{name}」的歌曲，但找到相同别名投票：\n"
                message += "\n".join(
                    f"- {item.tag}\n    ID {item.song_id}: {item.name}" for item in obj.data
                )
                message += "\n※ 可以使用「同意别名 XXXXX」进行投票"
                await adapter.send(event, actions, message)
                return
            alias_data = yuzu_alias_to_alias(obj.data)

    if alias_data:
        if len(alias_data) != 1:
            message = f"找到{len(alias_data)}个相同别名的曲目：\n"
            message += "\n".join(
                f"{song.song_id}：{song.song_name}" for song in alias_data
            )
            message += "\n※ 请使用「id xxxxx」查询指定曲目"
            await adapter.send(event, actions, message)
            return
        song = mai.total_list.by_id(alias_data[0].song_id)
        if song:
            message = MessageSegment.text("您要找的是不是：")
            message += await draw_chart_info(song, user)
            await adapter.send(event, actions, message)
        else:
            await adapter.send(event, actions, error_message)
        return

    if name.isdigit() and (song := mai.total_list.by_id(int(name))):
        message = MessageSegment.text("您要找的是不是：")
        message += await draw_chart_info(song, user)
        await adapter.send(event, actions, message)
        return
    id_match = re.fullmatch(r"id([0-9]+)", name, re.IGNORECASE)
    if id_match:
        song = mai.total_list.by_id(int(id_match.group(1)))
        if song is None:
            await adapter.send(event, actions, f"未找到ID「{id_match.group(1)}」的乐曲")
        else:
            message = MessageSegment.text("您要找的是不是：")
            message += await draw_chart_info(song, user)
            await adapter.send(event, actions, message)
        return

    result = mai.total_list.filter(title=name)
    if not result:
        await adapter.send(event, actions, error_message)
    elif len(result) == 1:
        message = MessageSegment.text("您要找的是不是：")
        message += await draw_chart_info(result[0], user)
        await adapter.send(event, actions, message)
    elif len(result) <= 5:
        message = f"未找到别名为「{name}」的歌曲，但找到「{len(result)}」个相似标题：\n"
        message += "\n".join(
            f"{f'「{song.song_id}」':<7} {song.song_name}"
            for song in sorted(result, key=lambda item: int(item.song_id))
        )
        message += "\n※ 请使用「id xxxxx」查询指定曲目"
        await adapter.send(event, actions, message)
    else:
        message = MessageSegment.text(
            f"未找到别名为「{name}」的歌曲，但找到「{len(result)}」个相似标题：\n"
        )
        message += draw_song_list(result, int(page))
        await adapter.send(event, actions, message)


async def handle_search_patterns(event, actions):
    text = str(getattr(event, "msg_str", "") or "").strip()
    match = re.fullmatch(r"(定数|bpm|曲师|谱师)?查歌\s?(.+)", text, re.IGNORECASE)
    if match:
        if not await require_resources(event, actions):
            return True
        user = await require_user(event, actions, check_auth=True, silent_auth_error=True)
        songs, page, error = _search_music(match.group(1), match.group(2))
        if error:
            await adapter.send(event, actions, error)
        else:
            await _render_search(event, actions, songs, page, user)
        return True

    match = re.fullmatch(r"(.+)是(?:什么|啥)歌[？?]?([0-9]+)?", text, re.IGNORECASE)
    if match:
        if not await require_resources(event, actions):
            return True
        user = await require_user(event, actions, check_auth=True, silent_auth_error=True)
        await _alias_search(event, actions, match.group(1).strip(), int(match.group(2) or 1), user)
        return True

    match = re.fullmatch(r"id\s?([0-9]+)", text, re.IGNORECASE)
    if match:
        if not await require_resources(event, actions):
            return True
        user = await require_user(event, actions, check_auth=True, silent_auth_error=True)
        song = mai.total_list.by_id(int(match.group(1)))
        await adapter.send(
            event,
            actions,
            f"未找到ID「{match.group(1)}」的乐曲" if song is None else await draw_chart_info(song, user),
        )
        return True
    return False
