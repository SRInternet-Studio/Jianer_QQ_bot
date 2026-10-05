"""Load the JianerCore bot config before tests import the framework.

``jianer.events`` / ``jianer.hyperogger`` read ``BotConfig.get("jianer-bot")``
at import time, so any test that imports ``bot.plugin_state`` needs the config
to be loaded first.  Prefer the local ``config.json`` and fall back to the
tracked example so a clean checkout can still run the suite.
"""

from __future__ import annotations

from pathlib import Path

from cfgr.manager import Serializers

from jianer import configurator

_ROOT = Path(__file__).resolve().parents[1]
_CONFIG = _ROOT / "config.json"
if not _CONFIG.is_file():
    _CONFIG = _ROOT / "config.example.json"

configurator.BotConfig.load_from(
    str(_CONFIG),
    Serializers.JSON,
    "jianer-bot",
)
