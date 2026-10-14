"""Yuri-YuzuChaN SSE alias push integration for JianerCore."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from textwrap import dedent
from types import SimpleNamespace
from typing import Any

import httpx

from .. import adapter
from ..config import log, maiconfig
from ..constants import VOTE_URL
from ..message import MessageSegment
from .clients.yuzuchan.models import PushAliasStatus, SSEMessage
from .handler import draw_chart_info
from .service import alias, mai


SSE_RECONNECT_DELAY = 3.0
SSE_RECONNECT_DELAY_MAX = 60.0


async def iter_sse(lines: AsyncIterator[str]) -> AsyncIterator[SSEMessage]:
    """Parse a text/event-stream response into complete SSE messages."""

    event = "message"
    data: list[str] = []
    event_id: str | None = None
    retry: int | None = None
    async for line in lines:
        if not line:
            if data:
                yield SSEMessage(
                    event=event,
                    data="\n".join(data),
                    id=event_id,
                    retry=retry,
                )
            event = "message"
            data = []
            event_id = None
            retry = None
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
        elif field == "id" and "\0" not in value:
            event_id = value
        elif field == "retry" and value.isdecimal():
            retry = int(value)
    if data:
        yield SSEMessage(
            event=event,
            data="\n".join(data),
            id=event_id,
            retry=retry,
        )


async def push_alias(
    push: PushAliasStatus,
    actions: Any,
    *,
    self_id: int | str,
) -> None:
    if not maiconfig.maimaidx_alias_push:
        await mai.get_music_alias()
        return
    if push.type != "Apply" or not push.status:
        return

    group_ids = {
        int(item["group_id"])
        for item in await adapter.get_group_list(actions)
        if item.get("group_id") is not None
    }
    messages: list[Any] = [
        MessageSegment.text(
            "检测到新的别名申请，可使用同意别名指令进行投票，"
            f"点击下方链接查看详情：「{VOTE_URL}」\n"
            "如果不需要接收推送消息，请使用「关闭别名推送」指令关闭推送"
        )
    ]
    for item in push.status:
        song = mai.total_list.by_id(item.song_id)
        if push.type != "Apply" or song is None:
            continue
        messages.append(
            MessageSegment.text(
                dedent(
                    f"""\
                    {item.tag}：
                    ID：{item.song_id}
                    标题：{song.song_name}
                    别名：{item.apply_alias}
                    """
                ).strip()
            )
            + await draw_chart_info(song)
        )
    if len(messages) == 1:
        return

    for group_id in group_ids:
        if group_id in alias.push.disable:
            continue
        try:
            await adapter.send_group_forward(
                actions,
                group_id,
                messages,
                self_id=self_id,
                nickname=maiconfig.bot_name,
            )
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(f"群：「{group_id}」推送消息失败")


async def _wait_or_stop(stop_event: asyncio.Event, seconds: float) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def sse_alias_server(
    context: Callable[[], tuple[Any | None, int | str | None]],
    stop_event: asyncio.Event,
) -> None:
    if maiconfig.maimaidx_alias_proxy:
        api = "https://www.yuzuchan.cn/api/v2/events"
    else:
        api = "https://www.yuzuchan.moe/api/v2/events"

    reconnect_delay = SSE_RECONNECT_DELAY
    last_event_id: str | None = None
    timeout = httpx.Timeout(connect=30, read=None, write=30, pool=30)
    while not stop_event.is_set():
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
            ) as session:
                headers = {"Accept": "text/event-stream"}
                if last_event_id is not None:
                    headers["Last-Event-ID"] = last_event_id
                async with session.stream("GET", api, headers=headers) as response:
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "")
                    if (
                        content_type.partition(";")[0].strip().lower()
                        != "text/event-stream"
                    ):
                        raise httpx.RemoteProtocolError(
                            "服务器返回了非 SSE 响应: "
                            f"{content_type or 'unknown'}"
                        )
                    log.success("别名推送服务器连接成功")
                    async for message in iter_sse(response.aiter_lines()):
                        if message.id is not None:
                            last_event_id = message.id or None
                        if message.retry is not None:
                            reconnect_delay = min(
                                max(message.retry / 1000, 0.1),
                                SSE_RECONNECT_DELAY_MAX,
                            )
                        if message.event != "alias":
                            continue
                        try:
                            payload = json.loads(message.data)
                            if payload.get("type") != "Apply":
                                continue
                            push = PushAliasStatus.model_validate(payload)
                            actions, self_id = context()
                            if actions is not None and self_id is not None:
                                await push_alias(
                                    push,
                                    actions,
                                    self_id=self_id,
                                )
                        except ValueError as error:
                            log.warning(f"收到无效的别名推送事件: {error}")
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            log.exception("处理别名推送事件失败")
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError as error:
            log.warning(
                f"别名推送服务器连接异常: {error}，"
                f"将在 {reconnect_delay:g} 秒后重连"
            )
        except Exception:
            log.exception(
                f"别名推送服务器连接失败，将在 {reconnect_delay:g} 秒后重试"
            )
        if await _wait_or_stop(stop_event, reconnect_delay):
            return

