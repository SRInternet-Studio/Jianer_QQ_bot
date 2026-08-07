from __future__ import annotations

import asyncio
import json

import pytest

from plugins.JianerAI.moderation import (
    ContentModerator,
    ModerationError,
    ModerationOptions,
    parse_moderation_decision,
)
from plugins.JianerAI.providers import MediaAttachment, ProviderRequestError


class FakeProvider:
    def __init__(
        self,
        response: str = "",
        *,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.response = response
        self.error = error
        self.delay = delay
        self.calls: list[dict[str, object]] = []

    async def chat(
        self,
        key,
        message,
        *,
        history=(),
        system_prompt="",
        attachments=(),
    ):
        self.calls.append(
            {
                "key": key,
                "message": message,
                "history": tuple(history),
                "system_prompt": system_prompt,
                "attachments": tuple(attachments),
            }
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.response


def test_content_moderator_allows_safe_request_with_bounded_context_and_media():
    async def scenario():
        provider = FakeProvider(
            '{"decision":"allow","categories":[],"reason":"正常科普",'
            '"refusal":""}'
        )
        moderator = ContentModerator(
            provider,
            options=ModerationOptions(
                model="review-model",
                max_context_messages=2,
                max_context_characters=40,
            ),
        )
        attachment = MediaAttachment(data=b"png", mime="image/png")
        persona = "你是一个温柔、耐心并且会自然说话的角色。" * 3
        decision = await moderator.review_request(
            "请解释这张医学示意图",
            persona=persona,
            history=(
                {"role": "user", "content": "最早的消息不应被发送"},
                {"role": "assistant", "content": "上一轮回答"},
                {"role": "user", "content": "当前上下文"},
            ),
            attachments=(attachment,),
        )

        assert decision.allowed is True
        assert decision.categories == ()
        assert len(provider.calls) == 1
        call = provider.calls[0]
        assert call["key"] == "review-model"
        assert call["history"] == ()
        assert call["attachments"] == (attachment,)
        assert "JianerAI content safety moderator" in str(
            call["system_prompt"]
        )
        payload = json.loads(str(call["message"]))
        assert payload["task"] == "review_current_user_request"
        assert payload["persona_template"] == persona
        assert len(payload["recent_context"]) <= 2
        assert "最早的消息不应被发送" not in str(payload)
        assert payload["current_request"]["attachments"] == [
            {"mime": "image/png", "size": 3}
        ]

    asyncio.run(scenario())


def test_content_moderator_returns_persona_refusal_for_disallowed_request():
    async def scenario():
        provider = FakeProvider(
            json.dumps(
                {
                    "decision": "refuse",
                    "categories": ["sexual_explicit"],
                    "reason": "请求生成露骨色情内容",
                    "refusal": "这种内容本姑娘可不写呀，换成含蓄的恋爱故事怎么样？",
                },
                ensure_ascii=False,
            )
        )
        moderator = ContentModerator(
            provider,
            options=ModerationOptions(model="review-model"),
        )

        decision = await moderator.review_request(
            "请求内容",
            persona="你习惯自称本姑娘，语气活泼但有原则。",
        )

        assert decision.refused is True
        assert decision.categories == ("sexual_explicit",)
        assert decision.refusal == (
            "这种内容本姑娘可不写呀，换成含蓄的恋爱故事怎么样？"
        )
        payload = json.loads(str(provider.calls[0]["message"]))
        assert "本姑娘" in payload["persona_template"]
        system_prompt = " ".join(
            str(provider.calls[0]["system_prompt"]).split()
        )
        assert "not like a generic safety assistant" in system_prompt
        assert "persona's identity" in system_prompt
        assert "preferred first-person reference" in system_prompt
        assert "at least two distinctive markers" in system_prompt

    asyncio.run(scenario())


def test_content_moderator_preserves_a_long_persona_template_by_default():
    async def scenario():
        provider = FakeProvider(
            '{"decision":"allow","categories":[],"reason":"",'
            '"refusal":""}'
        )
        moderator = ContentModerator(
            provider,
            options=ModerationOptions(model="review-model"),
        )
        persona = "角色开头：自称本姑娘。" + ("人设正文" * 5000) + "结尾口癖：哼哼。"

        decision = await moderator.review_request(
            "请解释光合作用",
            persona=persona,
        )

        assert decision.allowed is True
        payload = json.loads(str(provider.calls[0]["message"]))
        assert payload["persona_template"] == persona
        assert len(payload["persona_template"]) > 16000

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "value, error",
    [
        (
            '```json\n{"decision":"allow","categories":[],'
            '"reason":"","refusal":""}\n```',
            "raw JSON",
        ),
        (
            '{"decision":"allow","categories":["sexual_explicit"],'
            '"reason":"","refusal":""}',
            "allow decisions",
        ),
        (
            '{"decision":"refuse","categories":[],"reason":"",'
            '"refusal":"不能帮忙"}',
            "require categories",
        ),
        (
            '{"decision":"refuse","categories":["invented"],'
            '"reason":"","refusal":"不能帮忙"}',
            "unknown category",
        ),
        (
            '{"decision":"allow","categories":[],"reason":"",'
            '"refusal":"","extra":true}',
            "unknown fields",
        ),
        (
            '{"decision":"refuse","categories":["sexual_explicit"],'
            '"reason":"","refusal":"' + ("x" * 241) + '"}',
            "too long",
        ),
    ],
)
def test_moderation_parser_rejects_non_strict_decisions(value: str, error: str):
    with pytest.raises(ValueError, match=error):
        parse_moderation_decision(value)


def test_content_moderator_wraps_provider_failures_without_response_body():
    async def scenario():
        provider = FakeProvider(error=ProviderRequestError("upstream body"))
        moderator = ContentModerator(
            provider,
            options=ModerationOptions(model="review-model"),
        )

        with pytest.raises(ModerationError) as caught:
            await moderator.review_request("你好", persona="测试角色")

        assert caught.value.code == "moderation_provider_error"
        assert "upstream body" not in str(caught.value)

    asyncio.run(scenario())


def test_content_moderator_times_out():
    async def scenario():
        provider = FakeProvider(
            '{"decision":"allow","categories":[],"reason":"",'
            '"refusal":""}',
            delay=0.05,
        )
        moderator = ContentModerator(
            provider,
            options=ModerationOptions(
                model="review-model",
                timeout_seconds=0.001,
            ),
        )

        with pytest.raises(ModerationError) as caught:
            await moderator.review_request("你好", persona="测试角色")

        assert caught.value.code == "moderation_timeout"

    asyncio.run(scenario())
