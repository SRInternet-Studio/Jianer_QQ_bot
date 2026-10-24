"""Persistent preview/read/search store for oversized tool results."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from plugins.JianerAI.tools.contracts import ToolSpec


class ContentStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def put(
        self,
        content: str,
        *,
        source: str = "",
        title: str = "",
        kind: str = "tool",
    ) -> dict[str, Any]:
        text = str(content or "")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        content_id = digest[:20]
        directory = self.root / content_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "content.txt").write_text(text, encoding="utf-8")
        metadata = {
            "content_id": content_id,
            "chars": len(text),
            "source": str(source),
            "title": str(title),
            "kind": str(kind),
            "created_at": int(time.time()),
        }
        temporary = directory / f"metadata.tmp-{os.getpid()}"
        temporary.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, directory / "metadata.json")
        return metadata

    def read(
        self,
        content_id: str,
        *,
        offset: int = 0,
        limit: int = 4000,
    ) -> dict[str, Any]:
        directory = self._directory(content_id)
        text = (directory / "content.txt").read_text(encoding="utf-8")
        start = max(0, int(offset))
        size = max(1, min(20000, int(limit)))
        return {
            "content_id": str(content_id),
            "offset": start,
            "chars": len(text),
            "content": text[start : start + size],
            "truncated": start + size < len(text),
        }

    def search(
        self,
        content_id: str,
        query: str,
        *,
        limit: int = 20,
    ) -> dict[str, Any]:
        directory = self._directory(content_id)
        text = (directory / "content.txt").read_text(encoding="utf-8")
        needle = str(query or "")
        if not needle:
            raise ValueError("query cannot be empty")
        matches: list[dict[str, Any]] = []
        cursor = 0
        while len(matches) < max(1, min(100, int(limit))):
            index = text.find(needle, cursor)
            if index < 0:
                break
            start = max(0, index - 120)
            end = min(len(text), index + len(needle) + 120)
            matches.append(
                {
                    "offset": index,
                    "preview": text[start:end],
                }
            )
            cursor = index + max(1, len(needle))
        return {
            "content_id": str(content_id),
            "query": needle,
            "count": len(matches),
            "matches": matches,
        }

    def preview(self, text: str, limit: int = 1200) -> str:
        normalized = str(text or "").strip()
        if len(normalized) <= limit:
            return normalized
        return normalized[: max(1, limit - 1)].rstrip() + "…"

    def _directory(self, content_id: str) -> Path:
        value = str(content_id or "").strip()
        if not value or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("invalid content_id")
        directory = self.root / value
        if not directory.is_dir():
            raise FileNotFoundError(f"unknown content_id: {value}")
        return directory


def content_tools(store: ContentStore) -> tuple[ToolSpec, ...]:
    async def read_content(context, arguments):
        del context
        return store.read(
            str(arguments.get("content_id") or ""),
            offset=max(0, int(arguments.get("offset", 0))),
            limit=max(1, int(arguments.get("limit", 4000))),
        )

    async def search_content(context, arguments):
        del context
        return store.search(
            str(arguments.get("content_id") or ""),
            str(arguments.get("query") or ""),
            limit=max(1, int(arguments.get("limit", 20))),
        )

    return (
        ToolSpec(
            name="content_read",
            description="分段读取已保存的大工具结果。",
            input_schema={
                "type": "object",
                "properties": {
                    "content_id": {"type": "string", "minLength": 1},
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20000,
                        "default": 4000,
                    },
                },
                "required": ["content_id"],
                "additionalProperties": False,
            },
            handler=read_content,
            group="content",
            system_visible=True,
        ),
        ToolSpec(
            name="content_search",
            description="在已保存的大工具结果中搜索文本。",
            input_schema={
                "type": "object",
                "properties": {
                    "content_id": {"type": "string", "minLength": 1},
                    "query": {"type": "string", "minLength": 1},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 20,
                    },
                },
                "required": ["content_id", "query"],
                "additionalProperties": False,
            },
            handler=search_content,
            group="content",
            system_visible=True,
        ),
    )
