from __future__ import annotations

from plugins.JianerAI.providers import ModelConfig, _history_messages


def _config(max_history_chars: int) -> ModelConfig:
    return ModelConfig(
        key="demo",
        friendly_name="demo",
        provider="OpenAI Chat Completions",
        model="demo",
        api_key="test",
        max_history_messages=20,
        max_history_chars=max_history_chars,
    )


def test_history_keeps_newest_messages_within_character_budget():
    history = [
        {"role": "user", "content": "old-" + "x" * 20},
        {"role": "assistant", "content": "middle-" + "y" * 10},
        {"role": "user", "content": "newest"},
    ]
    result = _history_messages(_config(15), history)
    assert [item["content"] for item in result] == ["newest"]


def test_history_clips_a_single_oversized_message_to_newest_tail():
    history = [{"role": "user", "content": "0123456789abcdef"}]
    result = _history_messages(_config(5), history)
    assert result == [{"role": "user", "content": "bcdef"}]


def test_history_budget_zero_disables_character_limit():
    history = [{"role": "user", "content": "x" * 100}]
    assert _history_messages(_config(0), history) == history
