"""Dedicated configuration file for JianerMate.

The plugin keeps its own JSON config (``data/jianer_mate/config.json`` by
default). Values from that file win over the legacy ``config.others``
entries, so operators can tune JianerMate without touching the host config.
The file is created on first activation with the currently effective values.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping


DEFAULT_CONFIG_PATH = "data/jianer_mate/config.json"
CONFIG_PATH_KEY = "jianer_mate_config_path"

_README = (
    "JianerMate 插件配置：这里的值优先于宿主 config.json 里的 jianer_mate_* 键。"
    "改动后发送 ~主动发言 重载 生效；模型代码来自 aiconfig/*.ai.json。"
)

_SCALAR_KEYS = {
    "enabled": "jianer_mate_enabled",
    "data_dir": "jianer_mate_data_dir",
    "db_path": "jianer_mate_db_path",
    "meme_dir": "jianer_mate_meme_dir",
    "ai_config_dir": "jianer_mate_ai_config_dir",
    "group_blacklist": "jianer_mate_group_blacklist",
}

_SECTION_KEYS: dict[str, dict[str, str]] = {
    "models": {
        "planner": "jianer_mate_planner_model",
        "replyer": "jianer_mate_replyer_model",
    },
    "moderation": {
        "enabled": "jianer_mate_moderation_enabled",
        "model": "jianer_mate_moderation_model",
    },
    "style": {
        "max_parts": "jianer_mate_reply_max_parts",
        "max_chars": "jianer_mate_reply_max_chars",
        "delay_ms": "jianer_mate_reply_delay_ms",
        "typo_probability": "jianer_mate_typo_probability",
        "typo_correction": "jianer_mate_typo_correction",
        "kaomoji_probability": "jianer_mate_kaomoji_probability",
        "kaomoji": "jianer_mate_kaomoji",
        "catchphrases": "jianer_mate_catchphrases",
    },
    "proactive": {
        "talk_value": "jianer_mate_talk_value_default",
        "min_interval_seconds": "jianer_mate_min_interval_seconds",
        "debounce_seconds": "jianer_mate_debounce_seconds",
        "max_debounce_seconds": "jianer_mate_max_debounce_seconds",
        "quiet_hours": "jianer_mate_quiet_hours_default",
        "mentioned_reply": "jianer_mate_mentioned_reply",
        "reply_at": "jianer_mate_reply_at",
        "backoff_base_seconds": "jianer_mate_backoff_base_seconds",
        "backoff_cap_seconds": "jianer_mate_backoff_cap_seconds",
        "backoff_start_count": "jianer_mate_backoff_start_count",
    },
    "context": {
        "messages": "jianer_mate_context_messages",
        "keep_messages": "jianer_mate_history_keep_messages",
        "retention_days": "jianer_mate_history_retention_days",
    },
    "memes": {
        "enabled": "jianer_mate_meme_enabled",
        "steal_emoji": "jianer_mate_steal_emoji",
        "max_entries": "jianer_mate_meme_max_entries",
        "max_size_mb": "jianer_mate_meme_max_size_mb",
        "max_dimension": "jianer_mate_meme_max_dimension",
        "candidates": "jianer_mate_meme_candidates",
        "thumbnail_px": "jianer_mate_meme_thumbnail_px",
        "reuse_cooldown_seconds": "jianer_mate_meme_reuse_cooldown_seconds",
    },
    "limits": {
        "max_concurrent_ai": "jianer_mate_max_concurrent_ai",
        "request_timeout_seconds": "jianer_mate_request_timeout_seconds",
    },
    "tools": {
        "enabled": "jianer_mate_tools_enabled",
        "max_rounds": "jianer_mate_tool_max_rounds",
        "include_web_browser": "jianer_mate_tool_include_web_browser",
        "allow_mutating": "jianer_mate_tool_allow_mutating",
        "allowlist": "jianer_mate_tool_allowlist",
        "max_output_chars": "jianer_mate_tool_max_output_chars",
        "skills_dirs": "jianer_mate_tool_skills_dirs",
        "skills_max_file_bytes": "jianer_mate_tool_skills_max_file_bytes",
    },
}


class PluginConfigError(RuntimeError):
    """Raised when the dedicated config file cannot be used."""


def resolve_config_path(project_root: Path, host_others: Mapping[str, Any]) -> Path:
    """Where the dedicated file lives; only the host config can move it."""

    raw = str(host_others.get(CONFIG_PATH_KEY) or "").strip()
    path = Path(raw or DEFAULT_CONFIG_PATH)
    if not path.is_absolute():
        path = project_root / path
    return path


def load_plugin_config(
    path: str | Path,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Return ``(document, errors)``; a missing file is not an error."""

    config_path = Path(path)
    if not config_path.is_file():
        return {}, {}
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        return {}, {str(config_path): f"读取失败: {exc.__class__.__name__}"}
    except json.JSONDecodeError as exc:
        return {}, {
            str(config_path): f"JSON 解析失败: 第 {exc.lineno} 行第 {exc.colno} 列"
        }
    if not isinstance(raw, dict):
        return {}, {str(config_path): "配置文件必须是 JSON 对象"}
    return raw, {}


def flatten_plugin_config(document: Mapping[str, Any]) -> dict[str, Any]:
    """Translate the nested document into ``jianer_mate_*`` host-style keys."""

    if not isinstance(document, Mapping):
        raise PluginConfigError("配置文件必须是 JSON 对象")
    flat: dict[str, Any] = {}
    for key, value in document.items():
        # Flat host-style keys are accepted for copy/paste convenience.
        if (
            isinstance(key, str)
            and key.startswith("jianer_mate_")
            and key != CONFIG_PATH_KEY
        ):
            flat[key] = value
    for source_key, target_key in _SCALAR_KEYS.items():
        if source_key in document and document[source_key] is not None:
            flat[target_key] = document[source_key]
    if document.get("persona") is not None:
        flat["jianer_mate_persona"] = document["persona"]
    for section_name, mapping in _SECTION_KEYS.items():
        section = document.get(section_name)
        if not isinstance(section, Mapping):
            continue
        for source_key, target_key in mapping.items():
            if source_key in section and section[source_key] is not None:
                flat[target_key] = section[source_key]
    return flat


def build_config_document(options: Any) -> dict[str, Any]:
    """Render the effective options as a nested, hand-editable document."""

    root = Path(getattr(options, "project_root", Path.cwd()))
    persona = getattr(options, "persona", None)
    style = getattr(options, "style", None)
    meme = getattr(options, "meme", None)
    tools = getattr(options, "tools", None)
    return {
        "_readme": _README,
        "enabled": bool(getattr(options, "enabled", True)),
        "data_dir": _portable(root, getattr(options, "data_dir", None)),
        "ai_config_dir": _portable(root, getattr(options, "ai_config_dir", None)),
        "models": {
            "planner": str(getattr(options, "planner_model", "") or ""),
            "replyer": str(getattr(options, "replyer_model", "") or ""),
        },
        "moderation": {
            "enabled": bool(getattr(options, "moderation_enabled", False)),
            "model": str(getattr(options, "moderation_model", "") or ""),
        },
        "persona": persona.to_payload() if persona is not None else {},
        "style": {
            "max_parts": int(getattr(style, "max_parts", 3)),
            "max_chars": int(getattr(style, "max_chars", 24)),
            "delay_ms": [
                int(getattr(style, "delay_min_ms", 600)),
                int(getattr(style, "delay_max_ms", 1800)),
            ],
            "typo_probability": float(getattr(style, "typo_probability", 0.08)),
            "typo_correction": bool(getattr(style, "typo_correction", True)),
            "kaomoji_probability": float(
                getattr(style, "kaomoji_probability", 0.15)
            ),
            "kaomoji": list(getattr(style, "kaomoji", ())),
            "catchphrases": list(getattr(style, "catchphrases", ())),
        },
        "proactive": {
            "talk_value": float(getattr(options, "talk_value_default", 0.5)),
            "min_interval_seconds": int(
                getattr(options, "min_interval_seconds", 30)
            ),
            "debounce_seconds": float(
                getattr(options, "debounce_seconds", 3.0)
            ),
            "max_debounce_seconds": float(
                getattr(options, "max_debounce_seconds", 15.0)
            ),
            "quiet_hours": str(
                getattr(options, "quiet_hours_default", "23:00-08:00")
            ),
            "mentioned_reply": bool(
                getattr(options, "mentioned_reply_enabled", True)
            ),
            "reply_at": bool(getattr(options, "reply_at_enabled", False)),
            "backoff_base_seconds": float(
                getattr(options, "backoff_base_seconds", 15.0)
            ),
            "backoff_cap_seconds": float(
                getattr(options, "backoff_cap_seconds", 300.0)
            ),
            "backoff_start_count": int(
                getattr(options, "backoff_start_count", 2)
            ),
        },
        "context": {
            "messages": int(getattr(options, "context_messages", 40)),
            "keep_messages": int(getattr(options, "history_keep_messages", 300)),
            "retention_days": int(getattr(options, "history_retention_days", 7)),
        },
        "memes": {
            "enabled": bool(getattr(meme, "enabled", True)),
            "steal_emoji": bool(getattr(meme, "steal_emoji", True)),
            "max_entries": int(getattr(meme, "max_entries", 128)),
            "max_size_mb": float(getattr(meme, "max_size_mb", 5.0)),
            "max_dimension": int(getattr(meme, "max_dimension", 4096)),
            "candidates": int(getattr(meme, "candidates", 8)),
            "thumbnail_px": int(getattr(meme, "thumbnail_px", 512)),
            "reuse_cooldown_seconds": int(
                getattr(meme, "reuse_cooldown_seconds", 3600)
            ),
        },
        "limits": {
            "max_concurrent_ai": int(
                getattr(options, "max_concurrent_ai", 2)
            ),
            "request_timeout_seconds": float(
                getattr(options, "request_timeout_seconds", 60.0)
            ),
        },
        "tools": {
            "enabled": bool(getattr(tools, "enabled", True)),
            "max_rounds": int(getattr(tools, "max_rounds", 3)),
            "include_web_browser": bool(
                getattr(tools, "include_web_browser", False)
            ),
            "allow_mutating": bool(
                getattr(tools, "allow_mutating", False)
            ),
            "allowlist": (
                None
                if getattr(tools, "allowlist", None) is None
                else list(getattr(tools, "allowlist", ()) or ())
            ),
            "max_output_chars": int(
                getattr(tools, "max_output_chars", 4000)
            ),
            "skills_dirs": list(getattr(tools, "skills_dirs", ()) or ()),
            "skills_max_file_bytes": int(
                getattr(tools, "skills_max_file_bytes", 65536)
            ),
        },
        "group_blacklist": list(getattr(options, "group_blacklist", ())),
    }


def write_config_file(path: str | Path, document: Mapping[str, Any]) -> bool:
    """Atomically write the config document; ``False`` on filesystem errors."""

    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(document, ensure_ascii=False, indent=4) + "\n"
        handle, raw_path = tempfile.mkstemp(
            prefix=target.name + ".", dir=str(target.parent)
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
            os.replace(raw_path, target)
        except Exception:
            Path(raw_path).unlink(missing_ok=True)
            raise
    except OSError:
        return False
    return True


def ensure_config_file(path: str | Path, options: Any) -> bool:
    """Create the config file with effective values when it is missing."""

    target = Path(path)
    if target.exists():
        return False
    return write_config_file(target, build_config_document(options))


def _portable(project_root: Path, value: Any) -> str:
    if value in (None, ""):
        return ""
    path = Path(str(value))
    try:
        return str(path.resolve().relative_to(Path(project_root).resolve()))
    except (OSError, ValueError):
        return str(path)
