"""Group guessing games with reload-safe background tasks."""

import asyncio

from jianer.plugins.builtin.alconna import Command

from .. import adapter
from ..core.handler import draw_chart_info
from ..core.service import guess
from ..message import MessageSegment
from .common import (
    is_group,
    reject_unless_group_admin,
    require_data,
    require_resources,
)


_tasks = {}


def _group_id(event):
    try:
        return int(event.group_id)
    except (AttributeError, TypeError, ValueError):
        return None


def _cancel_group_task(group_id):
    task = _tasks.pop(group_id, None)
    if task is not None and task is not asyncio.current_task():
        task.cancel()


async def _reveal(group_id, event, actions, delay):
    try:
        await asyncio.sleep(delay)
        state = guess._group.get(group_id)
        if state is None or state.end or group_id not in guess.switch.enable:
            return
        state.end = True
        message = MessageSegment.text("答案是：\n")
        message += await draw_chart_info(state.song)
        guess.end(group_id)
        await adapter.send(event, actions, message, reply=False)
    except asyncio.CancelledError:
        raise
    finally:
        if _tasks.get(group_id) is asyncio.current_task():
            _tasks.pop(group_id, None)


async def _run_normal_guess(group_id, event, actions):
    try:
        await asyncio.sleep(4)
        for cycle in range(7):
            state = guess._group.get(group_id)
            if state is None or state.end or group_id not in guess.switch.enable:
                return
            if cycle < 6:
                await adapter.send(
                    event,
                    actions,
                    f"{cycle + 1}/7 这首歌{state.options[cycle]}",
                    reply=False,
                )
                await asyncio.sleep(8)
            else:
                message = MessageSegment.text("7/7 这首歌封面的一部分是：\n")
                message += MessageSegment.image(state.img)
                message += MessageSegment.text("答案将在30秒后揭晓")
                await adapter.send(event, actions, message, reply=False)
                await asyncio.sleep(30)
                state = guess._group.get(group_id)
                if state is None or state.end or group_id not in guess.switch.enable:
                    return
                state.end = True
                answer = MessageSegment.text("答案是：\n")
                answer += await draw_chart_info(state.song)
                guess.end(group_id)
                await adapter.send(event, actions, answer, reply=False)
    except asyncio.CancelledError:
        raise
    finally:
        if _tasks.get(group_id) is asyncio.current_task():
            _tasks.pop(group_id, None)


@Command("猜歌").handle()
async def handle_guess_start(event=None, actions=None):
    if not is_group(event):
        return False
    if not await require_resources(event, actions):
        return True
    group_id = _group_id(event)
    if group_id not in guess.switch.enable:
        await adapter.send(event, actions, "该群已关闭猜歌功能，开启请输入 开启mai猜歌")
        return True
    if group_id in guess._group:
        await adapter.send(event, actions, "该群已有正在进行的猜歌或猜曲绘")
        return True
    if not guess._guess_data:
        await adapter.send(event, actions, "猜歌数据为空，请先更新 maimai 数据。")
        return True
    guess.start(group_id)
    await adapter.send(
        event,
        actions,
        "我将从热门乐曲中选择一首歌，每隔8秒描述它的特征，"
        "请输入歌曲的 id、标题或别名进行猜歌。猜歌时其他命令仍可使用。",
    )
    _tasks[group_id] = asyncio.create_task(
        _run_normal_guess(group_id, event, actions),
        name=f"maimaidx-guess-{group_id}",
    )
    return True


@Command("猜曲绘").handle()
async def handle_guess_picture(event=None, actions=None):
    if not is_group(event):
        return False
    if not await require_resources(event, actions):
        return True
    group_id = _group_id(event)
    if group_id not in guess.switch.enable:
        await adapter.send(event, actions, "该群已关闭猜歌功能，开启请输入 开启mai猜歌")
        return True
    if group_id in guess._group:
        await adapter.send(event, actions, "该群已有正在进行的猜歌或猜曲绘")
        return True
    if not guess._guess_data:
        await adapter.send(event, actions, "猜歌数据为空，请先更新 maimai 数据。")
        return True
    guess.startpic(group_id)
    message = MessageSegment.text("以下裁切图片是哪首谱面的曲绘：\n")
    message += MessageSegment.image(guess._group[group_id].img)
    message += MessageSegment.text("请在30秒内输入答案")
    await adapter.send(event, actions, message)
    _tasks[group_id] = asyncio.create_task(
        _reveal(group_id, event, actions, 30),
        name=f"maimaidx-guess-picture-{group_id}",
    )
    return True


@Command("重置猜歌").handle()
async def handle_guess_reset(event=None, actions=None):
    if not is_group(event):
        return False
    if await reject_unless_group_admin(event, actions):
        return True
    group_id = _group_id(event)
    if group_id in guess._group:
        guess.end(group_id)
        _cancel_group_task(group_id)
        message = "已重置该群猜歌"
    else:
        message = "该群未处在猜歌状态"
    await adapter.send(event, actions, message)
    return True


async def _set_guess_enabled(event, actions, enabled):
    if not is_group(event):
        return False
    if await reject_unless_group_admin(event, actions):
        return True
    if not await require_data(event, actions):
        return True
    group_id = _group_id(event)
    message = await guess.on(group_id) if enabled else await guess.off(group_id)
    if not enabled:
        _cancel_group_task(group_id)
    await adapter.send(event, actions, message)
    return True


@Command("开启mai猜歌").handle()
async def handle_guess_enable(event=None, actions=None):
    return await _set_guess_enabled(event, actions, True)


@Command("关闭mai猜歌").handle()
async def handle_guess_disable(event=None, actions=None):
    return await _set_guess_enabled(event, actions, False)


async def handle_guess_answer(event, actions):
    if not is_group(event):
        return False
    group_id = _group_id(event)
    state = guess._group.get(group_id)
    if state is None:
        return False
    answer = str(getattr(event, "msg_str", "") or "").strip().lower()
    if answer not in {str(item).lower() for item in state.answer}:
        return False
    state.end = True
    message = MessageSegment.text("猜对了，答案是：\n")
    message += await draw_chart_info(state.song)
    guess.end(group_id)
    _cancel_group_task(group_id)
    await adapter.send(event, actions, message)
    return True


async def shutdown():
    tasks = list(_tasks.values())
    _tasks.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    guess._group.clear()
