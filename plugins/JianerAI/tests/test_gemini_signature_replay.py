from __future__ import annotations

from typing import Any

from plugins.JianerAI.providers import (
    AssistantTurn,
    ChatRequest,
    ModelConfig,
    ToolResultTurn,
    _build_gemini_payload,
    _sanitize_gemini_content,
)


def _call(name: str) -> dict[str, Any]:
    return {"functionCall": {"name": name, "args": {"query": "x"}}}


def test_standalone_signature_moves_to_function_call() -> None:
    cleaned = _sanitize_gemini_content(
        {
            "role": "model",
            "parts": [
                {"thoughtSignature": "sig-1"},
                _call("web_search"),
            ],
        }
    )
    assert cleaned is not None
    parts = cleaned["parts"]
    assert len(parts) == 1
    assert parts[0]["functionCall"]["name"] == "web_search"
    assert parts[0]["thoughtSignature"] == "sig-1"


def test_signature_is_copied_to_parallel_function_calls() -> None:
    cleaned = _sanitize_gemini_content(
        {
            "role": "model",
            "parts": [
                {"thoughtSignature": "sig-2"},
                _call("web_search"),
                _call("github_repository"),
            ],
        }
    )
    assert cleaned is not None
    parts = cleaned["parts"]
    assert [part["thoughtSignature"] for part in parts] == ["sig-2", "sig-2"]


def test_existing_signature_is_not_overwritten() -> None:
    own = dict(_call("web_search"))
    own["thoughtSignature"] = "own-sig"
    cleaned = _sanitize_gemini_content(
        {
            "role": "model",
            "parts": [{"thoughtSignature": "first-sig"}, own],
        }
    )
    assert cleaned is not None
    assert cleaned["parts"][0]["thoughtSignature"] == "own-sig"


def test_plain_text_parts_are_unchanged() -> None:
    content = {"role": "model", "parts": [{"text": "hello"}]}
    assert _sanitize_gemini_content(content) == content


def test_gemini_payload_replays_function_call_with_signature() -> None:
    config = ModelConfig(
        key="peachgemini",
        friendly_name="peachgemini",
        provider="Google GenerateContent",
        model="gemini-3.8-flash",
        api_key="sk-test",
    )
    turn = AssistantTurn(
        provider_content={
            "role": "model",
            "parts": [{"thoughtSignature": "sig-3"}, _call("web_search")],
        },
    )
    payload = _build_gemini_payload(
        config,
        ChatRequest(
            message="hello",
            turns=(
                turn,
                ToolResultTurn("call-1", "web_search", '{"ok":true}'),
            ),
        ),
    )
    model_content = payload["contents"][-2]
    assert model_content["parts"][0]["thoughtSignature"] == "sig-3"
    assert "functionCall" in model_content["parts"][0]


def test_unsigned_function_call_degrades_to_text_results() -> None:
    config = ModelConfig(
        key="peachgemini",
        friendly_name="peachgemini",
        provider="Google GenerateContent",
        model="gemini-3.8-flash",
        api_key="sk-test",
    )
    turn = AssistantTurn(
        text="我先查一下。",
        provider_content={
            "role": "model",
            "parts": [
                {"text": "我先查一下。"},
                _call("web_search"),
            ],
        },
    )
    payload = _build_gemini_payload(
        config,
        ChatRequest(
            message="hello",
            turns=(
                turn,
                ToolResultTurn("call-1", "web_search", '{"ok":true}'),
            ),
        ),
    )
    flattened = [
        part
        for content in payload["contents"]
        for part in content.get("parts", ())
    ]
    assert all("functionCall" not in part for part in flattened)
    assert all("functionResponse" not in part for part in flattened)
    assert any(
        "工具 web_search 返回结果" in str(part.get("text", ""))
        for part in flattened
    )
    assert all(
        "[工具调用]" not in str(part.get("text", ""))
        for part in flattened
    )


def test_signed_parallel_calls_get_aligned_responses() -> None:
    config = ModelConfig(
        key="peachgemini",
        friendly_name="peachgemini",
        provider="Google GenerateContent",
        model="gemini-3.8-flash",
        api_key="sk-test",
    )
    calls = [
        {"functionCall": {"id": "c1", "name": "web_search", "args": {}}},
        {"functionCall": {"id": "c2", "name": "web_search", "args": {}}},
    ]
    for call in calls:
        call["thoughtSignature"] = "sig-" + call["functionCall"]["id"]
    payload = _build_gemini_payload(
        config,
        ChatRequest(
            message="hello",
            turns=(
                AssistantTurn(
                    provider_content={"role": "model", "parts": calls},
                ),
                ToolResultTurn("c1", "web_search", '{"ok":true}'),
            ),
        ),
    )
    responses = payload["contents"][-1]["parts"]
    assert len(responses) == 2
    assert responses[0]["functionResponse"]["id"] == "c1"
    assert responses[1]["functionResponse"]["id"] == "c2"
    assert "error" in responses[1]["functionResponse"]["response"]
