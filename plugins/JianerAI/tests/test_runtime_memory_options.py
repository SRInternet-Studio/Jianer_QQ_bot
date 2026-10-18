from __future__ import annotations

from types import SimpleNamespace

from plugins.JianerAI.providers import _provider_error_detail
from plugins.JianerAI.service import RuntimeOptions


def test_episode_segmentation_model_option_is_configurable() -> None:
    options = RuntimeOptions.from_runtime(
        {
            "config": SimpleNamespace(
                others={"memory_episode_segmentation_model": "grok"}
            )
        }
    )
    assert options.memory_episode_segmentation_model == "grok"


def test_episode_segmentation_model_defaults_to_auto() -> None:
    options = RuntimeOptions.from_runtime(
        {"config": SimpleNamespace(others={})}
    )
    assert options.memory_episode_segmentation_model == "auto"


def test_provider_error_detail_includes_status_and_type() -> None:
    class _StatusError(Exception):
        status_code = 503

    class _PlainError(Exception):
        pass

    assert _provider_error_detail(_StatusError()) == " (HTTP 503, _StatusError)"
    assert _provider_error_detail(_PlainError()) == " (_PlainError)"
