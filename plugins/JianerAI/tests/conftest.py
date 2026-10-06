"""Load JianerCore configuration before importing the plugin modules."""

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
