"""帮助模式（图片/文本）的持久化与解析。"""
import json
import logging
import os

_logger = logging.getLogger(__name__)

HELP_MODE_FILE = "help_mode_settings.json"


def load_help_mode_settings() -> dict:
    if not os.path.exists(HELP_MODE_FILE):
        return {}
    try:
        with open(HELP_MODE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if k and v}
    except Exception:
        _logger.exception("load help mode settings failed")
    return {}


def save_help_mode_settings(settings: dict) -> bool:
    try:
        with open(HELP_MODE_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        _logger.exception("save help mode settings failed")
        return False


def normalize_help_mode(raw_mode: str) -> str | None:
    mode = str(raw_mode or "").strip().lower()
    if mode in {"图片", "图", "image", "img"}:
        return "图片"
    if mode in {"文本", "文字", "转发", "forward", "text"}:
        return "文本"
    return None
