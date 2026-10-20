from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from plugins.JianerAI.tools import reverse_image_search as reverse_module
from plugins.JianerAI.tools.contracts import ToolExecutionError
from plugins.JianerAI.tools.reverse_image_search import reverse_image_search_tool


class _FakeResponse:
    def __init__(
        self,
        *,
        payload: Any = None,
        text: str = "",
        content: bytes = b"",
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        url: str = "",
    ) -> None:
        self._payload = payload
        self.text = text
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._payload


class _FakeClient:
    def __init__(self, *, post: Any = None, get: Any = None) -> None:
        self._post = post
        self._get = get
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.gets: list[tuple[str, dict[str, Any]]] = []

    def __enter__(self) -> "_FakeClient":
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


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    post: Any = None,
    get: Any = None,
) -> _FakeClient:
    client = _FakeClient(post=post, get=get)
    monkeypatch.setattr(reverse_module, "_http_client", lambda: client)
    return client


def _context(*, mime: str = "image/png", data: bytes = b"image-bytes") -> Any:
    return SimpleNamespace(
        attachments=(SimpleNamespace(mime=mime, data=data),),
    )


def test_bing_backend_normalizes_visual_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "tags": [
            {
                "displayName": "DeepSeek",
                "actions": [
                    {
                        "data": {
                            "value": [
                                {
                                    "name": "DeepSeek 官网",
                                    "hostPageUrl": "https://deepseek.example/",
                                    "snippet": "AI research company",
                                }
                            ]
                        }
                    }
                ],
            }
        ]
    }
    client = _install(
        monkeypatch,
        post=lambda url, kwargs: _FakeResponse(payload=payload),
    )
    result = reverse_image_search_tool(
        environ={"BING_VISUAL_SEARCH_KEY": "bing-key"}
    ).handler(_context(), {"backend": "bing", "max_results": 3})
    assert client.posts[0][0].endswith("/v7.0/images/visualsearch")
    assert client.posts[0][1]["headers"]["Ocp-Apim-Subscription-Key"] == "bing-key"
    assert result["backend"] == "bing"
    assert result["results"][0]["title"] == "DeepSeek 官网"
    assert result["results"][0]["url"] == "https://deepseek.example/"


def test_yandex_backend_is_used_in_auto_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = (
        "<a href='https://result.example/a'>结果 A</a>"
        "<a href='https://yandex.com/images'>忽略</a>"
    )
    client = _install(
        monkeypatch,
        post=lambda url, kwargs: _FakeResponse(
            payload={
                "blocks": [
                    {"params": {"url": "/images/search?cbir_id=abc"}}
                ]
            }
        ),
        get=lambda url, kwargs: _FakeResponse(text=html),
    )
    result = reverse_image_search_tool(environ={}).handler(
        _context(),
        {"max_results": 3},
    )
    assert client.posts[0][0] == "https://yandex.com/images/search"
    assert result["backend"] == "yandex"
    assert [item["url"] for item in result["results"]] == [
        "https://result.example/a"
    ]


def test_saucenao_backend_parses_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _install(
        monkeypatch,
        post=lambda url, kwargs: _FakeResponse(
            payload={
                "results": [
                    {
                        "header": {"similarity": "95.2"},
                        "data": {
                            "title": "测试作品",
                            "ext_urls": ["https://source.example/item"],
                        },
                    }
                ]
            }
        ),
    )
    result = reverse_image_search_tool(
        environ={"SAUCENAO_API_KEY": "sauce-key"}
    ).handler(_context(), {"backend": "saucenao"})
    assert client.posts[0][0] == "https://saucenao.com/search.php"
    assert client.posts[0][1]["data"]["api_key"] == "sauce-key"
    assert result["backend"] == "saucenao"
    assert result["results"][0]["snippet"] == "相似度 95.2%"


def test_missing_image_reports_stable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch)
    with pytest.raises(ToolExecutionError) as error:
        reverse_image_search_tool(environ={}).handler(
            SimpleNamespace(attachments=()),
            {},
        )
    assert error.value.code == "reverse_image_search_no_image"


def test_image_url_is_downloaded_before_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _install(
        monkeypatch,
        get=lambda url, kwargs: _FakeResponse(
            content=b"downloaded-image",
            headers={"content-type": "image/jpeg"},
        ),
        post=lambda url, kwargs: _FakeResponse(
            payload={
                "tags": [
                    {
                        "actions": [
                            {
                                "data": {
                                    "value": [
                                        {
                                            "name": "结果",
                                            "hostPageUrl": "https://r.example/",
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                ]
            }
        ),
    )
    result = reverse_image_search_tool(
        environ={"BING_VISUAL_SEARCH_KEY": "bing-key"}
    ).handler(
        SimpleNamespace(attachments=()),
        {"image_url": "https://img.example/a.jpg", "backend": "bing"},
    )
    assert client.gets[0][0] == "https://img.example/a.jpg"
    assert client.posts[0][1]["files"]["image"][1] == b"downloaded-image"
    assert result["results"][0]["url"] == "https://r.example/"
