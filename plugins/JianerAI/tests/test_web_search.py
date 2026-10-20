from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from jianer.adapters import ConversationKey, ConversationKind

from plugins.JianerAI.tools import web_search as web_search_module
from plugins.JianerAI.tools.contracts import ToolContext, ToolExecutionError
from plugins.JianerAI.tools.web_search import web_search_tool


class _FakeProviders:
    def __init__(self, config: Any | None) -> None:
        self._config = config

    def get(self, key: str) -> Any:
        if self._config is None:
            raise KeyError(key)
        return self._config


def _context(*, model: str = "", config: Any | None = None) -> ToolContext:
    return ToolContext(
        event=SimpleNamespace(),
        actions=SimpleNamespace(capabilities=frozenset()),
        conversation=ConversationKey(
            protocol="milky",
            self_id="bot-1",
            kind=ConversationKind.GROUP,
            conversation_id="group-1",
            preset="default",
        ),
        canonical_user_id="person-1",
        runtime={},
        memory=None,
        agent=SimpleNamespace(providers=_FakeProviders(config)),
        model=model,
    )


class _FakeResponse:
    def __init__(
        self,
        payload: Any | None = None,
        *,
        text: str = "",
        status_code: int = 200,
    ) -> None:
        self._payload = {} if payload is None else payload
        self._text = text
        self.status_code = status_code
        self.request = httpx.Request("GET", "https://example.test/")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "request failed",
                request=self.request,
                response=httpx.Response(
                    self.status_code,
                    request=self.request,
                ),
            )

    def json(self) -> Any:
        return self._payload

    @property
    def text(self) -> str:
        return self._text


class _FakeHttpClient:
    def __init__(self, post: Any = None, get: Any = None) -> None:
        self._post = post
        self._get = get
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.gets: list[tuple[str, dict[str, Any]]] = []

    def __enter__(self) -> "_FakeHttpClient":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.posts.append((url, kwargs))
        if self._post is None:
            raise AssertionError(f"unexpected POST {url}")
        return self._post(url, kwargs)

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.gets.append((url, kwargs))
        if self._get is None:
            raise AssertionError(f"unexpected GET {url}")
        return self._get(url, kwargs)


def _install_http(
    monkeypatch: pytest.MonkeyPatch,
    *,
    post: Any = None,
    get: Any = None,
) -> _FakeHttpClient:
    client = _FakeHttpClient(post=post, get=get)
    monkeypatch.setattr(
        web_search_module,
        "_http_client",
        lambda *, timeout: client,
    )
    return client


def _install_ddgs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    results: list[dict[str, Any]] | None = None,
    error: BaseException | None = None,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    class _Client:
        def __init__(self, timeout: int | None = None) -> None:
            self.timeout = timeout

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *args: Any) -> bool:
            return False

        def text(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            calls.append({"query": query, **kwargs})
            if error is not None:
                raise error
            return [] if results is None else results

    monkeypatch.setattr(
        web_search_module,
        "_load_ddgs_class",
        lambda: _Client,
    )
    return calls


def _first_result(title: str = "标题", url: str = "https://a.example") -> dict[str, str]:
    return {"title": title, "href": url, "body": "摘要"}


def test_tool_exposes_backend_enum() -> None:
    spec = web_search_tool()
    backend = spec.input_schema["properties"]["backend"]
    expected = set(web_search_module._backend_options())
    assert set(backend["enum"]) == expected
    assert "bing" in backend["enum"]
    assert {"zhipu", "bocha", "bing_cn"} <= set(backend["enum"])


def test_backend_argument_overrides_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_ddgs(monkeypatch, results=[_first_result()])
    _install_http(monkeypatch)
    spec = web_search_tool(environ={"DDGS_BACKEND": "bing"})
    result = spec.handler(
        _context(),
        {"query": "hello", "backend": "google"},
    )
    assert calls[0]["backend"] == "google"
    assert result["backend"] == "google"
    assert result["results"][0]["url"] == "https://a.example"


def test_environment_backend_is_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_ddgs(monkeypatch, results=[_first_result()])
    _install_http(monkeypatch)
    spec = web_search_tool(environ={"DDGS_BACKEND": "brave"})
    result = spec.handler(_context(), {"query": "hello"})
    assert calls[0]["backend"] == "brave"
    assert result["backend"] == "brave"


def test_builtin_default_backend_is_bing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        web_search_module,
        "_ddgs_text_engines",
        lambda: frozenset({"brave", "duckduckgo", "google"}),
    )
    ddgs_calls = _install_ddgs(monkeypatch, results=[_first_result()])
    html_text = (
        "<li class='b_algo'><h2>"
        "<a href='https://bing.example/result'>Bing 结果</a>"
        "</h2><p>Bing 摘要</p></li>"
    )

    def get(url: str, kwargs: dict[str, Any]) -> _FakeResponse:
        assert url == "https://cn.bing.com/search"
        return _FakeResponse(text=html_text)

    _install_http(monkeypatch, get=get)
    spec = web_search_tool(environ={})
    result = spec.handler(_context(), {"query": "hello"})
    assert ddgs_calls == []
    assert result["backend"] == "bing_cn"
    assert result["results"][0]["url"] == "https://bing.example/result"


def test_invalid_backend_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_ddgs(monkeypatch, results=[])
    _install_http(monkeypatch)
    spec = web_search_tool(environ={})
    with pytest.raises(ValueError):
        spec.handler(_context(), {"query": "hello", "backend": "baidu"})
    with pytest.raises(ValueError):
        web_search_tool(environ={"DDGS_BACKEND": "baidu"}).handler(
            _context(),
            {"query": "hello"},
        )


def _deepseek_response() -> _FakeResponse:
    return _FakeResponse(
        {
            "results": [
                {
                    "title": "DeepSeek 结果",
                    "url": "https://deepseek.example/result",
                    "snippet": "短摘要",
                    "summary": "长摘要",
                }
            ]
        }
    )


def test_provider_deepseek_uses_official_search_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddgs_calls = _install_ddgs(monkeypatch, results=[_first_result()])
    seen: list[dict[str, Any]] = []

    def post(url: str, kwargs: dict[str, Any]) -> _FakeResponse:
        assert url == "https://api.deepseek.com/v1/tools/search"
        seen.append(kwargs)
        return _deepseek_response()

    client = _install_http(monkeypatch, post=post)
    spec = web_search_tool(
        environ={
            "WEB_SEARCH_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "sk-deepseek",
        }
    )
    result = spec.handler(
        _context(),
        {"query": "Q" * 200, "max_results": 3},
    )
    assert client.posts
    assert ddgs_calls == []
    assert seen[0]["headers"]["Authorization"] == "Bearer sk-deepseek"
    assert len(seen[0]["json"]["query"]) == 128
    assert seen[0]["json"]["count"] == 3
    assert seen[0]["json"]["need_content"] is False
    assert result["backend"] == "deepseek"
    assert result["results"][0]["snippet"] == "长摘要"


def test_deepseek_provider_ignores_backend_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddgs_calls = _install_ddgs(monkeypatch, results=[_first_result()])
    client = _install_http(monkeypatch, post=lambda url, kwargs: _deepseek_response())
    spec = web_search_tool(
        environ={
            "WEB_SEARCH_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "sk-deepseek",
            "DDGS_BACKEND": "google",
        }
    )
    result = spec.handler(
        _context(),
        {"query": "hello", "backend": "google"},
    )
    assert ddgs_calls == []
    assert client.posts[0][0] == "https://api.deepseek.com/v1/tools/search"
    assert result["backend"] == "deepseek"


def test_deepseek_provider_maps_timelimit_to_after_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddgs_calls = _install_ddgs(monkeypatch, results=[_first_result()])
    client = _install_http(monkeypatch, post=lambda url, kwargs: _deepseek_response())
    spec = web_search_tool(
        environ={
            "WEB_SEARCH_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "sk-search",
        }
    )
    result = spec.handler(
        _context(),
        {"query": "hello", "timelimit": "w"},
    )
    assert ddgs_calls == []
    assert client.posts[0][0] == "https://api.deepseek.com/v1/tools/search"
    assert client.posts[0][1]["headers"]["Authorization"] == "Bearer sk-search"
    assert " after:" in client.posts[0][1]["json"]["query"]
    assert result["backend"] == "deepseek"
    assert result["results"][0]["url"] == "https://deepseek.example/result"


def test_deepseek_provider_without_key_reports_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_ddgs(monkeypatch, results=[_first_result()])
    client = _install_http(monkeypatch)
    spec = web_search_tool(environ={"WEB_SEARCH_PROVIDER": "deepseek"})
    with pytest.raises(ToolExecutionError) as error:
        spec.handler(_context(), {"query": "hello"})
    assert error.value.code == "web_search_backend_not_configured"
    assert "DEEPSEEK_API_KEY" in error.value.safe_message
    assert client.posts == []


def test_default_provider_uses_ddgs_even_on_deepseek_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_ddgs(monkeypatch, results=[_first_result()])
    client = _install_http(monkeypatch)
    config = SimpleNamespace(
        base_url="https://api.deepseek.com",
        api_key="sk-deepseek",
    )
    spec = web_search_tool(environ={"DDGS_BACKEND": "google"})
    result = spec.handler(
        _context(model="deepseek", config=config),
        {"query": "hello"},
    )
    assert calls[0]["backend"] == "google"
    assert client.posts == []
    assert result["backend"] == "google"


def test_deepseek_can_serve_as_ddgs_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ddgs.exceptions import TimeoutException

    _install_ddgs(monkeypatch, error=TimeoutException("timed out"))
    client = _install_http(monkeypatch, post=lambda url, kwargs: _deepseek_response())
    spec = web_search_tool(
        environ={
            "DEEPSEEK_API_KEY": "sk-search",
            "WEB_SEARCH_FALLBACK_BACKENDS": "deepseek,bing_cn",
        }
    )
    result = spec.handler(_context(), {"query": "hello"})
    assert client.posts[0][0] == "https://api.deepseek.com/v1/tools/search"
    assert result["backend"] == "deepseek"


def test_invalid_provider_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_ddgs(monkeypatch, results=[_first_result()])
    _install_http(monkeypatch)
    spec = web_search_tool(environ={"WEB_SEARCH_PROVIDER": "serper"})
    with pytest.raises(ValueError):
        spec.handler(_context(), {"query": "hello"})


def test_deepseek_provider_failure_does_not_fall_back_to_ddgs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddgs_calls = _install_ddgs(monkeypatch, results=[_first_result()])

    def post(url: str, kwargs: dict[str, Any]) -> _FakeResponse:
        raise httpx.ConnectError("offline")

    _install_http(monkeypatch, post=post)
    spec = web_search_tool(
        environ={
            "WEB_SEARCH_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "sk-deepseek",
        }
    )
    with pytest.raises(ToolExecutionError) as error:
        spec.handler(_context(), {"query": "hello"})
    assert error.value.code == "web_search_unavailable"
    assert ddgs_calls == []


def test_ddgs_timeout_falls_back_to_zhipu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ddgs.exceptions import TimeoutException

    _install_ddgs(monkeypatch, error=TimeoutException("timed out"))

    def post(url: str, kwargs: dict[str, Any]) -> _FakeResponse:
        assert url == "https://open.bigmodel.cn/api/paas/v4/web_search"
        assert kwargs["headers"]["Authorization"] == "Bearer zp-key"
        assert kwargs["json"]["search_engine"] == "search_std"
        return _FakeResponse(
            {
                "search_result": [
                    {
                        "title": "智谱结果",
                        "link": "https://zhipu.example/result",
                        "content": "智谱摘要",
                    }
                ]
            }
        )

    _install_http(monkeypatch, post=post)
    spec = web_search_tool(
        environ={"ZHIPUAI_API_KEY": "zp-key", "DDGS_BACKEND": "google"}
    )
    result = spec.handler(_context(), {"query": "hello"})
    assert result["backend"] == "zhipu"
    assert result["results"] == [
        {
            "title": "智谱结果",
            "url": "https://zhipu.example/result",
            "snippet": "智谱摘要",
        }
    ]


def test_missing_zhipu_key_falls_back_to_bocha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ddgs.exceptions import TimeoutException

    _install_ddgs(monkeypatch, error=TimeoutException("timed out"))

    def post(url: str, kwargs: dict[str, Any]) -> _FakeResponse:
        assert url == "https://api.bochaai.com/v1/web-search"
        assert kwargs["headers"]["Authorization"] == "Bearer bocha-key"
        assert kwargs["json"]["summary"] is True
        return _FakeResponse(
            {
                "data": {
                    "webPages": {
                        "value": [
                            {
                                "name": "博查结果",
                                "url": "https://bocha.example/result",
                                "summary": "博查摘要",
                            }
                        ]
                    }
                }
            }
        )

    _install_http(monkeypatch, post=post)
    spec = web_search_tool(
        environ={"BOCHA_API_KEY": "bocha-key", "DDGS_BACKEND": "google"}
    )
    result = spec.handler(_context(), {"query": "hello"})
    assert result["backend"] == "bocha"
    assert result["results"][0]["url"] == "https://bocha.example/result"


def test_missing_paid_keys_falls_back_to_bing_cn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ddgs.exceptions import TimeoutException

    _install_ddgs(monkeypatch, error=TimeoutException("timed out"))
    html_text = (
        "<html><body><ol id='b_results'>"
        "<li class='b_algo'><h2><a href='https://cn.example/one'>标题一</a></h2>"
        "<div class='b_caption'><p>摘要一</p></div></li>"
        "<li class='b_algo'><h2><a href='https://cn.example/two'>标题二</a></h2>"
        "<p>摘要二</p></li>"
        "</ol></body></html>"
    )

    def get(url: str, kwargs: dict[str, Any]) -> _FakeResponse:
        assert url == "https://cn.bing.com/search"
        assert kwargs["params"]["q"] == "hello"
        assert kwargs["params"]["mkt"] == "zh-CN"
        return _FakeResponse(text=html_text)

    client = _install_http(monkeypatch, get=get)
    spec = web_search_tool(environ={"DDGS_BACKEND": "google"})
    result = spec.handler(_context(), {"query": "hello"})
    assert client.gets
    assert result["backend"] == "bing_cn"
    assert [item["url"] for item in result["results"]] == [
        "https://cn.example/one",
        "https://cn.example/two",
    ]
    assert result["results"][0]["title"] == "标题一"
    assert result["results"][0]["snippet"] == "摘要一"


def test_bing_cn_falls_back_to_global_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ddgs.exceptions import TimeoutException

    _install_ddgs(monkeypatch, error=TimeoutException("timed out"))
    seen: list[str] = []

    def get(url: str, kwargs: dict[str, Any]) -> _FakeResponse:
        seen.append(url)
        if url == "https://cn.bing.com/search":
            return _FakeResponse(text="<html><body>no results</body></html>")
        return _FakeResponse(
            text=(
                "<li class='b_algo'><h2>"
                "<a href='https://global.example/result'>全球结果</a>"
                "</h2><p>全球摘要</p></li>"
            )
        )

    _install_http(monkeypatch, get=get)
    spec = web_search_tool(environ={"DDGS_BACKEND": "google"})
    result = spec.handler(_context(), {"query": "hello"})
    assert seen == [
        "https://cn.bing.com/search",
        "https://www.bing.com/search",
    ]
    assert result["backend"] == "bing_cn"
    assert result["results"][0]["url"] == "https://global.example/result"


def test_empty_ddgs_results_do_not_trigger_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_ddgs(monkeypatch, results=[])
    client = _install_http(monkeypatch)
    spec = web_search_tool(
        environ={"ZHIPUAI_API_KEY": "zp-key", "DDGS_BACKEND": "google"}
    )
    result = spec.handler(_context(), {"query": "hello"})
    assert calls
    assert client.posts == []
    assert result["backend"] == "google"
    assert result["result_count"] == 0


def test_no_results_error_is_treated_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ddgs.exceptions import DDGSException

    _install_ddgs(monkeypatch, error=DDGSException("No results found."))
    client = _install_http(monkeypatch)
    spec = web_search_tool(
        environ={"ZHIPUAI_API_KEY": "zp-key", "DDGS_BACKEND": "google"}
    )
    result = spec.handler(_context(), {"query": "hello"})
    assert client.posts == []
    assert result["result_count"] == 0


def test_exhausted_fallback_chain_returns_stable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ddgs.exceptions import TimeoutException

    _install_ddgs(monkeypatch, error=TimeoutException("timed out"))

    def get(url: str, kwargs: dict[str, Any]) -> _FakeResponse:
        raise httpx.ConnectError("offline")

    _install_http(monkeypatch, get=get)
    spec = web_search_tool(environ={"DDGS_BACKEND": "google"})
    with pytest.raises(ToolExecutionError) as error:
        spec.handler(_context(), {"query": "hello"})
    assert error.value.code == "web_search_unavailable"


def test_explicit_china_backend_bypasses_ddgs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddgs_calls = _install_ddgs(monkeypatch, results=[_first_result()])

    def post(url: str, kwargs: dict[str, Any]) -> _FakeResponse:
        assert url == "https://api.bochaai.com/v1/web-search"
        return _FakeResponse(
            {
                "data": {
                    "webPages": {
                        "value": [
                            {
                                "name": "博查结果",
                                "url": "https://bocha.example/result",
                                "snippet": "摘要",
                            }
                        ]
                    }
                }
            }
        )

    _install_http(monkeypatch, post=post)
    spec = web_search_tool(environ={"BOCHA_API_KEY": "bocha-key"})
    result = spec.handler(
        _context(),
        {"query": "hello", "backend": "bocha"},
    )
    assert ddgs_calls == []
    assert result["backend"] == "bocha"


def test_duplicate_and_invalid_urls_are_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_ddgs(
        monkeypatch,
        results=[
            _first_result("一", "https://a.example"),
            _first_result("重复", "https://a.example"),
            _first_result("脚本", "javascript:alert(1)"),
        ],
    )
    _install_http(monkeypatch)
    spec = web_search_tool(environ={"DDGS_BACKEND": "google"})
    result = spec.handler(_context(), {"query": "hello"})
    assert [item["url"] for item in result["results"]] == [
        "https://a.example"
    ]


def _anysearch_response() -> _FakeResponse:
    return _FakeResponse(
        {
            "code": 0,
            "message": "success",
            "request_id": "req-1",
            "data": {
                "results": [
                    {
                        "title": "AnySearch 结果",
                        "url": "https://anysearch.example/result",
                        "snippet": "AnySearch 摘要",
                    }
                ],
                "metadata": {"total_results": 1},
            },
        }
    )


def test_anysearch_provider_sends_key_and_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddgs_calls = _install_ddgs(monkeypatch, results=[_first_result()])
    seen: list[dict[str, Any]] = []

    def post(url: str, kwargs: dict[str, Any]) -> _FakeResponse:
        assert url == "https://api.anysearch.com/v1/search"
        seen.append(kwargs)
        return _anysearch_response()

    client = _install_http(monkeypatch, post=post)
    spec = web_search_tool(
        environ={
            "WEB_SEARCH_PROVIDER": "anysearch",
            "ANYSEARCH_API_KEY": "as_sk_test",
            "ANYSEARCH_ZONE": "intl",
            "ANYSEARCH_LANGUAGE": "en",
        }
    )
    result = spec.handler(_context(), {"query": "hello", "max_results": 3})
    assert ddgs_calls == []
    assert client.posts
    assert seen[0]["headers"]["Authorization"] == "Bearer as_sk_test"
    assert seen[0]["json"] == {
        "query": "hello",
        "max_results": 3,
        "format": "json",
        "zone": "intl",
        "language": "en",
    }
    assert result["backend"] == "anysearch"
    assert result["results"][0]["url"] == "https://anysearch.example/result"


def test_anysearch_provider_supports_anonymous_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    def post(url: str, kwargs: dict[str, Any]) -> _FakeResponse:
        seen.append(kwargs)
        return _anysearch_response()

    _install_http(monkeypatch, post=post)
    spec = web_search_tool(environ={"WEB_SEARCH_PROVIDER": "anysearch"})
    result = spec.handler(_context(), {"query": "hello"})
    assert "Authorization" not in seen[0]["headers"]
    assert seen[0]["json"]["zone"] == "cn"
    assert seen[0]["json"]["language"] == "zh-CN"
    assert result["backend"] == "anysearch"


def test_anysearch_error_body_is_not_leaked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_body = (
        '{"code":-1,"message":"api_key=as_sk_secret_should_not_leak"}'
    )
    _install_http(
        monkeypatch,
        post=lambda url, kwargs: _FakeResponse(
            status_code=402,
            text=secret_body,
        ),
    )
    spec = web_search_tool(environ={"WEB_SEARCH_PROVIDER": "anysearch"})
    with pytest.raises(ToolExecutionError) as error:
        spec.handler(_context(), {"query": "hello"})
    assert error.value.code == "web_search_unavailable"
    assert "as_sk_secret_should_not_leak" not in str(error.value)


def test_anysearch_can_serve_as_ddgs_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ddgs.exceptions import TimeoutException

    _install_ddgs(monkeypatch, error=TimeoutException("timed out"))
    client = _install_http(
        monkeypatch,
        post=lambda url, kwargs: _anysearch_response(),
    )
    spec = web_search_tool(
        environ={"WEB_SEARCH_FALLBACK_BACKENDS": "anysearch,bing_cn"}
    )
    result = spec.handler(_context(), {"query": "hello"})
    assert client.posts[0][0] == "https://api.anysearch.com/v1/search"
    assert result["backend"] == "anysearch"


def test_anysearch_tag_and_params_are_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    def post(url: str, kwargs: dict[str, Any]) -> _FakeResponse:
        seen.append(kwargs)
        return _anysearch_response()

    _install_http(monkeypatch, post=post)
    spec = web_search_tool(
        environ={
            "WEB_SEARCH_PROVIDER": "anysearch",
            "ANYSEARCH_TAG": "code.doc",
            "ANYSEARCH_PARAMS": '{"library": "react"}',
        }
    )
    result = spec.handler(_context(), {"query": "hooks"})
    assert seen[0]["json"]["tag"] == "code.doc"
    assert seen[0]["json"]["params"] == {"library": "react"}
    assert result["backend"] == "anysearch"


def test_anysearch_invalid_params_reports_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_http(monkeypatch, post=lambda url, kwargs: _anysearch_response())
    spec = web_search_tool(
        environ={
            "WEB_SEARCH_PROVIDER": "anysearch",
            "ANYSEARCH_PARAMS": "not-json",
        }
    )
    with pytest.raises(ToolExecutionError) as error:
        spec.handler(_context(), {"query": "hello"})
    assert error.value.code == "web_search_backend_not_configured"
    assert "ANYSEARCH_PARAMS" in error.value.safe_message
