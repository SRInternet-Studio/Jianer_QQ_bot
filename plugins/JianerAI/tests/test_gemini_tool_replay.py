from __future__ import annotations

from typing import Any

from plugins.JianerAI.providers import (
    AssistantTurn,
    ChatRequest,
    ModelConfig,
    ProviderToolCall,
    ToolResultTurn,
    _build_gemini_payload,
)


def _config(model: str = "gemini-3.8-flash") -> ModelConfig:
    return ModelConfig(
        key="peachgemini",
        friendly_name="peachgemini",
        provider="Google GenerateContent",
        model=model,
        api_key="sk-test",
    )


def _payload(turns: Any, *, model: str = "gemini-3.8-flash") -> dict[str, Any]:
    return _build_gemini_payload(
        _config(model),
        ChatRequest(message="hello", turns=tuple(turns)),
    )


def _call(name: str, call_id: str, *, signed: bool) -> dict[str, Any]:
    part: dict[str, Any] = {
        "functionCall": {"id": call_id, "name": name, "args": {"query": "x"}},
    }
    if signed:
        part["thoughtSignature"] = "sig-" + call_id
    return part


def _structured_turn(*parts: dict[str, Any]) -> AssistantTurn:
    return AssistantTurn(
        text="",
        tool_calls=tuple(
            ProviderToolCall(
                id=str(part["functionCall"]["id"]),
                name=str(part["functionCall"]["name"]),
                arguments=dict(part["functionCall"]["args"]),
            )
            for part in parts
        ),
        provider_content={"role": "model", "parts": list(parts)},
    )


def test_signed_function_call_replays_with_matching_response() -> None:
    payload = _payload(
        [
            _structured_turn(_call("web_search", "c1", signed=True)),
            ToolResultTurn("c1", "web_search", '{"ok":true}'),
        ]
    )
    contents = payload["contents"]
    assert "functionCall" in contents[-2]["parts"][0]
    responses = contents[-1]["parts"]
    assert len(responses) == 1
    assert responses[0]["functionResponse"]["name"] == "web_search"
    assert responses[0]["functionResponse"]["id"] == "c1"


def test_missing_thought_signature_degrades_to_text_replay() -> None:
    payload = _payload(
        [
            _structured_turn(_call("web_search", "c1", signed=False)),
            ToolResultTurn("c1", "web_search", '{"ok":true}'),
        ]
    )
    flattened = [
        part
        for content in payload["contents"]
        for part in content.get("parts", ())
    ]
    assert all("functionCall" not in part for part in flattened)
    assert all("functionResponse" not in part for part in flattened)
    assert any(
        "[工具调用] web_search" in str(part.get("text", ""))
        for part in flattened
    )
    assert any(
        "[工具结果] web_search" in str(part.get("text", ""))
        for part in flattened
    )


def test_missing_result_is_synthesized_for_parallel_calls() -> None:
    payload = _payload(
        [
            _structured_turn(
                _call("get_repository", "c1", signed=True),
                _call("get_repository", "c2", signed=True),
            ),
            ToolResultTurn("c1", "get_repository", '{"ok":true}'),
        ]
    )
    responses = payload["contents"][-1]["parts"]
    assert len(responses) == 2
    assert responses[0]["functionResponse"]["id"] == "c1"
    assert responses[1]["functionResponse"]["id"] == "c2"
    assert "error" in responses[1]["functionResponse"]["response"]


def test_orphan_tool_result_replays_as_text() -> None:
    payload = _payload([ToolResultTurn("c1", "web_search", '{"ok":true}')])
    parts = payload["contents"][-1]["parts"]
    assert "functionResponse" not in parts[0]
    assert "[工具结果] web_search" in parts[0]["text"]


def test_non_gemini_3_model_keeps_unsigned_function_call() -> None:
    payload = _payload(
        [
            _structured_turn(_call("web_search", "c1", signed=False)),
            ToolResultTurn("c1", "web_search", '{"ok":true}'),
        ],
        model="gemini-2.5-pro",
    )
    assert "functionCall" in payload["contents"][-2]["parts"][0]
    assert "functionResponse" in payload["contents"][-1]["parts"][0]
