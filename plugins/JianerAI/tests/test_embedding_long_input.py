from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from plugins.JianerAI.memorix.core.embedding import api_adapter as embedding_adapter


class _FakeResponse:
    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self._text = text

    async def text(self) -> str:
        return self._text


class _FakePost:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _FakeSession:
    def __init__(self, *, handler: Any, calls: list[dict[str, Any]]) -> None:
        self._handler = handler
        self._calls = calls

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: Any = None,
    ) -> _FakePost:
        payload = dict(json or {})
        response = self._handler(payload)
        if response.status < 400:
            self._calls.append(payload)
        return _FakePost(response)


def _install_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    limit: int,
    calls: list[dict[str, Any]],
) -> None:
    def handler(payload: dict[str, Any]) -> _FakeResponse:
        text = str(payload.get("input") or "")
        if len(text) > limit:
            return _FakeResponse(
                400,
                json.dumps(
                    {
                        "error": {
                            "message": (
                                "input too long: 841 tokens > 512 max. "
                                "Truncate or shorten the text."
                            ),
                            "type": "input_too_long",
                        }
                    }
                ),
            )
        return _FakeResponse(
            200,
            json.dumps({"data": [{"embedding": [1.0, 0.0, 0.0, 0.0]}]}),
        )

    monkeypatch.setattr(
        embedding_adapter.aiohttp,
        "ClientSession",
        lambda **kwargs: _FakeSession(handler=handler, calls=calls),
    )


def _adapter() -> embedding_adapter.EmbeddingAPIAdapter:
    return embedding_adapter.EmbeddingAPIAdapter(
        batch_size=1,
        max_concurrent=1,
        default_dimension=4,
        enable_cache=False,
        model_name="BAAI/bge-m3",
        provider="openai",
        api_key="test-key",
        base_url="https://example.test/v1",
        retry_config={
            "max_attempts": 1,
            "min_wait_seconds": 0.01,
            "max_wait_seconds": 0.02,
            "backoff_multiplier": 1.0,
        },
    )


def test_input_too_long_marker_detection() -> None:
    assert embedding_adapter._is_input_too_long_error(
        '{"error":{"message":"input too long: 841 tokens > 512 max."}}'
    )
    assert not embedding_adapter._is_input_too_long_error(
        '{"error":{"message":"invalid api key"}}'
    )


def test_split_prefers_natural_boundary() -> None:
    text = "第一段内容。\n第二段更长的内容，用来验证切分位置。"
    left, right = embedding_adapter._split_text_for_embedding(text)
    assert left + right == text
    assert left and right
    assert left.endswith("\n") or left.endswith("。")


def test_merge_chunk_vectors_is_length_weighted_and_normalized() -> None:
    merged = embedding_adapter._merge_embedding_vectors(
        [[3.0, 0.0, 0.0, 0.0], [0.0, 4.0, 0.0, 0.0]],
        [1.0, 1.0],
    )
    assert merged[0] == pytest.approx(0.6, abs=1e-6)
    assert merged[1] == pytest.approx(0.8, abs=1e-6)
    norm = sum(value * value for value in merged) ** 0.5
    assert norm == pytest.approx(1.0, abs=1e-6)


def test_long_text_is_split_and_encoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    _install_session(monkeypatch, limit=300, calls=calls)
    adapter = _adapter()
    vector = asyncio.run(adapter._get_embedding_direct_http("长" * 900))
    assert len(vector) == 4
    assert len(calls) >= 4
    assert all(len(str(call.get("input") or "")) <= 300 for call in calls)


def test_chunk_still_too_long_falls_back_to_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    _install_session(monkeypatch, limit=120, calls=calls)
    adapter = _adapter()
    vector = asyncio.run(adapter._get_embedding_direct_http("长" * 250))
    assert len(vector) == 4
    assert all(len(str(call.get("input") or "")) <= 120 for call in calls)
