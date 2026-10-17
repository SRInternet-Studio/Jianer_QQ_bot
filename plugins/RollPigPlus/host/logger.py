"""Logging bridge for the RollPigPlus modules.

The ported modules were written against loguru's ``logger``; this adapter
forwards every call to the JianerCore host logger.
"""

from __future__ import annotations

import logging
from typing import Any

from bot import plugin_state

_FALLBACK = logging.getLogger("rollpig")
if not _FALLBACK.handlers:
    _FALLBACK.addHandler(logging.NullHandler())


class _HostLogger:
    def _logger(self) -> Any | None:
        try:
            return plugin_state.get_logger()
        except Exception:
            return None

    def _emit(self, level: str, message: Any, *args: Any) -> None:
        text = str(message)
        if args:
            try:
                text = text % args
            except (TypeError, ValueError):
                text = " ".join([text, *(str(item) for item in args)])
        logger = self._logger()
        method = getattr(logger, level, None) if logger is not None else None
        if callable(method):
            try:
                if level == "exception":
                    method(text)
                else:
                    method(text)
                return
            except Exception:
                pass
        fallback_level = "error" if level == "exception" else level
        getattr(_FALLBACK, fallback_level if fallback_level != "success" else "info")(text)

    def debug(self, message: Any, *args: Any) -> None:
        self._emit("debug", message, *args)

    def info(self, message: Any, *args: Any) -> None:
        self._emit("info", message, *args)

    def success(self, message: Any, *args: Any) -> None:
        self._emit("info", message, *args)

    def warning(self, message: Any, *args: Any) -> None:
        self._emit("warning", message, *args)

    warn = warning

    def error(self, message: Any, *args: Any) -> None:
        self._emit("error", message, *args)

    def exception(self, message: Any, *args: Any) -> None:
        self._emit("exception", message, *args)

    def critical(self, message: Any, *args: Any) -> None:
        self._emit("critical", message, *args)

    def trace(self, message: Any, *args: Any) -> None:
        self._emit("debug", message, *args)


logger = _HostLogger()

__all__ = ["logger"]
