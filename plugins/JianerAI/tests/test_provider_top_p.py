from __future__ import annotations

from plugins.JianerAI.providers import (
    ChatRequest,
    ModelConfig,
    _build_anthropic_payload,
    _build_gemini_payload,
    _build_openai_payload,
    _build_responses_payload,
)


def _config(top_p: float) -> ModelConfig:
    return ModelConfig(
        key="haiku",
        friendly_name="haiku",
        provider="OpenAI Chat Completions",
        model="claude-haiku-5-5",
        api_key="sk-test",
        top_p=top_p,
    )


def _request() -> ChatRequest:
    return ChatRequest(message="hello")


def test_openai_omits_default_top_p_but_keeps_custom() -> None:
    assert "top_p" not in _build_openai_payload(_config(1.0), _request())
    assert _build_openai_payload(_config(0.9), _request())["top_p"] == 0.9


def test_responses_omits_default_top_p_but_keeps_custom() -> None:
    assert "top_p" not in _build_responses_payload(_config(1.0), _request())
    assert _build_responses_payload(_config(0.9), _request())["top_p"] == 0.9


def test_anthropic_omits_default_top_p_but_keeps_custom() -> None:
    assert "top_p" not in _build_anthropic_payload(_config(1.0), _request())
    assert _build_anthropic_payload(_config(0.9), _request())["top_p"] == 0.9


def test_gemini_omits_default_top_p_but_keeps_custom() -> None:
    default_config = _build_gemini_payload(_config(1.0), _request())
    assert "topP" not in default_config["generationConfig"]
    custom_config = _build_gemini_payload(_config(0.9), _request())
    assert custom_config["generationConfig"]["topP"] == 0.9


def test_openai_relay_moves_system_prompt_to_user_instruction() -> None:
    config = ModelConfig(
        key="relay",
        friendly_name="relay",
        provider="OpenAI Chat Completions",
        model="deepseek-v4.1-flash",
        api_key="test",
        base_url="https://relay.example/v1",
    )
    payload = _build_openai_payload(
        config,
        ChatRequest(message="你是谁？", system_prompt="你是机娘星语。"),
    )
    assert [item["role"] for item in payload["messages"]] == ["user", "user"]
    assert "你是机娘星语。" in payload["messages"][0]["content"]
    assert payload["messages"][1]["content"] == "你是谁？"


def test_openai_official_endpoint_keeps_system_role() -> None:
    config = ModelConfig(
        key="official",
        friendly_name="official",
        provider="OpenAI Chat Completions",
        model="gpt-test",
        api_key="test",
        base_url="https://api.openai.com/v1",
    )
    payload = _build_openai_payload(
        config,
        ChatRequest(message="你好", system_prompt="你是机娘星语。"),
    )
    assert [item["role"] for item in payload["messages"]] == ["system", "user"]


def test_explicit_system_mode_overrides_relay_detection() -> None:
    config = ModelConfig(
        key="relay",
        friendly_name="relay",
        provider="OpenAI Chat Completions",
        model="deepseek-v4.1-flash",
        api_key="test",
        base_url="https://relay.example/v1",
        system_prompt_mode="system",
    )
    payload = _build_openai_payload(
        config,
        ChatRequest(message="你好", system_prompt="你是机娘星语。"),
    )
    assert [item["role"] for item in payload["messages"]] == ["system", "user"]
