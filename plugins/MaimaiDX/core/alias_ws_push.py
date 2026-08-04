"""YuzuChaN alias WebSocket push integration for JianerCore adapters."""

import asyncio
import json
import re
from types import SimpleNamespace
from typing import Any, Callable

import httpx
from httpx_ws import (
    WebSocketDisconnect,
    WebSocketInvalidTypeReceived,
    WebSocketNetworkError,
    WebSocketUpgradeError,
    aconnect_ws,
)

from .. import adapter
from ..config import log, maiconfig
from ..constants import UUID, VOTE_URL
from ..message import MessageSegment
from .clients.yuzuchan.models import PushAliasStatus
from .handler import draw_chart_info
from .service import alias, mai


INITIAL_RECONNECT_SECONDS = 5.0
MAX_RECONNECT_SECONDS = 60.0
_WS_PATH = re.compile(r"/ws/[A-Za-z0-9_-]+")


def _event(group_id: int | str, user_id: int | str = 0) -> Any:
    return SimpleNamespace(
        group_id=group_id,
        user_id=user_id,
        message_id=None,
    )


async def push_alias(
    push: PushAliasStatus,
    actions: Any,
    *,
    self_id: int | str,
) -> None:
    if not push.status:
        log.warning(
            "Yuri-YuzuChaN别名实时事件状态为空："
            f"type={push.type}"
        )
        return

    if push.type in {"Approved", "Reject"}:
        status = push.status[0]
        if push.type == "Approved":
            text = (
                "\n您申请的别名已通过审核\n"
                "=================\n"
                f"{status.tag}：\nID：{status.song_id}\n标题：{status.name}\n"
                f"别名：{status.apply_alias}\n=================\n"
                f"请使用指令「同意别名 {status.tag}」进行投票"
            )
        else:
            text = (
                "\n您申请的别名被拒绝\n"
                "=================\n"
                f"ID：{status.song_id}\n标题：{status.name}\n"
                f"别名：{status.apply_alias}"
            )
        message = MessageSegment.at(status.apply_uid) + MessageSegment.text(text)
        song = mai.total_list.by_id(status.song_id)
        if song is not None:
            message += await draw_chart_info(song)
        await adapter.send(
            _event(status.group_id, status.apply_uid),
            actions,
            message,
            reply=False,
        )
        log.info(
            "Yuri-YuzuChaN别名事件推送完成："
            f"type={push.type}, items={len(push.status)}, targets=1"
        )
        return

    if not maiconfig.maimaidx_alias_push:
        await mai.get_music_alias()
        return

    messages = []
    for status in push.status:
        song = mai.total_list.by_id(status.song_id)
        if song is None:
            continue
        if push.type == "Apply":
            text = (
                "检测到新的别名申请\n"
                "=================\n"
                f"{status.tag}：\nID：{status.song_id}\n标题：{song.song_name}\n"
                f"别名：{status.apply_alias}\n浏览{VOTE_URL}查看详情"
            )
        elif push.type == "End":
            text = (
                "检测到新增别名\n"
                "=================\n"
                f"ID：{status.song_id}\n标题：{song.song_name}\n"
                f"别名：{status.apply_alias}"
            )
        else:
            continue
        messages.append(MessageSegment.text(text) + await draw_chart_info(song))

    if not messages:
        log.warning(
            "Yuri-YuzuChaN别名实时事件未生成可发送内容："
            f"type={push.type}, items={len(push.status)}"
        )
        return

    groups = await adapter.get_group_list(actions)
    group_ids = {int(item["group_id"]) for item in groups}

    target_count = 0
    success_count = 0
    for group_id in group_ids:
        if group_id in alias.push.disable:
            continue
        target_count += 1
        try:
            await adapter.send_group_forward(
                actions,
                group_id,
                messages,
                self_id=self_id,
                nickname=maiconfig.bot_name,
            )
            success_count += 1
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(f"向群 {group_id} 推送别名失败")
    log.info(
        "Yuri-YuzuChaN别名事件推送完成："
        f"type={push.type}, items={len(push.status)}, messages={len(messages)}, "
        f"targets={success_count}/{target_count}"
    )


async def _wait_or_stop(stop_event: asyncio.Event, seconds: float) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


def _exception_leaves(exc: BaseException) -> list[BaseException]:
    nested = getattr(exc, "exceptions", None)
    if not nested:
        return [exc]
    leaves: list[BaseException] = []
    for item in nested:
        leaves.extend(_exception_leaves(item))
    return leaves


def _safe_exception_text(exc: BaseException) -> str:
    """Expose the useful leaf cause without leaking the per-bot WS path."""

    parts: list[str] = []
    for leaf in _exception_leaves(exc)[:4]:
        chain: list[str] = []
        current: BaseException | None = leaf
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            message = " ".join(str(current).split())
            message = _WS_PATH.sub("/ws/[redacted]", message)
            detail = type(current).__name__
            if message:
                detail += f": {message[:240]}"
            chain.append(detail)
            current = current.__cause__
        parts.append(" <- ".join(chain))
    return "; ".join(parts)[:800] or type(exc).__name__


def _is_connection_error(exc: BaseException) -> bool:
    connection_errors = (
        WebSocketDisconnect,
        WebSocketNetworkError,
        WebSocketUpgradeError,
        httpx.LocalProtocolError,
        httpx.TransportError,
        OSError,
    )
    leaves = _exception_leaves(exc)
    return bool(leaves) and all(
        isinstance(item, connection_errors) for item in leaves
    )


async def ws_alias_server(
    context: Callable[[], tuple[Any | None, int | str | None]],
    stop_event: asyncio.Event,
) -> None:
    log.info("正在连接别名推送服务器")
    host = (
        "www.yuzuchan.cn/api/v2/aliases"
        if maiconfig.maimaidx_alias_proxy
        else "www.yuzuchan.moe/api/v2/aliases"
    )
    reconnect_seconds = INITIAL_RECONNECT_SECONDS
    while not stop_event.is_set():
        disconnect_error: BaseException | None = None
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as session:
                async with aconnect_ws(f"wss://{host}/ws/{UUID}", session) as ws:
                    log.success("别名推送服务器连接成功")
                    reconnect_seconds = INITIAL_RECONNECT_SECONDS
                    while not stop_event.is_set():
                        try:
                            data = await ws.receive_text()
                        except (
                            WebSocketDisconnect,
                            WebSocketNetworkError,
                            httpx.LocalProtocolError,
                        ) as exc:
                            # Catch inside httpx-ws' TaskGroup context. Otherwise
                            # it deliberately re-raises this as ExceptionGroup.
                            disconnect_error = exc
                            break
                        except WebSocketInvalidTypeReceived:
                            log.warning("忽略非文本的别名推送消息")
                            continue
                        if data == "Hello":
                            log.info("别名推送服务器正常运行")
                            continue
                        try:
                            status = PushAliasStatus.model_validate(json.loads(data))
                        except Exception:
                            log.warning("忽略无法解析的别名推送消息")
                            continue
                        log.info(
                            "收到Yuri-YuzuChaN别名实时事件："
                            f"type={status.type}, items={len(status.status)}"
                        )
                        actions, self_id = context()
                        if actions is not None and self_id is not None:
                            try:
                                await push_alias(
                                    status, actions, self_id=self_id
                                )
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                log.exception(
                                    "处理单条别名推送失败，保持当前连接"
                                )
            if disconnect_error is not None:
                log.warning(
                    "别名推送连接断开: "
                    + _safe_exception_text(disconnect_error)
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = _safe_exception_text(exc)
            if _is_connection_error(exc):
                log.warning(f"别名推送连接断开: {detail}")
            else:
                log.error(f"别名推送连接失败: {detail}")
        if stop_event.is_set():
            return
        delay = reconnect_seconds
        log.info(f"别名推送将在 {delay:g} 秒后重连")
        if await _wait_or_stop(stop_event, delay):
            return
        reconnect_seconds = min(
            reconnect_seconds * 2,
            MAX_RECONNECT_SECONDS,
        )
