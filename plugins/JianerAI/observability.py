from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote, quote_plus


_REDACTED = "[REDACTED]"
_SENSITIVE_KEY_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "passwd",
    "password",
    "private_key",
    "secret",
    "token",
)
_BEARER_PATTERN = re.compile(
    r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+"
)
_COMMON_TOKEN_PATTERN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|AIza[A-Za-z0-9_-]{20,})\b"
)


def sanitize_log_data(
    value: Any,
    *,
    sensitive_values: Sequence[str] | set[str] = (),
    tool_name: str = "",
) -> Any:
    secrets = tuple(
        sorted(
            {str(item) for item in sensitive_values if str(item)},
            key=len,
            reverse=True,
        )
    )
    return _sanitize(value, secrets=secrets, tool_name=str(tool_name))


def format_log_data(
    value: Any,
    *,
    sensitive_values: Sequence[str] | set[str] = (),
    tool_name: str = "",
    max_chars: int = 6000,
) -> str:
    sanitized = sanitize_log_data(
        value,
        sensitive_values=sensitive_values,
        tool_name=tool_name,
    )
    try:
        output = json.dumps(
            sanitized,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        output = json.dumps(str(sanitized), ensure_ascii=False)
    limit = max(256, int(max_chars))
    if len(output) <= limit:
        return output
    omitted = len(output) - limit
    return f"{output[:limit]}…<truncated {omitted} chars>"


def safe_log_info(logger: Any, message: str) -> None:
    method = getattr(logger, "info", None)
    if not callable(method):
        return
    try:
        method(str(message))
    except Exception:
        # Logging must never interrupt a conversation or tool execution.
        return


def _sanitize(value: Any, *, secrets: tuple[str, ...], tool_name: str) -> Any:
    if isinstance(value, Mapping):
        source = dict(value)
        if (
            tool_name == "web_browser"
            and str(source.get("action") or "").casefold() == "fill"
            and "value" in source
        ):
            source["value"] = _REDACTED
        if tool_name == "render_information_card" and "html" in source:
            raw_html = str(source.get("html") or "")
            digest = hashlib.sha256(raw_html.encode("utf-8")).hexdigest()[:12]
            source["html"] = f"<html:{len(raw_html)} chars sha256:{digest}>"
        if tool_name == "bash":
            for key in ("command", "output"):
                raw_value = source.get(key)
                if isinstance(raw_value, str):
                    digest = hashlib.sha256(raw_value.encode("utf-8")).hexdigest()[:12]
                    source[key] = f"<{key}:{len(raw_value)} chars sha256:{digest}>"
        output: dict[str, Any] = {}
        for key, item in source.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                output[key_text] = _REDACTED
            else:
                output[key_text] = _sanitize(
                    item,
                    secrets=secrets,
                    tool_name=tool_name,
                )
        return output
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [
            _sanitize(item, secrets=secrets, tool_name=tool_name)
            for item in value
        ]
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, str):
        decoded = _decode_json_container(value)
        if decoded is not None:
            return _sanitize(decoded, secrets=secrets, tool_name=tool_name)
        if tool_name == "bash":
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
            return f"<bash payload:{len(value)} chars sha256:{digest}>"
        output = value
        for secret in secrets:
            for variant in {secret, quote(secret, safe=""), quote_plus(secret)}:
                if variant:
                    output = output.replace(variant, _REDACTED)
        output = _BEARER_PATTERN.sub(r"\1[REDACTED]", output)
        return _COMMON_TOKEN_PATTERN.sub(_REDACTED, output)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _decode_json_container(value: str) -> Any | None:
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        decoded = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return None
    return decoded if isinstance(decoded, (Mapping, list)) else None


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")
    return any(marker in normalized for marker in _SENSITIVE_KEY_MARKERS)


_LOG_LEVEL_NUMBERS = {
    "TRACE": 5,
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}
_BRIDGE_TAKEOVER_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "jieba")
_LOGGING_INTERNAL_FILES = frozenset(
    os.path.realpath(path) for path in (logging.__file__, __file__)
)
_bridge_lock = threading.RLock()
_bridge_handler: "_LoguruInterceptHandler | None" = None


class _LoguruInterceptHandler(logging.Handler):
    """Forward standard-library records into loguru's single log pipeline."""

    def __init__(self, host_logger: Any = None) -> None:
        super().__init__()
        self._host_logger = host_logger

    def set_host_logger(self, host_logger: Any) -> None:
        self._host_logger = host_logger

    def _threshold(self) -> int:
        level = getattr(self._host_logger, "log_level", None)
        if isinstance(level, int):
            return level
        return _LOG_LEVEL_NUMBERS.get(str(level or "").strip().upper(), logging.INFO)

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < self._threshold():
            return
        try:
            from loguru import logger as loguru_logger
        except ImportError:  # pragma: no cover - loguru ships with jianer-bot
            return
        try:
            level: str | int = loguru_logger.level(record.levelname).name
        except (KeyError, ValueError):
            level = record.levelno
        frame = logging.currentframe()
        depth = 0
        while frame is not None and (
            os.path.realpath(frame.f_code.co_filename) in _LOGGING_INTERNAL_FILES
        ):
            frame = frame.f_back
            depth += 1
        try:
            loguru_logger.opt(depth=depth, exception=record.exc_info).log(
                level, record.getMessage()
            )
        except Exception:  # pragma: no cover - logging must never break callers
            self.handleError(record)


def install_stdlib_logging_bridge(
    host_logger: Any = None,
    *,
    takeover: Sequence[str] = _BRIDGE_TAKEOVER_LOGGERS,
) -> None:
    """Route third-party ``logging`` records into the bot's loguru pipeline.

    The vendored Jianer Memory core and its console use ``logging.getLogger``
    directly. Without this bridge their records never reach loguru: INFO/DEBUG
    are dropped by the handler-less root logger and WARNING+ leak through
    ``logging.lastResort`` as unformatted stderr text.
    """

    global _bridge_handler
    with _bridge_lock:
        root = logging.getLogger()
        if _bridge_handler is None:
            _bridge_handler = _LoguruInterceptHandler(host_logger)
            logging.basicConfig(level=0, handlers=[_bridge_handler], force=True)
        else:
            if host_logger is not None:
                _bridge_handler.set_host_logger(host_logger)
            if _bridge_handler not in root.handlers:
                root.addHandler(_bridge_handler)
            root.setLevel(0)
        for name in takeover:
            target = logging.getLogger(name)
            target.handlers = []
            target.propagate = True
