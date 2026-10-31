from __future__ import annotations

import asyncio

from plugins.JianerAI.providers import (
    _extract_gemini_response,
    _extract_openai_response,
    _extract_responses_response,
    _extract_usage,
)


def test_extract_usage_supports_chat_responses_deepseek_and_gemini() -> None:
    assert _extract_usage(
        {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 80},
            }
        }
    ) == (100, 20, 80)
    assert _extract_usage(
        {
            "usage": {
                "input_tokens": 120,
                "output_tokens": 30,
                "input_tokens_details": {"cached_tokens": 90},
            }
        }
    ) == (120, 30, 90)
    assert _extract_usage(
        {
            "usage": {
                "prompt_tokens": 50,
                "completion_tokens": 10,
                "prompt_cache_hit_tokens": 40,
            }
        }
    ) == (50, 10, 40)
    assert _extract_usage(
        {
            "usageMetadata": {
                "promptTokenCount": 64,
                "candidatesTokenCount": 8,
                "cachedContentTokenCount": 32,
            }
        }
    ) == (64, 8, 32)


def test_provider_responses_carry_usage_amounts() -> None:
    openai = asyncio.run(
        _extract_openai_response(
            {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 80},
                },
            }
        )
    )
    assert (openai.prompt_tokens, openai.completion_tokens) == (100, 20)
    assert openai.cached_prompt_tokens == 80
    assert openai.cache_hit_ratio == 0.8

    responses = _extract_responses_response(
        {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "ok"}],
                }
            ],
            "usage": {
                "input_tokens": 120,
                "output_tokens": 30,
                "input_tokens_details": {"cached_tokens": 90},
            },
        }
    )
    assert (responses.prompt_tokens, responses.completion_tokens) == (120, 30)
    assert responses.cached_prompt_tokens == 90

    gemini = _extract_gemini_response(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": "ok"}],
                    }
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 64,
                "candidatesTokenCount": 8,
                "cachedContentTokenCount": 32,
            },
        }
    )
    assert (gemini.prompt_tokens, gemini.completion_tokens) == (64, 8)
    assert gemini.cached_prompt_tokens == 32
