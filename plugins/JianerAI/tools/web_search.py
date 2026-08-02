from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from plugins.JianerAI.tools.contracts import (
    ToolContext,
    ToolRisk,
    ToolSpec,
)


_ALLOWED_BACKENDS = frozenset(
    {
        "auto",
        "bing",
        "brave",
        "duckduckgo",
        "google",
        "grokipedia",
        "mojeek",
        "wikipedia",
        "yahoo",
        "yandex",
    }
)
_DDGS_TIMEOUT_SECONDS = 8
_TOOL_TIMEOUT_SECONDS = 12.0
_DEFAULT_MAX_RESULTS = 5
_MAX_RESULTS = 8
_MAX_TITLE_CHARS = 300
_MAX_SNIPPET_CHARS = 1200
_MAX_URL_CHARS = 2048


def web_search_tool() -> ToolSpec:
    return ToolSpec(
        name="web_search",
        description=(
            "搜索互联网上的当前公开信息，返回标题、URL 和搜索摘要。"
            "适合需要最新资料的问题，结果中的 URL 仅供回答依据；除非用户明确要求"
            "来源、出处、引用或链接，否则不要在最终回答中展示来源或 URL。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词或问题。",
                    "minLength": 1,
                    "maxLength": 500,
                },
                "max_results": {
                    "type": "integer",
                    "description": "返回结果数量，范围 1 到 8。",
                    "minimum": 1,
                    "maximum": _MAX_RESULTS,
                    "default": _DEFAULT_MAX_RESULTS,
                },
                "timelimit": {
                    "type": "string",
                    "description": "可选时间范围：d 天、w 周、m 月、y 年。",
                    "enum": ["d", "w", "m", "y"],
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=_web_search,
        risk=ToolRisk.READ_ONLY,
        timeout_seconds=_TOOL_TIMEOUT_SECONDS,
        max_output_chars=12000,
    )


def _web_search(
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    del context
    query = _clean_text(arguments["query"], limit=500)
    if not query:
        raise ValueError("search query cannot be blank")
    max_results = int(arguments.get("max_results", _DEFAULT_MAX_RESULTS))
    timelimit_value = arguments.get("timelimit")
    timelimit = (
        str(timelimit_value).strip().casefold()
        if timelimit_value is not None
        else None
    )
    backend = _configured_backend()
    ddgs_class = _load_ddgs_class()
    with ddgs_class(timeout=_DDGS_TIMEOUT_SECONDS) as client:
        raw_results = client.text(
            query,
            region="cn-zh",
            safesearch="moderate",
            timelimit=timelimit,
            max_results=max_results,
            page=1,
            backend=backend,
        )
        results = _normalize_results(raw_results or (), limit=max_results)
    return {
        "query": query,
        "backend": backend,
        "result_count": len(results),
        "results": results,
    }


def _configured_backend() -> str:
    backend = str(os.environ.get("DDGS_BACKEND") or "auto").strip().casefold()
    if backend not in _ALLOWED_BACKENDS:
        raise ValueError("DDGS_BACKEND is not an allowed search backend")
    return backend


def _load_ddgs_class() -> type[Any]:
    try:
        from ddgs import DDGS
    except ImportError as exc:
        raise RuntimeError("DDGS search dependency is unavailable") from exc
    return DDGS


def _normalize_results(
    raw_results: Iterable[Any],
    *,
    limit: int,
) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in raw_results:
        if not isinstance(item, Mapping):
            continue
        url = _clean_url(item.get("href") or item.get("url"))
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        output.append(
            {
                "title": _clean_text(item.get("title"), limit=_MAX_TITLE_CHARS),
                "url": url,
                "snippet": _clean_text(
                    item.get("body") or item.get("snippet"),
                    limit=_MAX_SNIPPET_CHARS,
                ),
            }
        )
        if len(output) >= limit:
            break
    return output


def _clean_url(value: Any) -> str:
    url = str(value or "").strip()
    if (
        not url
        or len(url) > _MAX_URL_CHARS
        or any(char.isspace() for char in url)
    ):
        return ""
    parsed = urlsplit(url)
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    return url


def _clean_text(value: Any, *, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]
