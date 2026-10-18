"""Load the host config before importing JianerCore modules."""

from __future__ import annotations

from pathlib import Path

from cfgr.manager import Serializers
from jianer import configurator


_ROOT = Path(__file__).resolve().parents[3]
_CONFIG = _ROOT / "config.json"
if not _CONFIG.is_file():
    _CONFIG = _ROOT / "config.example.json"

configurator.BotConfig.load_from(
    str(_CONFIG),
    Serializers.JSON,
    "jianer-bot",
)

from bot import plugin_state  # noqa: E402 - needs the config loaded first

# The host sets this before loading plugins; tests must match so command
# patterns are built with the production prefix.
plugin_state.get_runtime()["reminder"] = "~"
