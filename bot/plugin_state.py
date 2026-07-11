"""Shared state for JianerCore new-style plugins."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from jianer.plugins import PluginManager
from jianer.plugins.builtin import alconna as alconna_plugin

PLUGIN_FOLDER = "plugins"
DISABLED_PREFIX = "d_"

_config: Any = None
_logger: Any = None
_plugin_manager: PluginManager | None = None
_load_result: Any = None
_disabled_plugins: list[str] = []
_help_text = ""

_runtime: dict[str, Any] = {
    "reminder": "",
    "bot_name": "",
    "bot_name_en": "",
    "one_slogan": "",
    "confused_word": "",
    "root_users": [],
    "super_users": [],
    "manage_users": [],
    "admins": [],
    "supers": [],
    "cooldowns": {},
    "cooldowns1": {},
    "generating": False,
}

def configure(
    *,
    config: Any,
    logger: Any,
    reminder: str,
    bot_name: str,
    bot_name_en: str,
    one_slogan: str,
    confused_word: str,
    root_users: list,
    cooldowns: dict,
    cooldowns1: dict,
) -> None:
    global _config, _logger
    _config = config
    _logger = logger
    _runtime.update(
        {
            "reminder": reminder,
            "bot_name": bot_name,
            "bot_name_en": bot_name_en,
            "one_slogan": one_slogan,
            "confused_word": confused_word,
            "root_users": root_users,
            "cooldowns": cooldowns,
            "cooldowns1": cooldowns1,
        }
    )


def get_config() -> Any:
    return _config


def get_logger() -> Any:
    return _logger


def get_runtime() -> dict[str, Any]:
    return _runtime


def set_auth_snapshot(
    admins: list[str],
    supers: list[str],
    root_users: list,
    super_users: list,
    manage_users: list,
) -> None:
    _runtime.update(
        {
            "admins": [str(item) for item in admins],
            "supers": [str(item) for item in supers],
            "root_users": root_users,
            "super_users": super_users,
            "manage_users": manage_users,
        }
    )


def set_generating(value: bool) -> None:
    _runtime["generating"] = bool(value)


def is_generating() -> bool:
    return bool(_runtime.get("generating", False))


def get_connection() -> Any:
    config = get_config()
    if config is None:
        return None
    getter = getattr(config, "get_connection", None)
    if callable(getter):
        return getter()
    return getattr(config, "connection", None)


def websocket_url() -> str:
    connection = get_connection()
    host = getattr(connection, "host", None)
    port = getattr(connection, "port", None)
    if host is None and isinstance(connection, dict):
        host = connection.get("host")
        port = connection.get("port")
    return f"ws://{host}:{port}"


def reload_plugins(logger: Any | None = None):
    global _plugin_manager, _load_result, _disabled_plugins, _help_text
    # Command matchers live in the built-in Alconna plugin. Clear registrations
    # from the previous load so the project's runtime reload remains idempotent.
    alconna_plugin._clear_matchers()
    manager = PluginManager(logger=logger or _logger)
    result = manager.load_plugins(PLUGIN_FOLDER)
    _plugin_manager = manager
    _load_result = result
    _disabled_plugins = _scan_disabled_plugins()
    _help_text = _render_help_text(result)
    return result


def get_plugin_manager() -> PluginManager | None:
    return _plugin_manager


def get_load_result() -> Any:
    return _load_result


def loaded_plugins() -> list[str]:
    if _load_result is None:
        return []
    return list(getattr(_load_result, "loaded", []))


def disabled_plugins() -> list[str]:
    return list(_disabled_plugins)


def failed_plugins() -> list[str]:
    if _load_result is None:
        return []
    return list(getattr(_load_result, "failed", []))


def plugin_warnings() -> list[str]:
    if _load_result is None:
        return []
    return list(getattr(_load_result, "warnings", []))


def plugin_help_text() -> str:
    return _help_text


async def dispatch_plugins(
    event: Any,
    actions: Any,
    *,
    message_text: str | None = None,
) -> bool:
    manager = get_plugin_manager()
    if manager is None:
        return False
    dispatch_event = (
        _MessageTextEventProxy(event, message_text)
        if message_text is not None
        else event
    )
    return await manager.dispatch(dispatch_event, actions)


def get_plugin_module(plugin_id: str) -> Any | None:
    manager = get_plugin_manager()
    if manager is None:
        return None
    plugin = manager.plugins.get(plugin_id)
    return getattr(plugin, "module", None)


class _MessageTextEventProxy:
    """Override command text while retaining the adapter event interface."""

    def __init__(self, event: Any, message_text: str) -> None:
        self._event = event
        self.msg_str = message_text

    def __getattr__(self, name: str) -> Any:
        return getattr(self._event, name)


def find_plugin_path(plugin_name: str, *, enable: bool) -> Path | None:
    folder = Path(PLUGIN_FOLDER).resolve()
    wanted = plugin_name.strip()
    if not wanted:
        return None

    candidates: list[Path] = []
    names = {wanted}
    if wanted.startswith("jianerbot-plugin-"):
        names.add(wanted.removeprefix("jianerbot-plugin-"))
    names.add(wanted.replace("-", "_"))

    if enable:
        prefixes = [DISABLED_PREFIX]
    else:
        prefixes = [""]

    for base in names:
        for prefix in prefixes:
            candidates.extend(
                [
                    folder / f"{prefix}{base}.py",
                    folder / f"{prefix}{base}.pyw",
                    folder / f"{prefix}{base}",
                ]
            )

    for candidate in candidates:
        if candidate.exists():
            return candidate

    for entry in folder.iterdir() if folder.exists() else []:
        if enable and not entry.name.startswith(DISABLED_PREFIX):
            continue
        if not enable and entry.name.startswith(DISABLED_PREFIX):
            continue
        metadata_name = _read_metadata_name(_entry_file(entry))
        if metadata_name == wanted:
            return entry
    return None


def _scan_disabled_plugins() -> list[str]:
    folder = Path(PLUGIN_FOLDER)
    if not folder.exists():
        return []
    disabled = []
    for entry in folder.iterdir():
        if entry.name.startswith(DISABLED_PREFIX):
            disabled.append(_display_name(entry.name[len(DISABLED_PREFIX) :]))
    return disabled


def _render_help_text(result: Any) -> str:
    lines = []
    plugin_map = getattr(result, "plugin_map", {}) or {}
    for plugin_id in getattr(result, "dependency_order", []) or []:
        if plugin_id == "jianerbot-plugin-alconna":
            continue
        plugin = plugin_map.get(plugin_id)
        metadata = getattr(plugin, "metadata", None)
        usage = getattr(metadata, "usage", "") if metadata else ""
        description = getattr(metadata, "description", "") if metadata else ""
        text = usage or description
        if text:
            lines.extend(str(text).splitlines())
    reminder = _runtime.get("reminder", "")
    return "".join(
        f"\n       {line.strip().replace('{reminder}', reminder)}"
        for line in lines
        if line.strip()
    )


def _display_name(filename: str) -> str:
    return Path(filename).stem


def _entry_file(entry: Path) -> Path:
    if entry.is_dir():
        return entry / "setup.py"
    return entry


def _read_metadata_name(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__plugin_meta__"
            for target in node.targets
        ):
            continue
        value = node.value
        if not isinstance(value, ast.Call):
            return None
        if value.args:
            try:
                name = ast.literal_eval(value.args[0])
                return str(name)
            except (ValueError, TypeError):
                return None
        for keyword in value.keywords:
            if keyword.arg == "name":
                try:
                    name = ast.literal_eval(keyword.value)
                    return str(name)
                except (ValueError, TypeError):
                    return None
    return None
