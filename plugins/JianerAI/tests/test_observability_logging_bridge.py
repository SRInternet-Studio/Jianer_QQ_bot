from __future__ import annotations

import logging
from typing import Any

from loguru import logger as loguru_logger

import plugins.JianerAI.observability as observability


class _HostLogger:
    """Minimal stand-in for the host's hyperogger ``Logger`` wrapper."""

    def __init__(self, level: str) -> None:
        self.log_level = level


def _record(level: int, message: str, *args: Any) -> logging.LogRecord:
    return logging.LogRecord(
        name="Jianer Memory.BridgeTest",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=args,
        exc_info=None,
    )


def _capture_messages() -> tuple[list[str], int]:
    messages: list[str] = []

    def sink(message: Any) -> None:
        messages.append(message.record["message"])

    return messages, loguru_logger.add(sink, level=0)


def test_intercept_handler_forwards_stdlib_records_to_loguru() -> None:
    messages, sink_id = _capture_messages()
    try:
        handler = observability._LoguruInterceptHandler(_HostLogger("DEBUG"))
        handler.emit(_record(logging.INFO, "记忆后台 %s", "桥接成功"))
    finally:
        loguru_logger.remove(sink_id)
    assert messages == ["记忆后台 桥接成功"]


def test_intercept_handler_respects_configured_level() -> None:
    messages, sink_id = _capture_messages()
    try:
        handler = observability._LoguruInterceptHandler(_HostLogger("INFO"))
        handler.emit(_record(logging.DEBUG, "prefix dict has been built successfully"))
        handler.emit(_record(logging.WARNING, "双池向量尚未就绪"))
    finally:
        loguru_logger.remove(sink_id)
    assert messages == ["双池向量尚未就绪"]


def test_install_bridge_is_idempotent_and_takes_over_known_loggers() -> None:
    root = logging.getLogger()
    jieba = logging.getLogger("jieba")
    saved_root_handlers = list(root.handlers)
    saved_root_level = root.level
    saved_bridge = observability._bridge_handler
    saved_jieba_handlers = list(jieba.handlers)
    saved_jieba_propagate = jieba.propagate
    jieba.addHandler(logging.NullHandler())
    jieba.propagate = False
    try:
        observability._bridge_handler = None
        observability.install_stdlib_logging_bridge(_HostLogger("INFO"))
        observability.install_stdlib_logging_bridge(_HostLogger("INFO"))
        assert jieba.handlers == []
        assert jieba.propagate is True
        assert observability._bridge_handler in root.handlers
        assert root.handlers.count(observability._bridge_handler) == 1
    finally:
        observability._bridge_handler = saved_bridge
        root.handlers = saved_root_handlers
        root.setLevel(saved_root_level)
        jieba.handlers = saved_jieba_handlers
        jieba.propagate = saved_jieba_propagate
