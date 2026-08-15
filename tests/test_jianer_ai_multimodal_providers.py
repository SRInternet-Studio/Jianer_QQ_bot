from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from jianer import segments as Segments
from jianer.adapters import MediaKind

from plugins.JianerAI.media_input import ProcessedMedia, ProcessedVideo
from plugins.JianerAI.providers import (
    ChatRequest,
    EmptyProviderResponseError,
    FunctionTool,
    MediaAttachment,
    MediaCapabilityError,
    ModelConfig,
    ProviderRegistry,
    ToolResultTurn,
    _build_anthropic_payload,
    _build_gemini_payload,
    _build_openai_payload,
    _build_responses_payload,
    _default_anthropic_request,
    _default_gemini_request,
    _extract_gemini_response,
    _google_sdk_contents,
    _google_sdk_generate_config,
    _google_sdk_response_payload,
)
from plugins.JianerAI.service import JianerAIService


def _write_config(
    root: Path,
    name: str,
    response_type: str,
    **overrides,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "FriendlyName": name,
        "Model": f"{name}-model",
        "ResponseType": response_type,
        "ApiKey": "test-secret",
        "BaseUrl": "https://gateway.invalid/v1",
        "Temperature": 0.2,
        "MaxTokens": 256,
        **overrides,
    }
    (root / f"{name}.ai.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _responses_text(response_id: str, text: str) -> dict:
    return {
        "id": response_id,
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
    }


def _numeric_enum_tool_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "index_types": {
                "type": "array",
                "items": {
                    "type": "integer",
                    "enum": [0, 1, 2, 3, 5, 6, 8, 9, 10, 11, 12, 13, 14, 15, 16],
                },
            },
            "interval_minutes": {
                "type": "integer",
                "enum": [15, 30, 60],
                "default": 15,
            },
        },
        "required": ["index_types"],
        "additionalProperties": False,
    }


def test_canonical_parser_names_and_legacy_aliases_load(tmp_path: Path) -> None:
    config_dir = tmp_path / "aiconfig"
    values = {
        "chat": ("OpenAI Chat Completions", "openai_chat_completions"),
        "responses": ("OpenAI Responses", "openai_responses"),
        "google": ("Google GenerateContent", "google_generate_content"),
        "anthropic": ("Anthropic Messages", "anthropic_messages"),
        "legacy-openai": ("openai", "openai_chat_completions"),
        "legacy-gemini": ("gemini", "google_generate_content"),
    }
    for name, (response_type, _) in values.items():
        _write_config(config_dir, name, response_type)

    registry = ProviderRegistry(config_dir, transport=lambda *_: None)

    assert {
        name: registry.get(name).provider
        for name in values
    } == {name: normalized for name, (_, normalized) in values.items()}


def test_one_function_tool_schema_is_serialized_for_all_parsers() -> None:
    from google.genai import types

    schema = _numeric_enum_tool_schema()
    request = ChatRequest(
        message="query",
        tools=(
            FunctionTool(
                name="common_lookup",
                description="Common lookup",
                parameters=schema,
            ),
        ),
    )
    config = ModelConfig(
        key="test",
        friendly_name="Test",
        provider="OpenAI Chat Completions",
        model="test-model",
        api_key="test-secret",
    )

    chat = _build_openai_payload(config, request)
    responses = _build_responses_payload(config, request)
    google = _build_gemini_payload(config, request)
    anthropic = _build_anthropic_payload(config, request)

    assert chat["tools"][0]["function"]["parameters"] == schema
    assert responses["tools"][0]["parameters"] == schema
    assert google["tools"][0]["functionDeclarations"][0][
        "parametersJsonSchema"
    ] == schema
    assert anthropic["tools"][0]["input_schema"] == schema

    google_config = _google_sdk_generate_config(google, types)
    declaration = google_config.tools[0].function_declarations[0]
    assert declaration.parameters is None
    assert declaration.parameters_json_schema == schema


def test_google_gemini_3_tool_requests_default_to_medium_thinking() -> None:
    from google.genai import types

    tool = FunctionTool(
        name="lookup",
        description="Lookup",
        parameters=_numeric_enum_tool_schema(),
    )
    config = ModelConfig(
        key="google",
        friendly_name="Google",
        provider="Google GenerateContent",
        model="gemini-3.1-pro",
        api_key="test-secret",
    )

    tool_payload = _build_gemini_payload(
        config,
        ChatRequest(message="query", tools=(tool,)),
    )
    plain_payload = _build_gemini_payload(
        config,
        ChatRequest(message="query"),
    )
    high_payload = _build_gemini_payload(
        ModelConfig(
            key="google-high",
            friendly_name="Google High",
            provider="Google GenerateContent",
            model="gemini-3.1-pro",
            api_key="test-secret",
            thinking_level="HIGH",
        ),
        ChatRequest(message="query", tools=(tool,)),
    )

    assert tool_payload["generationConfig"]["thinkingConfig"] == {
        "thinkingLevel": "medium"
    }
    assert "thinkingConfig" not in plain_payload["generationConfig"]
    assert high_payload["generationConfig"]["thinkingConfig"] == {
        "thinkingLevel": "high"
    }
    sdk_config = _google_sdk_generate_config(tool_payload, types)
    assert sdk_config.thinking_config.thinking_level.value == "MEDIUM"


def test_google_empty_response_reports_safe_finish_metadata(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config_dir = tmp_path / "google-empty"
        _write_config(
            config_dir,
            "google",
            "Google GenerateContent",
            Model="gemini-3.1-pro",
        )
        payloads: list[dict] = []

        async def transport(provider, config, payload):
            assert provider == "gemini"
            payloads.append(dict(payload))
            return {
                "candidates": [
                    {
                        "content": {"role": "model"},
                        "finishReason": "MAX_TOKENS",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 4000,
                    "candidatesTokenCount": 0,
                    "thoughtsTokenCount": 2000,
                    "totalTokenCount": 6000,
                },
            }

        registry = ProviderRegistry(config_dir, transport=transport)
        with pytest.raises(
            EmptyProviderResponseError,
            match=r"finish_reason=MAX_TOKENS.*thoughts_tokens=2000",
        ):
            await registry.complete_request(
                "google",
                ChatRequest(
                    message="query",
                    tools=(
                        FunctionTool(
                            name="lookup",
                            description="Lookup",
                            parameters=_numeric_enum_tool_schema(),
                        ),
                    ),
                ),
            )

        assert payloads[0]["generationConfig"]["thinkingConfig"] == {
            "thinkingLevel": "medium"
        }

    asyncio.run(scenario())


def test_all_parsers_replay_only_supplied_local_history(tmp_path: Path) -> None:
    async def scenario() -> None:
        config_dir = tmp_path / "all-local-history"
        _write_config(config_dir, "chat", "OpenAI Chat Completions")
        _write_config(config_dir, "responses", "OpenAI Responses")
        _write_config(config_dir, "google", "Google GenerateContent")
        _write_config(config_dir, "anthropic", "Anthropic Messages")
        payloads: dict[str, dict] = {}

        async def transport(provider, config, payload):
            payloads[config.key] = dict(payload)
            if provider == "openai":
                return {"choices": [{"message": {"content": "ok"}}]}
            if provider == "responses":
                return _responses_text("ignored-response-id", "ok")
            if provider == "gemini":
                return {
                    "candidates": [
                        {"content": {"parts": [{"text": "ok"}]}}
                    ]
                }
            return {"content": [{"type": "text", "text": "ok"}]}

        registry = ProviderRegistry(config_dir, transport=transport)
        history = (
            {"role": "user", "content": "local question"},
            {"role": "assistant", "content": "local answer"},
        )
        for key in ("chat", "responses", "google", "anthropic"):
            assert await registry.chat(key, "current", history=history) == "ok"

        assert payloads["chat"]["messages"] == [
            *history,
            {"role": "user", "content": "current"},
        ]
        assert payloads["responses"]["input"] == [
            *history,
            {"role": "user", "content": "current"},
        ]
        assert payloads["responses"]["store"] is False
        assert "previous_response_id" not in payloads["responses"]
        assert payloads["google"]["contents"] == [
            {"role": "user", "parts": [{"text": "local question"}]},
            {"role": "model", "parts": [{"text": "local answer"}]},
            {"role": "user", "parts": [{"text": "current"}]},
        ]
        assert payloads["anthropic"]["messages"] == [
            *history,
            {"role": "user", "content": [{"type": "text", "text": "current"}]},
        ]

    asyncio.run(scenario())


def test_google_transport_uses_genai_sdk_and_custom_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        import httpx
        from google import genai
        from google.genai import types

        captured: dict[str, object] = {}

        class FakeResponse:
            def model_dump(self, **kwargs):
                captured["dump_kwargs"] = kwargs
                return {
                    "candidates": [
                        {"content": {"parts": [{"text": "sdk ok"}]}}
                    ]
                }

        class FakeModels:
            async def generate_content(self, **kwargs):
                captured["request"] = kwargs
                return FakeResponse()

        class FakeAsyncClient:
            def __init__(self):
                self.models = FakeModels()

            async def aclose(self):
                captured["closed"] = True

        class FakeClient:
            def __init__(self, **kwargs):
                captured["client"] = kwargs
                self.aio = FakeAsyncClient()

        monkeypatch.setattr(genai, "Client", FakeClient)
        config = ModelConfig(
            key="google",
            friendly_name="Google",
            provider="Google GenerateContent",
            model="gemini-test",
            api_key="test-secret",
            base_url="https://gateway.invalid/google/v1beta",
            request_timeout_seconds=12.5,
        )
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": "describe"},
                        {
                            "inlineData": {
                                "mimeType": "image/png",
                                "data": "aW1hZ2U=",
                            }
                        },
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0.2,
                "topP": 0.9,
                "maxOutputTokens": 128,
            },
            "systemInstruction": {
                "role": "system",
                "parts": [{"text": "be concise"}],
            },
            "tools": [
                {
                    "functionDeclarations": [
                        {
                            "name": "lookup",
                            "description": "Lookup",
                            "parameters": _numeric_enum_tool_schema(),
                        }
                    ]
                }
            ],
        }

        response = await _default_gemini_request(config, payload)

        assert (
            response["candidates"][0]["content"]["parts"][0]["text"]
            == "sdk ok"
        )
        client_options = captured["client"]
        assert isinstance(client_options, dict)
        http_options = client_options["http_options"]
        assert isinstance(http_options, types.HttpOptions)
        assert http_options.base_url == "https://gateway.invalid/google"
        assert http_options.api_version == "v1beta"
        assert http_options.timeout == 12_500
        assert isinstance(
            http_options.async_client_args["transport"],
            httpx.AsyncHTTPTransport,
        )
        request = captured["request"]
        assert isinstance(request, dict)
        assert request["model"] == "gemini-test"
        assert isinstance(request["contents"][0], types.Content)
        assert request["contents"][0].parts[1].inline_data.data == b"image"
        assert isinstance(request["config"], types.GenerateContentConfig)
        assert request["config"].automatic_function_calling.disable is True
        assert request["config"].should_return_http_response is True
        declaration = request["config"].tools[0].function_declarations[0]
        assert declaration.parameters is None
        assert declaration.parameters_json_schema["properties"][
            "index_types"
        ]["items"]["enum"] == [
            0,
            1,
            2,
            3,
            5,
            6,
            8,
            9,
            10,
            11,
            12,
            13,
            14,
            15,
            16,
        ]
        assert declaration.parameters_json_schema["properties"][
            "interval_minutes"
        ]["enum"] == [15, 30, 60]
        assert captured["dump_kwargs"] == {
            "mode": "json",
            "by_alias": True,
            "exclude_none": True,
        }
        assert captured["closed"] is True

    asyncio.run(scenario())


def test_google_sdk_prefers_lossless_raw_http_response_body() -> None:
    raw_response = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {
                                "id": "call-raw-1",
                                "name": "maimaidx_b50",
                                "args": {},
                            }
                        }
                    ],
                },
                "finishReason": "STOP",
            }
        ]
    }
    response = SimpleNamespace(
        sdk_http_response=SimpleNamespace(body=json.dumps(raw_response)),
        model_dump=lambda **_: {"candidates": []},
    )

    payload = _google_sdk_response_payload(response)
    extracted = _extract_gemini_response(payload)

    assert payload == raw_response
    assert extracted.tool_calls[0].id == "call-raw-1"
    assert extracted.tool_calls[0].name == "maimaidx_b50"
    assert extracted.tool_calls[0].arguments == {}


def test_gemini_extractor_excludes_thought_text_from_final_answer() -> None:
    content = {
        "role": "model",
        "parts": [
            {
                "text": "private thought summary",
                "thought": True,
                "thoughtSignature": "EjQ=",
            },
            {"text": "visible answer with explicit flag", "thought": False},
            {"text": "visible final answer"},
        ],
    }

    extracted = _extract_gemini_response(
        {"candidates": [{"content": content}]}
    )

    assert extracted.text == (
        "visible answer with explicit flag\nvisible final answer"
    )
    assert extracted.turn.text == extracted.text
    assert extracted.turn.provider_content == content


def test_google_thought_only_response_is_not_returned_as_answer(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config_dir = tmp_path / "google-thought-only"
        _write_config(
            config_dir,
            "google",
            "Google GenerateContent",
            Model="gemini-3.1-pro",
        )

        async def transport(provider, config, payload):
            assert provider == "gemini"
            return {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "text": "private thought summary",
                                    "thought": True,
                                }
                            ],
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "thoughtsTokenCount": 42,
                    "totalTokenCount": 42,
                },
            }

        registry = ProviderRegistry(config_dir, transport=transport)
        with pytest.raises(
            EmptyProviderResponseError,
            match=r"finish_reason=STOP.*thoughts_tokens=42",
        ):
            await registry.complete_request(
                "google",
                ChatRequest(message="query"),
            )

    asyncio.run(scenario())


def test_google_sdk_tool_signature_round_trips_as_base64() -> None:
    from google.genai import types

    config = ModelConfig(
        key="google",
        friendly_name="Google",
        provider="Google GenerateContent",
        model="gemini-test",
        api_key="test-secret",
    )
    first = _extract_gemini_response(
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "text": "private thought summary",
                                "thought": True,
                                "thoughtSignature": "EjQ=",
                            },
                            {
                                "functionCall": {
                                    "id": "call-1",
                                    "name": "multiply",
                                    "args": {"a": 6, "b": 7},
                                },
                                "thoughtSignature": "Vng=",
                            }
                        ],
                    }
                }
            ]
        }
    )
    replay_payload = _build_gemini_payload(
        config,
        ChatRequest(
            message="6*7",
            turns=(
                first.turn,
                ToolResultTurn(
                    call_id="call-1",
                    name="multiply",
                    content='{"result":42}',
                ),
            ),
        ),
    )

    replay_contents = _google_sdk_contents(replay_payload, types)

    assert first.text == ""
    assert replay_contents[1].parts[0].thought is True
    assert replay_contents[1].parts[0].text == "private thought summary"
    assert replay_contents[1].parts[0].thought_signature == b"\x12\x34"
    assert replay_contents[1].parts[1].thought_signature == b"\x56\x78"
    assert replay_contents[2].parts[0].function_response.response == {
        "result": 42
    }


def test_responses_always_uses_local_history_and_store_false(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config_dir = tmp_path / "responses-local"
        _write_config(
            config_dir,
            "responses",
            "OpenAI Responses",
            other={
                "store": True,
                "previous_response_id": "forbidden-extra-id",
                "input": "forbidden-extra-input",
            },
            Extra_Body={"store": True},
        )
        payloads: list[dict] = []

        async def transport(provider, config, payload):
            assert provider == "responses"
            payloads.append(dict(payload))
            return _responses_text(f"ignored-{len(payloads)}", "ok")

        registry = ProviderRegistry(config_dir, transport=transport)
        await registry.chat("responses", "first")
        await registry.chat(
            "responses",
            "second",
            history=(
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "ok"},
            ),
        )

        assert len(payloads) == 2
        assert all(payload["store"] is False for payload in payloads)
        assert all("previous_response_id" not in payload for payload in payloads)
        assert payloads[0]["input"] == [
            {"role": "user", "content": "first"}
        ]
        assert payloads[1]["input"] == [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "second"},
        ]

    asyncio.run(scenario())


def test_responses_tool_round_uses_full_local_input(tmp_path: Path) -> None:
    async def scenario() -> None:
        config_dir = tmp_path / "responses-tools"
        _write_config(config_dir, "responses", "OpenAI Responses")
        payloads: list[dict] = []

        async def transport(provider, config, payload):
            payloads.append(dict(payload))
            if len(payloads) == 1:
                return {
                    "id": "ignored-tool-response-id",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call-1",
                            "name": "calculate_expression",
                            "arguments": '{"expression":"6*7"}',
                        }
                    ],
                }
            return _responses_text("ignored-final-response-id", "42")

        registry = ProviderRegistry(config_dir, transport=transport)
        declaration = FunctionTool(
            name="calculate_expression",
            description="Calculate",
            parameters={"type": "object", "properties": {}},
        )
        first = await registry.complete_request(
            "responses",
            ChatRequest(
                message="6*7",
                tools=(declaration,),
            ),
        )
        second = await registry.complete_request(
            "responses",
            ChatRequest(
                message="6*7",
                tools=(declaration,),
                turns=(
                    first.turn,
                    ToolResultTurn(
                        call_id="call-1",
                        name="calculate_expression",
                        content='{"result":42}',
                    ),
                ),
            ),
        )

        assert first.tool_calls[0].name == "calculate_expression"
        assert second.text == "42"
        assert all(payload["store"] is False for payload in payloads)
        assert all("previous_response_id" not in payload for payload in payloads)
        assert payloads[1]["input"] == [
            {"role": "user", "content": "6*7"},
            {
                "type": "function_call",
                "call_id": "call-1",
                "name": "calculate_expression",
                "arguments": '{"expression":"6*7"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": '{"result":42}',
            }
        ]

    asyncio.run(scenario())


def test_responses_transcribes_audio_and_expands_video_frames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_normalize(data: bytes, mime: str) -> ProcessedMedia:
        assert data
        assert mime.startswith("audio/")
        return ProcessedMedia(data=b"wave", mime="audio/wav")

    async def fake_video(data: bytes, mime: str) -> ProcessedVideo:
        assert data == b"video"
        assert mime == "video/mp4"
        return ProcessedVideo(
            frames=(ProcessedMedia(data=b"jpeg", mime="image/jpeg"),),
            audio=ProcessedMedia(data=b"wave", mime="audio/wav"),
            duration_seconds=12.5,
        )

    monkeypatch.setattr(
        "plugins.JianerAI.providers.normalize_audio",
        fake_normalize,
    )
    monkeypatch.setattr(
        "plugins.JianerAI.providers.process_video",
        fake_video,
    )

    async def scenario() -> None:
        config_dir = tmp_path / "responses-media"
        _write_config(
            config_dir,
            "responses",
            "OpenAI Responses",
            TranscriptionModel="transcribe-model",
        )
        payloads: list[tuple[str, dict]] = []

        async def transport(provider, config, payload):
            payloads.append((provider, dict(payload)))
            if provider == "transcription":
                assert payload["model"] == "transcribe-model"
                return {"text": "听到的内容"}
            return _responses_text("resp-media", "understood")

        registry = ProviderRegistry(config_dir, transport=transport)
        audio_answer = await registry.chat(
            "responses",
            "听一下",
            attachments=(
                MediaAttachment(data=b"audio", mime="audio/ogg"),
            ),
        )
        video_answer = await registry.chat(
            "responses",
            "看一下",
            attachments=(
                MediaAttachment(data=b"video", mime="video/mp4"),
            ),
        )

        assert audio_answer == "understood"
        assert video_answer == "understood"
        response_payloads = [
            payload for provider, payload in payloads if provider == "responses"
        ]
        audio_content = response_payloads[0]["input"][-1]["content"]
        assert "[语音转写]" in audio_content
        assert "听到的内容" in audio_content
        video_content = response_payloads[1]["input"][-1]["content"]
        assert "[视频画面]" in video_content[0]["text"]
        assert "[视频音轨转写]" in video_content[0]["text"]
        assert video_content[1]["type"] == "input_image"
        assert video_content[1]["image_url"].startswith(
            "data:image/jpeg;base64,"
        )

    asyncio.run(scenario())


def test_anthropic_messages_image_tools_and_unsupported_media(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config_dir = tmp_path / "anthropic"
        _write_config(
            config_dir,
            "claude",
            "Anthropic Messages",
            BaseUrl="https://anthropic-gateway.invalid/custom",
        )
        payloads: list[dict] = []

        async def transport(provider, config, payload):
            assert provider == "anthropic"
            assert config.base_url == "https://anthropic-gateway.invalid/custom"
            payloads.append(dict(payload))
            return {
                "content": [
                    {"type": "text", "text": "checking"},
                    {
                        "type": "tool_use",
                        "id": "toolu-1",
                        "name": "lookup",
                        "input": {"query": "value"},
                    },
                ]
            }

        registry = ProviderRegistry(config_dir, transport=transport)
        result = await registry.complete_request(
            "claude",
            ChatRequest(
                message="inspect",
                system_prompt="system",
                attachments=(
                    MediaAttachment(data=b"png", mime="image/png"),
                ),
                tools=(
                    FunctionTool(
                        name="lookup",
                        description="Lookup",
                        parameters={"type": "object", "properties": {}},
                    ),
                ),
            ),
        )

        assert result.text == "checking"
        assert result.tool_calls[0].id == "toolu-1"
        assert payloads[0]["system"] == "system"
        image = payloads[0]["messages"][-1]["content"][1]
        assert image["source"]["media_type"] == "image/png"
        assert payloads[0]["tools"][0]["input_schema"]["type"] == "object"

        with pytest.raises(MediaCapabilityError, match="不支持语音"):
            await registry.chat(
                "claude",
                "listen",
                attachments=(
                    MediaAttachment(data=b"audio", mime="audio/wav"),
                ),
            )

    asyncio.run(scenario())


def test_chat_rejects_video_and_google_embeds_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        config_dir = tmp_path / "video"
        _write_config(config_dir, "chat", "OpenAI Chat Completions")
        _write_config(config_dir, "google", "Google GenerateContent")
        payloads: list[dict] = []

        async def transport(provider, config, payload):
            payloads.append(dict(payload))
            return {
                "candidates": [
                    {"content": {"parts": [{"text": "video seen"}]}}
                ]
            }

        registry = ProviderRegistry(config_dir, transport=transport)
        video = MediaAttachment(data=b"video", mime="video/mp4")
        with pytest.raises(MediaCapabilityError, match="不支持视频"):
            await registry.chat("chat", "watch", attachments=(video,))

        answer = await registry.chat(
            "google",
            "watch",
            attachments=(video,),
        )
        assert answer == "video seen"
        inline = payloads[0]["contents"][-1]["parts"][1]["inlineData"]
        assert inline["mimeType"] == "video/mp4"

    asyncio.run(scenario())


def test_anthropic_sdk_receives_custom_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    class FakeMessages:
        async def create(self, **payload):
            captured["payload"] = payload
            return {"content": [{"type": "text", "text": "ok"}]}

    class FakeAsyncAnthropic:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.messages = FakeMessages()

        async def close(self):
            captured["closed"] = True

    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        SimpleNamespace(AsyncAnthropic=FakeAsyncAnthropic),
    )
    config = ModelConfig(
        key="claude",
        friendly_name="Claude",
        provider="Anthropic Messages",
        model="claude-test",
        api_key="secret",
        base_url="https://custom.invalid/anthropic",
    )

    asyncio.run(
        _default_anthropic_request(
            config,
            {
                "model": "claude-test",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 64,
            },
        )
    )

    assert captured["client"] == {
        "api_key": "secret",
        "base_url": "https://custom.invalid/anthropic",
        "timeout": 120.0,
    }
    assert captured["closed"] is True


def test_service_recognizes_video_segments_and_applies_video_policy() -> None:
    service = object.__new__(JianerAIService)
    segment = Segments.Video("https://cdn.example.test/clip.mp4")
    event = SimpleNamespace(message_id="message-1", message=(segment,))

    request = service._media_request(segment, event)

    assert request is not None
    assert request.media_kind is MediaKind.VIDEO
    policy = service._media_policy(request)
    assert policy.max_bytes == 20 * 1024 * 1024
    assert "video/mp4" in policy.allowed_mime_types
    assert service._has_media(event) is True
    assert service._segments_text((segment,)) == "[视频]"
