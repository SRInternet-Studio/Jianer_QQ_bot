"""Long-term memory for JianerMate.

JianerMate originally shipped without long-term memory (only "recent
messages"), so the migrated memory tools were stubs.  This module gives them a
real backend by reusing JianerAI's legacy scoped memory store - the same code
path that implements ``list_scoped_memories`` / ``create_scoped_memory`` /
``update_scoped_memory`` for the original plugin.  No embeddings or vector
index are required, and the JianerAI plugin does not have to be enabled.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any


_LOGGER = logging.getLogger("jianer_mate.memory")


class LongTermMemory:
    """Thin lifecycle wrapper around JianerAI's ``JianerMemoryStore``."""

    def __init__(
        self,
        path: str | Path,
        *,
        enabled: bool = True,
        default_memory_enabled: bool = True,
        logger: Any | None = None,
    ) -> None:
        self.path = Path(path)
        self._logger = logger or _LOGGER
        self._store: Any | None = None
        self._error = ""
        if not enabled:
            self._error = "长期记忆已在配置中关闭"
            return
        try:
            from plugins.JianerAI.memory import JianerMemoryStore  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001 - optional dependency
            self._error = f"未能加载记忆实现：{exc.__class__.__name__}"
            self._log("warning", self._error)
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            store = JianerMemoryStore(
                str(self.path),
                default_memory_enabled=bool(default_memory_enabled),
                default_memory_interval_seconds=3600,
            )
            store.initialize()
        except Exception as exc:  # noqa: BLE001 - never break chat over memory
            self._error = f"初始化失败：{exc.__class__.__name__}"
            self._log("error", f"长期记忆初始化失败：{exc}")
            return
        self._store = store

    @property
    def available(self) -> bool:
        return self._store is not None

    @property
    def error(self) -> str:
        return self._error

    def close(self) -> None:
        store, self._store = self._store, None
        if store is not None:
            with contextlib.suppress(Exception):
                store.close()

    def __getattr__(self, name: str) -> Any:
        store = self.__dict__.get("_store")
        if store is None:
            raise AttributeError(name)
        return getattr(store, name)

    def _log(self, level: str, message: str) -> None:
        handler = getattr(self._logger, level, None)
        if callable(handler):
            handler(message)
