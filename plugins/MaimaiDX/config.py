"""Configuration bridge for the JianerCore MaimaiDX plugin."""

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from bot import plugin_state

from .core.lxns_oauth import LXNS_OOB_REDIRECT_URI


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = PROJECT_ROOT / ".env"
_FILE_VALUES = dotenv_values(_ENV_FILE)
_COLOR_TAG = re.compile(r"</?[a-zA-Z]+>")


def _value(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    if raw is None:
        raw = _FILE_VALUES.get(name, default)
    if raw is None:
        return default
    return str(raw).strip()


def _optional(name: str) -> str | None:
    value = _value(name)
    return value or None


def _boolean(name: str, default: bool) -> bool:
    raw = _value(name, "true" if default else "false").lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _resource_path() -> Path:
    raw = _value("MAIMAIDX_PATH", "data/maimaidx/static")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _state_path() -> Path:
    raw = _value("MAIMAIDX_STATE_PATH", "data/maimaidx/private")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


class LoggerCompat:
    """Expose the small Loguru surface retained by the upstream core."""

    def __init__(self, logger: Any | None = None) -> None:
        self._logger = logger or logging.getLogger("jianer.maimaidx")

    def opt(self, **_: Any) -> "LoggerCompat":
        return self

    def success(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self.info(message, *args, **kwargs)

    def debug(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._call("debug", message, *args, **kwargs)

    def info(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._call("info", message, *args, **kwargs)

    def warning(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._call("warning", message, *args, **kwargs)

    def error(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._call("error", message, *args, **kwargs)

    def exception(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._call("exception", message, *args, **kwargs)

    def _call(self, level: str, message: Any, *args: Any, **kwargs: Any) -> None:
        method = getattr(self._logger, level, None)
        if not callable(method) and level == "exception":
            method = getattr(self._logger, "error", None)
        if not callable(method):
            return
        text = _COLOR_TAG.sub("", str(message))
        try:
            method(text, *args, **kwargs)
        except TypeError:
            method(text)


@dataclass(frozen=True)
class MaimaiConfig:
    maimaidx_path: str
    state_path: str
    maimaidx_alias_proxy: bool
    maimaidx_alias_push: bool
    save_in_memory: bool
    assets_online: bool
    bot_name: str
    bot_name_en: str


@dataclass(frozen=True)
class DivingFishConfig:
    divingfish_prober_proxy: bool
    divingfish_token: str | None


@dataclass(frozen=True)
class LxnsConfig:
    lxns_dev_token: str | None
    lx_client_id: str | None
    lx_client_secret: str | None
    redirect_uri: str | None
    lxns_bind_private_only: bool


_runtime = plugin_state.get_runtime()
log = LoggerCompat(plugin_state.get_logger())
_bot_name = str(_runtime.get("bot_name") or "Jianer")
maiconfig = MaimaiConfig(
    maimaidx_path=str(_resource_path()),
    state_path=str(_state_path()),
    maimaidx_alias_proxy=_boolean("MAIMAIDX_ALIAS_PROXY", False),
    maimaidx_alias_push=_boolean("MAIMAIDX_ALIAS_PUSH", True),
    save_in_memory=_boolean("SAVE_IN_MEMORY", True),
    assets_online=_boolean("ASSETS_ONLINE", True),
    bot_name=_bot_name,
    bot_name_en=str(_runtime.get("bot_name_en") or _bot_name),
)
dfconfig = DivingFishConfig(
    divingfish_prober_proxy=_boolean("DIVINGFISH_PROBER_PROXY", False),
    divingfish_token=_optional("DIVINGFISH_TOKEN"),
)
lxnsconfig = LxnsConfig(
    lxns_dev_token=_optional("LXNS_DEV_TOKEN"),
    lx_client_id=_optional("LX_CLIENT_ID"),
    lx_client_secret=_optional("LX_CLIENT_SECRET"),
    redirect_uri=_optional("REDIRECT_URI") or LXNS_OOB_REDIRECT_URI,
    lxns_bind_private_only=_boolean("LXNS_BIND_PRIVATE_ONLY", True),
)
