from __future__ import annotations

import builtins
import sys
from typing import Any

try:
    from Hyper import Configurator, Logger
except Exception:  # pragma: no cover
    Configurator = None
    Logger = None


class _FallbackLevels:
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    DEBUG = "DEBUG"


LOG_LEVELS = Logger.levels if Logger is not None else _FallbackLevels()
_LOGGER = None
_LEVEL_READY = False


def _ensure_console_encoding() -> None:
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


_ensure_console_encoding()


def get_logger():
    global _LOGGER, _LEVEL_READY
    if _LOGGER is not None:
        if not _LEVEL_READY:
            try:
                if Configurator and getattr(Configurator, "cm", None):
                    cfg = Configurator.cm.get_cfg()
                    level = getattr(cfg, "log_level", "INFO")
                    _LOGGER.set_level(level)
                    _LEVEL_READY = True
            except Exception:
                pass
        return _LOGGER

    if Logger is None:
        return None

    _LOGGER = Logger.Logger()
    try:
        if Configurator and getattr(Configurator, "cm", None):
            cfg = Configurator.cm.get_cfg()
            level = getattr(cfg, "log_level", "INFO")
            _LOGGER.set_level(level)
            _LEVEL_READY = True
    except Exception:
        pass
    return _LOGGER


def project_log(*args: Any, level: Any = None, sep: str = " ", end: str = "\n", **kwargs: Any) -> None:
    message = sep.join(str(a) for a in args)
    if end and end != "\n":
        message = f"{message}{end.rstrip()}"

    logger = get_logger()
    if logger is None:
        try:
            builtins.print(*args, sep=sep, end=end, **kwargs)
        except UnicodeEncodeError:
            safe = message.encode("gbk", errors="replace").decode("gbk", errors="replace")
            builtins.print(safe, **kwargs)
        return

    if level is None:
        try:
            logger.log(message)
        except UnicodeEncodeError:
            safe = message.encode("gbk", errors="replace").decode("gbk", errors="replace")
            builtins.print(safe, **kwargs)
    else:
        try:
            logger.log(message, level=level)
        except UnicodeEncodeError:
            safe = message.encode("gbk", errors="replace").decode("gbk", errors="replace")
            builtins.print(safe, **kwargs)
