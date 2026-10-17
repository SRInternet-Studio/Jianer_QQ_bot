"""Plugin configuration loading for the RollPigPlus modules.

Configuration comes from ``ROLLPIG_*`` environment variables and from the
optional ``rollpig_config.json`` (or ``config/rollpig.json``) file that the
upstream ``Config`` model already merges by itself.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

from .logger import logger


def _env_data() -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper.startswith("ROLLPIG_"):
            data[upper.lower()] = value
    return data


def get_plugin_config(model: Any) -> Any:
    """Instantiate the plugin config model from environment settings."""

    data = _env_data()
    try:
        return model(**data)
    except Exception as error:
        logger.warning(f"rollpig: 插件配置解析失败，使用默认值: {error}")
        try:
            return model()
        except Exception:
            return SimpleNamespace(**data)


__all__ = ["get_plugin_config"]
