from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

from plugins.JianerAI.tools.contracts import (
    ToolContext,
    ToolExecutionError,
    ToolRisk,
    ToolSpec,
)


_LOGGER = logging.getLogger(__name__)

_DDGS_ENGINE_HINTS = (
    "bing",
    "brave",
    "duckduckgo",
    "google",
    "grokipedia",
    "mojeek",
    "startpage",
    "wikipedia",
    "yahoo",
    "yandex",
)
_CHINA_BACKENDS = ("zhipu", "bocha", "bing_cn")
_DIRECT_BING_BACKEND = "bing_cn"
_DEEPSEEK_BACKEND = "deepseek"
_ANYSEARCH_BACKEND = "anysearch"
_KEYED_DIRECT_BACKENDS = {
    "zhipu": "ZHIPUAI_API_KEY",
    "bocha": "BOCHA_API_KEY",
}
# Search providers selected by the host, not by the model.
_DDGS_PROVIDER = "ddgs"
_DEEPSEEK_PROVIDER = _DEEPSEEK_BACKEND
_SEARCH_PROVIDERS = (_DDGS_PROVIDER, _DEEPSEEK_PROVIDER, _ANYSEARCH_BACKEND)
_DEFAULT_DDGS_BACKEND = "bing"
_DEFAULT_FALLBACK_BACKENDS = _CHINA_BACKENDS

_DDGS_TIMEOUT_SECONDS = 8
_DEEPSEEK_TIMEOUT_SECONDS = 10.0
_ANYSEARCH_TIMEOUT_SECONDS = 15.0
_FALLBACK_ATTEMPT_TIMEOUT_SECONDS = 6.0
_FALLBACK_TOTAL_TIMEOUT_SECONDS = 12.0
_TOOL_TIMEOUT_SECONDS = 22.0
_DEFAULT_MAX_RESULTS = 5
_MAX_RESULTS = 8
_MAX_TITLE_CHARS = 300
_MAX_SNIPPET_CHARS = 1200
_MAX_URL_CHARS = 2048
_DEEPSEEK_MAX_QUERY_CHARS = 128
_ZHIPU_MAX_QUERY_CHARS = 70
_TIMELIMIT_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}

_DEEPSEEK_SEARCH_URL = "https://api.deepseek.com/v1/tools/search"
_ANYSEARCH_SEARCH_URL = "https://api.anysearch.com/v1/search"
_ZHIPU_SEARCH_URL = "https://open.bigmodel.cn/api/paas/v4/web_search"
_BOCHA_SEARCH_URL = "https://api.bochaai.com/v1/web-search"
_BING_CN_SEARCH_URLS = (
    "https://cn.bing.com/search",
    "https://www.bing.com/search",
)
_BING_CN_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@lru_cache(maxsize=1)
def _ddgs_text_engines() -> frozenset[str]:
    """Text engines actually enabled by the installed ``ddgs`` build."""

    try:
        from ddgs.engines import ENGINES
    except Exception:
        return frozenset(_DDGS_ENGINE_HINTS)
    engines = ENGINES.get("text") if isinstance(ENGINES, Mapping) else None
    if not isinstance(engines, Mapping) or not engines:
        return frozenset(_DDGS_ENGINE_HINTS)
    return frozenset(
        str(name).strip().casefold() for name in engines if str(name).strip()
    )


def _backend_options() -> tuple[str, ...]:
    options: list[str] = ["auto"]
    for name in sorted(_ddgs_text_engines()):
        if name not in options:
            options.append(name)
    for name in ("bing", *_CHINA_BACKENDS):
        if name not in options:
            options.append(name)
    return tuple(options)


def _search_provider(
    project_root: Path | None,
    environ: Mapping[str, str] | None,
) -> str:
    provider = (
        _config_value("WEB_SEARCH_PROVIDER", project_root, environ)
        or _DDGS_PROVIDER
    ).casefold()
    if provider not in _SEARCH_PROVIDERS:
        raise ValueError(
            "WEB_SEARCH_PROVIDER is not supported; available: "
            + ", ".join(_SEARCH_PROVIDERS)
        )
    return provider


class _BackendUnavailable(RuntimeError):
    """A configured backend cannot run, for example because its key is absent."""

    def __init__(self, backend: str) -> None:
        super().__init__(backend)
        self.backend = backend


@dataclass(frozen=True, slots=True)
class _SearchOutcome:
    backend: str
    results: list[dict[str, str]]


def web_search_tool(
    project_root: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> ToolSpec:
    root = Path(project_root).resolve() if project_root is not None else None

    def handler(
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        return _web_search(
            context,
            arguments,
            project_root=root,
            environ=environ,
        )

    return ToolSpec(
        name="web_search",
        description=(
            "搜索互联网上的当前公开信息，返回标题、URL 和搜索摘要。"
            "宿主可配置使用 DDGS 聚合搜索、DeepSeek 官方搜索 API 或 AnySearch；"
            "使用 DDGS 时可用 backend 指定搜索引擎，省略则使用宿主默认后端。"
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
                "backend": {
                    "type": "string",
                    "description": (
                        "DDGS 搜索引擎后端；省略时使用宿主默认后端。"
                        "宿主改用 DeepSeek 网页搜索 API 时该参数无效果。"
                    ),
                    "enum": list(_backend_options()),
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=handler,
        risk=ToolRisk.READ_ONLY,
        timeout_seconds=_TOOL_TIMEOUT_SECONDS,
        max_output_chars=12000,
    )


def _web_search(
    context: ToolContext,
    arguments: Mapping[str, Any],
    *,
    project_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
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

    del context
    provider = _search_provider(project_root, environ)
    if provider == _DEEPSEEK_PROVIDER:
        return _deepseek_provider_search(
            query,
            max_results=max_results,
            timelimit=timelimit,
            project_root=project_root,
            environ=environ,
        )
    if provider == _ANYSEARCH_BACKEND:
        try:
            results = _anysearch_search(
                query,
                max_results=max_results,
                project_root=project_root,
                environ=environ,
                timeout=_ANYSEARCH_TIMEOUT_SECONDS,
            )
        except ToolExecutionError:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "AnySearch web search failed: %s", type(exc).__name__
            )
            raise _unavailable_error() from exc
        return _payload(query, _ANYSEARCH_BACKEND, results)

    backend = _resolve_backend(arguments.get("backend"), project_root, environ)
    if backend == "bing" and "bing" not in _ddgs_text_engines():
        _LOGGER.info(
            "ddgs has no text engine named 'bing'; using the built-in "
            "Bing client instead"
        )
        backend = _DIRECT_BING_BACKEND
    if backend in _CHINA_BACKENDS:
        outcome = _search_explicit_direct_backend(
            backend,
            query,
            max_results=max_results,
            timelimit=timelimit,
            project_root=project_root,
            environ=environ,
        )
        if outcome is not None:
            return _payload(query, outcome.backend, outcome.results)
        fallback = _run_fallback_chain(
            query,
            max_results=max_results,
            timelimit=timelimit,
            project_root=project_root,
            environ=environ,
        )
        if fallback is None:
            raise _unavailable_error()
        return _payload(query, fallback.backend, fallback.results)

    try:
        results = _ddgs_search(
            query,
            backend=backend,
            max_results=max_results,
            timelimit=timelimit,
        )
    except Exception as exc:
        if _is_ddgs_empty_result(exc):
            return _payload(query, backend, [])
        if not _is_ddgs_fallback_error(exc):
            raise _unavailable_error() from exc
        _LOGGER.warning(
            "web_search DDGS backend %s failed (%s); trying fallback chain",
            backend,
            type(exc).__name__,
        )
        fallback = _run_fallback_chain(
            query,
            max_results=max_results,
            timelimit=timelimit,
            project_root=project_root,
            environ=environ,
        )
        if fallback is None:
            raise _unavailable_error() from exc
        return _payload(query, fallback.backend, fallback.results)
    return _payload(query, backend, results)


def _deepseek_provider_search(
    query: str,
    *,
    max_results: int,
    timelimit: str | None,
    project_root: Path | None,
    environ: Mapping[str, str] | None,
) -> dict[str, Any]:
    api_key = _config_value("DEEPSEEK_API_KEY", project_root, environ)
    if not api_key:
        raise ToolExecutionError(
            "web_search_backend_not_configured",
            "网页搜索已配置为 DeepSeek 网页搜索 API，但未配置 DEEPSEEK_API_KEY。",
        )
    try:
        results = _deepseek_search(
            query,
            max_results=max_results,
            api_key=api_key,
            timelimit=timelimit,
        )
    except Exception as exc:
        _LOGGER.warning("DeepSeek web search failed: %s", type(exc).__name__)
        raise _unavailable_error() from exc
    return _payload(query, _DEEPSEEK_BACKEND, results)


def _deepseek_search(
    query: str,
    *,
    max_results: int,
    api_key: str,
    timelimit: str | None = None,
    timeout: float = _DEEPSEEK_TIMEOUT_SECONDS,
) -> list[dict[str, str]]:
    body = {
        "query": _deepseek_query(query, timelimit),
        "count": max(1, min(max_results, 50)),
        "need_content": False,
    }
    with _http_client(timeout=timeout) as client:
        response = client.post(
            _DEEPSEEK_SEARCH_URL,
            headers=_bearer_headers(api_key),
            json=body,
        )
        response.raise_for_status()
        payload = response.json()
    items = payload.get("results") if isinstance(payload, Mapping) else None
    return _normalize_results(
        _map_items(
            items,
            title_keys=("title",),
            url_keys=("url",),
            snippet_keys=("summary", "snippet"),
        ),
        limit=max_results,
    )


def _deepseek_query(query: str, timelimit: str | None) -> str:
    """Map the DDGS-style time limit onto the ``after:`` search operator."""

    if not timelimit:
        return _truncate(query, _DEEPSEEK_MAX_QUERY_CHARS)
    days = _TIMELIMIT_DAYS.get(str(timelimit).casefold())
    if days is None:
        return _truncate(query, _DEEPSEEK_MAX_QUERY_CHARS)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    suffix = f" after:{since}"
    room = _DEEPSEEK_MAX_QUERY_CHARS - len(suffix)
    return f"{query[:room]}{suffix}"


def _anysearch_search(
    query: str,
    *,
    max_results: int,
    project_root: Path | None,
    environ: Mapping[str, str] | None,
    timeout: float,
) -> list[dict[str, str]]:
    body: dict[str, Any] = {
        "query": query,
        "max_results": max(1, min(max_results, 10)),
        "format": "json",
        "zone": _config_value("ANYSEARCH_ZONE", project_root, environ) or "cn",
        "language": (
            _config_value("ANYSEARCH_LANGUAGE", project_root, environ)
            or "zh-CN"
        ),
    }
    tag = _config_value("ANYSEARCH_TAG", project_root, environ)
    if tag:
        body["tag"] = tag
    params = _anysearch_params(project_root, environ)
    if params:
        body["params"] = params
    headers = {"Content-Type": "application/json"}
    api_key = _config_value("ANYSEARCH_API_KEY", project_root, environ)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    with _http_client(timeout=timeout) as client:
        response = client.post(_ANYSEARCH_SEARCH_URL, headers=headers, json=body)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, Mapping) or str(payload.get("code")) != "0":
        # Never surface the body: HTTP 402 responses may contain credentials.
        raise RuntimeError("anysearch returned an error envelope")
    items = _mapping_value(payload, "data", "results")
    return _normalize_results(
        _map_items(
            items,
            title_keys=("title",),
            url_keys=("url",),
            snippet_keys=("snippet", "content"),
        ),
        limit=max_results,
    )


def _anysearch_params(
    project_root: Path | None,
    environ: Mapping[str, str] | None,
) -> dict[str, Any]:
    raw = _config_value("ANYSEARCH_PARAMS", project_root, environ)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolExecutionError(
            "web_search_backend_not_configured",
            "ANYSEARCH_PARAMS 必须是 JSON 对象。",
        ) from exc
    if not isinstance(value, Mapping):
        raise ToolExecutionError(
            "web_search_backend_not_configured",
            "ANYSEARCH_PARAMS 必须是 JSON 对象。",
        )
    return dict(value)


def _search_explicit_direct_backend(
    backend: str,
    query: str,
    *,
    max_results: int,
    timelimit: str | None,
    project_root: Path | None,
    environ: Mapping[str, str] | None,
) -> _SearchOutcome | None:
    try:
        results = _search_direct_backend(
            backend,
            query,
            max_results=max_results,
            timelimit=timelimit,
            project_root=project_root,
            environ=environ,
            timeout=_FALLBACK_ATTEMPT_TIMEOUT_SECONDS,
        )
    except _BackendUnavailable as exc:
        raise _not_configured_error(exc) from exc
    except Exception as exc:
        _LOGGER.warning(
            "web_search backend %s failed: %s",
            backend,
            type(exc).__name__,
        )
        return None
    return _SearchOutcome(backend, results)


def _run_fallback_chain(
    query: str,
    *,
    max_results: int,
    timelimit: str | None,
    project_root: Path | None,
    environ: Mapping[str, str] | None,
) -> _SearchOutcome | None:
    deadline = time.monotonic() + _FALLBACK_TOTAL_TIMEOUT_SECONDS
    empty_outcome: _SearchOutcome | None = None
    for backend in _fallback_backends(project_root, environ):
        remaining = deadline - time.monotonic()
        if remaining <= 1.0:
            break
        try:
            results = _search_direct_backend(
                backend,
                query,
                max_results=max_results,
                timelimit=timelimit,
                project_root=project_root,
                environ=environ,
                timeout=min(_FALLBACK_ATTEMPT_TIMEOUT_SECONDS, remaining),
            )
        except _BackendUnavailable:
            continue
        except Exception as exc:
            _LOGGER.warning(
                "web_search fallback backend %s failed: %s",
                backend,
                type(exc).__name__,
            )
            continue
        if results:
            return _SearchOutcome(backend, results)
        if empty_outcome is None:
            empty_outcome = _SearchOutcome(backend, results)
    return empty_outcome


def _search_direct_backend(
    backend: str,
    query: str,
    *,
    max_results: int,
    timelimit: str | None,
    project_root: Path | None,
    environ: Mapping[str, str] | None,
    timeout: float,
) -> list[dict[str, str]]:
    if backend == "bing_cn":
        return _bing_cn_search(query, max_results=max_results, timeout=timeout)
    if backend == _ANYSEARCH_BACKEND:
        return _anysearch_search(
            query,
            max_results=max_results,
            project_root=project_root,
            environ=environ,
            timeout=timeout,
        )
    api_key = _backend_api_key(backend, project_root, environ)
    if not api_key:
        raise _BackendUnavailable(backend)
    if backend == _DEEPSEEK_BACKEND:
        return _deepseek_search(
            query,
            max_results=max_results,
            api_key=api_key,
            timelimit=timelimit,
            timeout=timeout,
        )
    if backend == "zhipu":
        return _zhipu_search(
            query,
            max_results=max_results,
            api_key=api_key,
            timeout=timeout,
        )
    if backend == "bocha":
        return _bocha_search(
            query,
            max_results=max_results,
            api_key=api_key,
            timeout=timeout,
        )
    raise _BackendUnavailable(backend)


def _zhipu_search(
    query: str,
    *,
    max_results: int,
    api_key: str,
    timeout: float,
) -> list[dict[str, str]]:
    body = {
        "search_engine": "search_std",
        "search_query": _truncate(query, _ZHIPU_MAX_QUERY_CHARS),
        "count": max_results,
    }
    with _http_client(timeout=timeout) as client:
        response = client.post(
            _ZHIPU_SEARCH_URL,
            headers=_bearer_headers(api_key),
            json=body,
        )
        response.raise_for_status()
        payload = response.json()
    items = payload.get("search_result") if isinstance(payload, Mapping) else None
    return _normalize_results(
        _map_items(
            items,
            title_keys=("title",),
            url_keys=("link", "url"),
            snippet_keys=("content", "snippet", "summary"),
        ),
        limit=max_results,
    )


def _bocha_search(
    query: str,
    *,
    max_results: int,
    api_key: str,
    timeout: float,
) -> list[dict[str, str]]:
    body = {
        "query": query,
        "count": max_results,
        "summary": True,
        "page": 1,
    }
    with _http_client(timeout=timeout) as client:
        response = client.post(
            _BOCHA_SEARCH_URL,
            headers=_bearer_headers(api_key),
            json=body,
        )
        response.raise_for_status()
        payload = response.json()
    items = _mapping_value(payload, "data", "webPages", "value")
    return _normalize_results(
        _map_items(
            items,
            title_keys=("name", "title"),
            url_keys=("url",),
            snippet_keys=("summary", "snippet"),
        ),
        limit=max_results,
    )


def _bing_cn_search(
    query: str,
    *,
    max_results: int,
    timeout: float,
) -> list[dict[str, str]]:
    params = {
        "q": query,
        "count": str(max_results),
        "mkt": "zh-CN",
        "setlang": "zh-hans",
    }
    headers = {
        "User-Agent": _BING_CN_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": "SRCHHPGUSR=SRCHLANG=zh-Hans",
    }
    last_error: Exception | None = None
    succeeded = False
    with _http_client(timeout=max(2.0, timeout / 2)) as client:
        for url in _BING_CN_SEARCH_URLS:
            try:
                response = client.get(url, params=params, headers=headers)
                response.raise_for_status()
            except Exception as exc:
                last_error = exc
                continue
            succeeded = True
            parser = _BingCnParser()
            parser.feed(response.text)
            results = _normalize_results(parser.results, limit=max_results)
            if results:
                return results
    if not succeeded and last_error is not None:
        raise last_error
    return []


def _ddgs_search(
    query: str,
    *,
    backend: str,
    max_results: int,
    timelimit: str | None,
) -> list[dict[str, str]]:
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
    return _normalize_results(raw_results or (), limit=max_results)


def _resolve_backend(
    value: Any,
    project_root: Path | None,
    environ: Mapping[str, str] | None,
) -> str:
    backend = str(value or "").strip().casefold()
    if not backend:
        backend = (
            _config_value("DDGS_BACKEND", project_root, environ)
            or _DEFAULT_DDGS_BACKEND
        ).casefold()
    if (
        backend == "auto"
        or backend == "bing"
        or backend == "google"
        or backend in _CHINA_BACKENDS
        or backend in _ddgs_text_engines()
    ):
        return backend
    raise ValueError(
        "unsupported search backend; available: "
        + ", ".join(_backend_options())
    )


def _fallback_backends(
    project_root: Path | None,
    environ: Mapping[str, str] | None,
) -> tuple[str, ...]:
    raw = _config_value("WEB_SEARCH_FALLBACK_BACKENDS", project_root, environ)
    if not raw:
        return _DEFAULT_FALLBACK_BACKENDS
    backends: list[str] = []
    for item in raw.split(","):
        backend = item.strip().casefold()
        if not backend or backend in backends:
            continue
        if (
            backend != _DEEPSEEK_BACKEND
            and backend != _ANYSEARCH_BACKEND
            and backend not in _CHINA_BACKENDS
        ):
            raise ValueError(
                "WEB_SEARCH_FALLBACK_BACKENDS contains an unsupported backend"
            )
        backends.append(backend)
    return tuple(backends) or _DEFAULT_FALLBACK_BACKENDS


def _backend_api_key(
    backend: str,
    project_root: Path | None,
    environ: Mapping[str, str] | None,
) -> str:
    variable = (
        "DEEPSEEK_API_KEY"
        if backend == _DEEPSEEK_BACKEND
        else _KEYED_DIRECT_BACKENDS.get(backend)
    )
    if variable is None:
        return ""
    return _config_value(variable, project_root, environ)


def _not_configured_error(exc: _BackendUnavailable) -> ToolExecutionError:
    variable = _KEYED_DIRECT_BACKENDS.get(exc.backend, "")
    detail = f"未配置 {variable}" if variable else "无法使用"
    return ToolExecutionError(
        "web_search_backend_not_configured",
        f"搜索后端 {exc.backend} {detail}，无法使用该后端。",
    )


def _config_value(
    name: str,
    project_root: Path | None,
    environ: Mapping[str, str] | None,
) -> str:
    if environ is not None:
        return str(environ.get(name) or "").strip()
    value = os.environ.get(name)
    if value is not None:
        return str(value).strip()
    if project_root is None:
        return ""
    try:
        file_values = dotenv_values(project_root / ".env")
    except OSError:
        return ""
    return str(file_values.get(name) or "").strip()


def _bearer_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _http_client(*, timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True)


def _load_ddgs_class() -> type[Any]:
    try:
        from ddgs import DDGS
    except ImportError as exc:
        raise RuntimeError("DDGS search dependency is unavailable") from exc
    return DDGS


@lru_cache(maxsize=1)
def _ddgs_exception_types() -> tuple[type[BaseException], ...]:
    try:
        from ddgs.exceptions import (
            DDGSException,
            RatelimitException,
            TimeoutException,
        )
    except ImportError:
        return ()
    return (DDGSException, RatelimitException, TimeoutException)


def _is_ddgs_empty_result(exc: BaseException) -> bool:
    types = _ddgs_exception_types()
    if len(types) != 3:
        return False
    ddgs_exception, ratelimit_exception, timeout_exception = types
    if isinstance(exc, (ratelimit_exception, timeout_exception)):
        return False
    if not isinstance(exc, ddgs_exception):
        return False
    return "no result" in str(exc).casefold()


def _is_ddgs_fallback_error(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TransportError, OSError, TimeoutError)):
        return True
    types = _ddgs_exception_types()
    if not types:
        return False
    ddgs_exception, ratelimit_exception, timeout_exception = types
    if isinstance(exc, (ratelimit_exception, timeout_exception)):
        return True
    if isinstance(exc, ddgs_exception):
        return "no result" not in str(exc).casefold()
    return False


def _unavailable_error() -> ToolExecutionError:
    return ToolExecutionError("web_search_unavailable", "网页搜索暂时不可用。")


def _payload(
    query: str,
    backend: str,
    results: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    normalized = [dict(item) for item in results]
    return {
        "query": query,
        "backend": backend,
        "result_count": len(normalized),
        "results": normalized,
    }


def _map_items(
    items: Any,
    *,
    title_keys: tuple[str, ...],
    url_keys: tuple[str, ...],
    snippet_keys: tuple[str, ...],
) -> list[dict[str, Any]]:
    if (
        items is None
        or isinstance(items, (str, bytes, bytearray))
        or not isinstance(items, Iterable)
    ):
        return []
    mapped: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        mapped.append(
            {
                "title": _first_value(item, title_keys),
                "url": _first_value(item, url_keys),
                "snippet": _first_value(item, snippet_keys),
            }
        )
    return mapped


def _first_value(item: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = item.get(key)
        if value:
            return value
    return ""


def _mapping_value(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _truncate(value: str, limit: int) -> str:
    return value[:limit] if len(value) > limit else value


class _BingCnParser(HTMLParser):
    """Extract title, URL, and snippet from ``cn.bing.com`` result cards."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._li_depth = 0
        self._in_title = False
        self._in_snippet = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        tag = tag.casefold()
        attributes = {
            str(key).casefold(): str(value or "")
            for key, value in attrs
        }
        if (
            tag == "li"
            and self._current is None
            and "b_algo" in attributes.get("class", "").split()
        ):
            self._current = {"title": "", "url": "", "snippet": ""}
            self._li_depth = 1
            return
        if self._current is None:
            return
        if tag == "li":
            self._li_depth += 1
        elif tag == "h2" and not self._current["title"]:
            self._in_title = True
        elif tag == "a" and not self._current["url"]:
            href = attributes.get("href", "").strip()
            if href.casefold().startswith(("http://", "https://")):
                self._current["url"] = href
        elif tag == "p" and not self._current["snippet"]:
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self._current is None:
            return
        if tag == "h2":
            self._in_title = False
        elif tag == "p":
            self._in_snippet = False
        elif tag == "li":
            self._li_depth -= 1
            if self._li_depth <= 0:
                self.results.append(self._current)
                self._current = None
                self._li_depth = 0
                self._in_title = False
                self._in_snippet = False

    def handle_data(self, data: str) -> None:
        if self._current is None:
            return
        text = " ".join(data.split())
        if not text:
            return
        if self._in_title:
            self._current["title"] = _append_text(
                self._current["title"], text
            )
        elif self._in_snippet:
            self._current["snippet"] = _append_text(
                self._current["snippet"], text
            )


def _append_text(current: str, addition: str) -> str:
    return f"{current} {addition}".strip()


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
