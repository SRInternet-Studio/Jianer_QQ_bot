from __future__ import annotations

from typing import Any

import pytest

from plugins.JianerAI.tools import image_search as image_search_module
from plugins.JianerAI.tools.contracts import ToolExecutionError
from plugins.JianerAI.tools.image_search import image_search_tool


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

        def images(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            calls.append({"query": query, **kwargs})
            if error is not None:
                raise error
            return [] if results is None else results

    monkeypatch.setattr(
        image_search_module,
        "_load_ddgs_class",
        lambda: _Client,
    )
    return calls


def test_tool_exposes_available_image_engines() -> None:
    spec = image_search_tool()
    backend = spec.input_schema["properties"]["backend"]
    assert "auto" in backend["enum"]
    assert "bing" in backend["enum"]
    assert "duckduckgo" in backend["enum"]
    assert spec.timeout_seconds == 18.0


def test_image_search_normalizes_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_ddgs(
        monkeypatch,
        results=[
            {
                "title": "  一张  图片 ",
                "image": "https://img.example/a.jpg",
                "thumbnail": "https://img.example/a-thumb.jpg",
                "url": "https://source.example/a",
                "width": "1920",
                "height": 1080,
                "source": "Example",
            },
            {
                "title": "重复",
                "image": "https://img.example/a.jpg",
            },
            {
                "title": "非法",
                "image": "javascript:alert(1)",
            },
        ],
    )
    result = image_search_tool().handler(
        object(),
        {"query": "cat", "max_results": 5, "backend": "bing"},
    )
    assert calls[0]["backend"] == "bing"
    assert calls[0]["region"] == "zh-cn"
    assert calls[0]["safesearch"] == "moderate"
    assert result["backend"] == "bing"
    assert result["result_count"] == 1
    item = result["results"][0]
    assert item == {
        "title": "一张 图片",
        "image_url": "https://img.example/a.jpg",
        "thumbnail_url": "https://img.example/a-thumb.jpg",
        "source_url": "https://source.example/a",
        "width": 1920,
        "height": 1080,
        "source": "Example",
    }


def test_invalid_backend_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_ddgs(monkeypatch, results=[])
    with pytest.raises(ValueError):
        image_search_tool().handler(
            object(),
            {"query": "cat", "backend": "google"},
        )


def test_image_search_failure_returns_stable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_ddgs(monkeypatch, error=RuntimeError("network down"))
    with pytest.raises(ToolExecutionError) as error:
        image_search_tool().handler(object(), {"query": "cat"})
    assert error.value.code == "image_search_unavailable"


def test_empty_results_return_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_ddgs(monkeypatch, results=[])
    result = image_search_tool().handler(object(), {"query": "cat"})
    assert result["backend"] == "auto"
    assert result["result_count"] == 0
    assert result["results"] == []
