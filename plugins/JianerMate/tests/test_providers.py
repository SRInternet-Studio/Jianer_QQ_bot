import asyncio

import pytest

from plugins.JianerMate.providers import (
    ChatImage,
    ModelEntry,
    OpenAICompatibleClient,
    OPENAI_CHAT_COMPLETIONS,
    ProviderError,
    ResponseParseError,
    extract_json_object,
    load_models,
    normalize_response_type,
    parse_json_object,
)


def test_response_type_normalisation():
    assert normalize_response_type("OpenAI") == OPENAI_CHAT_COMPLETIONS
    assert normalize_response_type("chat_completions") == OPENAI_CHAT_COMPLETIONS
    assert normalize_response_type("Gemini") == "google_generate_content"
    assert normalize_response_type("Anthropic Messages") == "anthropic_messages"


def test_load_models_marks_unsupported_entries(tmp_path):
    (tmp_path / "gpt.ai.json").write_text(
        '{"FriendlyName": "GPT", "Model": "gpt-4o", "ResponseType": "OpenAI",'
        ' "ApiKey": "sk-test", "BaseUrl": "https://api.example.com/v1"}',
        encoding="utf-8",
    )
    (tmp_path / "gemini.ai.json").write_text(
        '{"Model": "gemini-2", "ResponseType": "Google GenerateContent",'
        ' "ApiKey": "x"}',
        encoding="utf-8",
    )
    (tmp_path / "broken.ai.json").write_text("{", encoding="utf-8")

    entries, errors = load_models(tmp_path)
    assert entries["gpt"].supported is True
    assert entries["gpt"].endpoint == "https://api.example.com/v1/chat/completions"
    assert entries["gemini"].supported is False
    assert "不在本插件支持范围内" in entries["gemini"].unsupported_reason
    assert "broken.ai.json" in errors


def test_extract_json_object_handles_fences_and_nesting():
    payload = parse_json_object(
        '当然可以：\n```json\n{"speak": true, "topic": "天气 {今天}",'
        ' "nested": {"a": 1}}\n```'
    )
    assert payload["speak"] is True
    assert payload["topic"] == "天气 {今天}"
    with pytest.raises(ValueError):
        extract_json_object("no json here")


class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []
        self.closed = False

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        return self.responses.pop(0)

    async def close(self):
        self.closed = True


def _entry(**overrides):
    values = {
        "key": "test",
        "friendly_name": "Test",
        "response_type": OPENAI_CHAT_COMPLETIONS,
        "model": "test-model",
        "api_key": "sk-test",
        "base_url": "https://api.example.com/v1",
    }
    values.update(overrides)
    return ModelEntry(**values)


def test_client_inlines_images_and_retries_without_json_mode():
    async def scenario():
        session = _FakeSession(
            [
                _FakeResponse(400, '{"error": "response_format unsupported"}'),
                _FakeResponse(
                    200,
                    '{"choices": [{"message": {"content": "{\\"speak\\": true}"}}]}',
                ),
            ]
        )
        client = OpenAICompatibleClient(
            {"test": _entry()}, session=session, max_retries=1
        )
        payload = await client.complete_json(
            "test",
            system_prompt="sys",
            user_text="hello",
            images=[ChatImage(data=b"\x89PNG", mime="image/png")],
        )
        assert payload == {"speak": True}
        assert session.posts[0]["json"]["response_format"] == {"type": "json_object"}
        assert "response_format" not in session.posts[1]["json"]
        content = session.posts[0]["json"]["messages"][1]["content"]
        assert content[0]["type"] == "text"
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        await client.close()
        # An injected session belongs to the caller and must not be closed.
        assert session.closed is False

    asyncio.run(scenario())


def test_client_rejects_unsupported_models():
    async def scenario():
        client = OpenAICompatibleClient({}, session=_FakeSession([]))
        with pytest.raises(Exception):
            await client.complete_text(
                "missing", system_prompt="s", user_text="u"
            )

    asyncio.run(scenario())


def test_client_reports_http_errors_with_status_and_retries():
    async def scenario():
        session = _FakeSession(
            [
                _FakeResponse(500, "upstream exploded"),
                _FakeResponse(
                    200, '{"choices": [{"message": {"content": "ok"}}]}'
                ),
            ]
        )
        client = OpenAICompatibleClient(
            {"test": _entry()}, session=session, max_retries=1
        )
        text = await client.complete_text(
            "test", system_prompt="s", user_text="u"
        )
        assert text == "ok"
        assert len(session.posts) == 2

        failing = OpenAICompatibleClient(
            {"test": _entry()},
            session=_FakeSession([_FakeResponse(500, "boom")]),
            max_retries=0,
        )
        with pytest.raises(ProviderError) as captured:
            await failing.complete_text(
                "test", system_prompt="s", user_text="u"
            )
        assert captured.value.status == 500

    asyncio.run(scenario())


def test_client_wraps_unparsable_json():
    async def scenario():
        session = _FakeSession(
            [
                _FakeResponse(
                    200,
                    '{"choices": [{"message": {"content": "不是 JSON"}}]}',
                )
            ]
        )
        client = OpenAICompatibleClient(
            {"test": _entry()}, session=session, max_retries=0
        )
        with pytest.raises(ResponseParseError):
            await client.complete_json(
                "test", system_prompt="s", user_text="u"
            )

    asyncio.run(scenario())
