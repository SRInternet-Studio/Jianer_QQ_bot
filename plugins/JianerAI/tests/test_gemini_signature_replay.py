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
