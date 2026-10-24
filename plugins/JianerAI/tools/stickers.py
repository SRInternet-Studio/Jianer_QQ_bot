"""HypeR-compatible model tools for the global sticker library."""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import aiohttp
from jianer import common as Manager
from jianer import segments as Segments

from plugins.JianerAI.sticker_store import StickerEntry, StickerStore
from plugins.JianerAI.tools.contracts import (
    ToolContext,
    ToolExecutionError,
    ToolRisk,
    ToolSpec,
)


StickerDescriber = Callable[[bytes, str], Awaitable[str]]
_LAST_SENT: dict[str, float] = {}
_MAX_IMAGE_BYTES = 12 * 1024 * 1024


def sticker_tools(
    store: StickerStore,
    describe: StickerDescriber,
) -> tuple[ToolSpec, ...]:
    async def add(context: ToolContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
        raw = await _image_bytes(context, str(arguments.get("url") or ""))
        if raw is None:
            raise ToolExecutionError(
                "sticker_image_missing",
                "没有找到可收藏的图片；请带图、回复图片或提供图片 URL。",
            )
        keywords = str(arguments.get("keywords") or "").strip()
        description = await _safe_describe(describe, raw, keywords)
        result = await asyncio.to_thread(
            store.add,
            raw,
            description or keywords or "(未生成描述)",
            keywords,
            str(context.principal_id or context.canonical_user_id or ""),
        )
        if result.entry is None:
            return {
                "ok": False,
                "status": result.status,
                "reason": result.reason,
            }
        return {
            "ok": result.status in {"added", "duplicate"},
            "status": result.status,
            **result.entry.public(),
        }

    async def delete(
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        del context
        sticker_id = int(arguments.get("sticker_id") or 0)
        removed = await asyncio.to_thread(store.delete, sticker_id)
        return {
            "ok": removed is not None,
            "sticker_id": sticker_id,
            "deleted": removed is not None,
        }

    async def search(
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        del context
        query = str(arguments.get("query") or "").strip()
        top_k = max(1, min(10, int(arguments.get("top_k", 5))))
        found = await asyncio.to_thread(store.search, query, top_k)
        return {
            "items": [
                entry.public(score=score)
                for entry, score in found
            ],
            "total": store.count(),
        }

    async def send(
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        sticker_id = int(arguments.get("sticker_id") or 0)
        entry = store.get(sticker_id)
        if entry is None:
            raise ToolExecutionError(
                "sticker_not_found",
                f"表情包 #{sticker_id} 不存在，请先检索。",
            )
        scene_key = f"{context.ev_type}:{context.scene_id}"
        interval = max(0.0, store.options.send_interval_seconds)
        remaining = max(
            0.0,
            interval - (time.time() - _LAST_SENT.get(scene_key, 0.0)),
        )
        if remaining > 0:
            raise ToolExecutionError(
                "sticker_rate_limited",
                f"表情包发送太频繁，约 {int(remaining) + 1} 秒后再试。",
            )
        result = await _send_entry(context, entry)
        _LAST_SENT[scene_key] = time.time()
        await asyncio.to_thread(store.mark_used, sticker_id)
        return {"sent": True, "sticker_id": sticker_id, "result": result}

    async def list_recent(
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        del context
        limit = max(1, min(30, int(arguments.get("limit", 10))))
        entries = await asyncio.to_thread(store.list_recent, limit)
        return {
            "items": [entry.public() for entry in entries],
            "total": store.count(),
        }

    return (
        ToolSpec(
            name="sticker_add",
            description=(
                "收藏一张图片到全局表情包库。可传图片 URL；省略 URL 时使用"
                "当前消息或引用消息中的第一张图片。动画 GIF/WebP 会原样保存，"
                "不会压成静态首帧。keywords 用于辅助检索。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "maxLength": 2048},
                    "keywords": {"type": "string", "maxLength": 500},
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=add,
            risk=ToolRisk.MUTATING,
            scenes=frozenset({"group", "private"}),
            group="sticker",
            timeout_seconds=90.0,
            max_output_chars=6000,
        ),
        ToolSpec(
            name="sticker_delete",
            description="按 id 删除全局表情包库中的图片。",
            input_schema={
                "type": "object",
                "properties": {
                    "sticker_id": {"type": "integer", "minimum": 1}
                },
                "required": ["sticker_id"],
                "additionalProperties": False,
            },
            handler=delete,
            risk=ToolRisk.MUTATING,
            scenes=frozenset({"group", "private"}),
            group="sticker",
        ),
        ToolSpec(
            name="sticker_search",
            description=(
                "按画面、情绪或用途检索全局表情包库，返回 id、描述和关键词。"
                "没有合适候选时不要硬发。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 500},
                    "top_k": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 5,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=search,
            scenes=frozenset({"group", "private"}),
            group="sticker",
        ),
        ToolSpec(
            name="sticker_send",
            description=(
                "把检索到的表情包发送到当前群聊或私聊。发送后不要解释工具过程。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "sticker_id": {"type": "integer", "minimum": 1}
                },
                "required": ["sticker_id"],
                "additionalProperties": False,
            },
            handler=send,
            risk=ToolRisk.PRESENTATION,
            scenes=frozenset({"group", "private"}),
            group="sticker",
            timeout_seconds=30.0,
        ),
        ToolSpec(
            name="sticker_list",
            description="按收藏时间倒序列出全局表情包库条目。",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 30,
                        "default": 10,
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=list_recent,
            scenes=frozenset({"group", "private"}),
            group="sticker",
        ),
    )


async def _safe_describe(
    describe: StickerDescriber,
    raw: bytes,
    hint: str,
) -> str:
    try:
        return str(await describe(raw, hint) or "").strip()
    except Exception:
        return ""


async def _send_entry(
    context: ToolContext,
    entry: StickerEntry,
) -> dict[str, Any]:
    path = Path(entry.file).resolve()
    if not path.is_file():
        raise ToolExecutionError(
            "sticker_file_missing",
            f"表情包 #{entry.id} 的图片文件缺失。",
        )
    image = Segments.Image(f"file://{path}")
    image.summary = "[表情包]"
    setattr(image, "is_emoji", True)
    target = (
        {"group_id": str(context.conversation.conversation_id)}
        if context.conversation.kind.value == "group"
        else {"user_id": str(context.conversation.conversation_id)}
    )
    result = await context.actions.send(
        message=Manager.Message(image),
        **target,
    )
    data = getattr(result, "data", None)
    return {
        "message_id": str(getattr(data, "message_id", "") or ""),
    }


async def _image_bytes(
    context: ToolContext,
    source: str,
) -> bytes | None:
    value = str(source or "").strip()
    if value:
        return await _download_image(value)
    for attachment in getattr(context, "attachments", ()) or ():
        data = getattr(attachment, "data", None)
        mime = str(getattr(attachment, "mime", "") or "")
        if isinstance(data, bytes) and mime.startswith("image/"):
            return data
    for segment in getattr(context.event, "message", ()) or ():
        if not isinstance(segment, Segments.Image):
            continue
        candidate = str(
            getattr(segment, "url", None)
            or getattr(segment, "file", None)
            or ""
        ).strip()
        if candidate:
            return await _download_image(candidate)
    return None


async def _download_image(source: str) -> bytes | None:
    if source.startswith("base64:"):
        return base64.b64decode(source[len("base64:") :], validate=False)
    if source.startswith("file://"):
        return await asyncio.to_thread(
            Path(source[len("file://") :]).read_bytes
        )
    if Path(source).is_file():
        return await asyncio.to_thread(Path(source).read_bytes)
    if not source.startswith(("http://", "https://")):
        return None
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(source) as response:
            if response.status != 200:
                return None
            data = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                data.extend(chunk)
                if len(data) > _MAX_IMAGE_BYTES:
                    raise ToolExecutionError(
                        "sticker_image_too_large",
                        "图片超过 12 MiB，无法收藏。",
                    )
            return bytes(data)
