import random
import time
from dataclasses import replace

from plugins.JianerMate.config import (
    MemeOptions,
    RuntimeOptions,
    StyleOptions,
)
from plugins.JianerMate.service import in_quiet_hours, parse_hours_window, valid_hours_window
from plugins.JianerMate import style as mate_style


class _Config:
    black_list: list = []


def _runtime(others=None):
    values = {"jianer_mate_config_path": ".pytest-missing-mate-config.json"}
    values.update(others or {})
    config = _Config()
    config.others = values
    return {"config": config, "reminder": "~"}


def test_defaults_without_extra_configuration():
    options = RuntimeOptions.from_runtime(_runtime())
    assert options.enabled is True
    assert options.talk_value_default == 0.5
    assert options.min_interval_seconds == 30
    assert options.quiet_hours_default == "23:00-08:00"
    assert options.style.max_parts == 3
    assert options.style.max_chars == 24
    assert options.meme.max_entries == 128
    assert options.persona.name == "简儿"


def test_configuration_overrides_and_clamping(tmp_path):
    options = RuntimeOptions.from_runtime(
        _runtime(
            {
                "bot_name": "小简",
                "jianer_mate_data_dir": str(tmp_path / "mate"),
                "jianer_mate_planner_model": "planner",
                "jianer_mate_replyer_model": "vision",
                "jianer_mate_persona": {
                    "name": "阿简",
                    "personality": "话不多",
                    "behavior_style": "只在被点名时说话",
                    "reply_style": "最多两个字",
                },
                "jianer_mate_reply_delay_ms": {"min": 1000, "max": 200},
                "jianer_mate_reply_max_parts": 99,
                "jianer_mate_talk_value_default": 2.5,
                "jianer_mate_quiet_hours_default": "01:00-05:00",
                "jianer_mate_group_blacklist": ["123", "456"],
                "jianer_mate_meme_max_entries": 4,
            }
        )
    )
    assert options.persona.name == "阿简"
    assert options.persona.personality == "话不多"
    assert options.planner_model == "planner"
    assert options.replyer_model == "vision"
    assert options.style.delay_min_ms == 1000
    assert options.style.delay_max_ms == 1000
    assert options.style.max_parts == 6
    assert options.talk_value_default == 1.0
    assert options.quiet_hours_default == "01:00-05:00"
    assert options.group_blacklist == ("123", "456")
    assert options.meme.max_entries == 4
    assert options.database_path.parent == tmp_path / "mate"


def test_quiet_hours_windows():
    assert parse_hours_window("23:00-08:00") == (23 * 60, 8 * 60)
    assert parse_hours_window("关闭") is None
    assert parse_hours_window("25:00-08:00") is None
    assert valid_hours_window("00:30-06:00")
    assert not valid_hours_window("8-9")

    night = time.mktime((2026, 10, 18, 1, 30, 0, 0, 0, -1))
    noon = time.mktime((2026, 10, 18, 12, 0, 0, 0, 0, -1))
    assert in_quiet_hours("23:00-08:00", now=night) is True
    assert in_quiet_hours("23:00-08:00", now=noon) is False
    assert in_quiet_hours("关闭", now=night) is False
    assert in_quiet_hours("12:00-13:00", now=noon) is True


def test_sanitize_messages_shortens_and_strips_markdown():
    options = StyleOptions(max_parts=3, max_chars=10)
    raw = [
        "**你好**，今天天气不错。要不要一起去吃饭？",
        "```\nprint(1)\n```",
        "- 第三条",
        "- 第四条会被丢弃",
    ]
    messages = mate_style.sanitize_messages(raw, options)
    assert messages
    assert len(messages) <= 3
    assert all(len(message) <= options.max_chars * 3 for message in messages)
    assert all("**" not in message and "```" not in message for message in messages)
    assert all(not message.startswith("- ") for message in messages)


def test_typo_injection_skips_protected_spans():
    rng = random.Random(1)
    text, changed = mate_style.inject_typo(
        "你好 https://example.com/a 在吗 @12345 （好的） 123456",
        probability=1.0,
        rng=rng,
    )
    assert changed is True
    assert "https://example.com/a" in text
    assert "@12345" in text
    assert "（好的）" in text
    assert "123456" in text
    assert text != "你好 https://example.com/a 在吗 @12345 （好的） 123456"


def test_typo_injection_disabled_and_decorated():
    rng = random.Random(2)
    text, changed = mate_style.inject_typo("你好", probability=0.0, rng=rng)
    assert (text, changed) == ("你好", False)

    options = replace(
        StyleOptions(),
        kaomoji_probability=1.0,
        kaomoji=("ovo",),
        catchphrases=("好耶",),
    )
    decorated = mate_style.decorate("好", options, rng=random.Random(3))
    assert "好" in decorated
    assert decorated != "好"


def test_sample_delay_respects_bounds():
    options = StyleOptions(delay_min_ms=100, delay_max_ms=200)
    rng = random.Random(4)
    for _ in range(20):
        assert 100 <= mate_style.sample_delay_ms(options, rng=rng) <= 200
    fixed = StyleOptions(delay_min_ms=50, delay_max_ms=50)
    assert mate_style.sample_delay_ms(fixed, rng=rng) == 50


def test_meme_options_size_bytes():
    options = MemeOptions(max_size_mb=2.0)
    assert options.max_size_bytes == 2 * 1024 * 1024
