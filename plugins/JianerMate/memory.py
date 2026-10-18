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
import json
import logging
from pathlib import Path
from typing import Any

from plugins.JianerMate.compat import ensure_jianer_ai


ensure_jianer_ai()


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
        backend: str = "memorix",
        data_dir: str | Path | None = None,
        project_root: str | Path | None = None,
        embedding_config_path: str | Path | None = None,
        bot_name: str = "简儿",
    ) -> None:
        self.path = Path(path)
        self._logger = logger or _LOGGER
        self.backend = str(backend or "memorix").strip().casefold()
        self.data_dir = Path(data_dir) if data_dir is not None else self.path.parent / "memorix"
        self.project_root = Path(project_root or Path.cwd())
        self.embedding_config_path = (
            Path(embedding_config_path)
            if embedding_config_path is not None
            else self.project_root / "aiconfig" / "embedding.json"
        )
        self.bot_name = str(bot_name or "简儿")
        self._store: Any | None = None
        self._legacy_store: Any | None = None
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
            legacy = JianerMemoryStore(
                str(self.path),
                default_memory_enabled=bool(default_memory_enabled),
                default_memory_interval_seconds=3600,
            )
        except Exception as exc:  # noqa: BLE001 - never break chat over memory
            self._error = f"初始化失败：{exc.__class__.__name__}"
            self._log("error", f"长期记忆初始化失败：{exc}")
            return
        self._legacy_store = legacy
        store: Any = legacy
        if self.backend == "memorix":
            store = self._build_memorix(legacy) or legacy
        if store is legacy:
            with contextlib.suppress(Exception):
                legacy.initialize()
        self._store = store

    def _build_memorix(self, legacy: Any) -> Any | None:
        """Build the memorix kernel (FAISS + embeddings + FTS) when possible."""

        try:
            from plugins.JianerAI.memorix_adapter import (  # noqa: PLC0415
                JianerMemoryAdapter,
            )
        except Exception as exc:  # noqa: BLE001 - optional dependency
            self._log(
                "warning",
                f"memorix 内核不可用，回退旧记忆：{exc.__class__.__name__}",
            )
            return None
        embedding = self._embedding_config()
        if embedding is None:
            self._log(
                "warning",
                f"未找到可用的 embedding 配置（{self.embedding_config_path}），"
                "memorix 回退为旧记忆存储",
            )
            return None
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            adapter = JianerMemoryAdapter(
                project_root=self.project_root,
                data_dir=self.data_dir,
                config={
                    "plugin": {"enabled": True},
                    "bot": {"nickname": self.bot_name},
                    "storage": {"data_dir": str(self.data_dir)},
                    "embedding": embedding,
                    "retrieval": {
                        "vector_pools": {"mode": "dual"},
                        "sparse": {"enabled": True},
                    },
                },
                legacy_store=legacy,
                enabled=True,
            )
            initializer = getattr(adapter, "initialize", None)
            if callable(initializer):
                initializer()
        except Exception as exc:  # noqa: BLE001 - fall back instead of failing
            self._log(
                "error", f"memorix 初始化失败，回退旧记忆：{exc.__class__.__name__}"
            )
            return None
        self.backend = "memorix"
        self._log("info", f"长期记忆后端：memorix（{self.data_dir}）")
        return adapter

    def _embedding_config(self) -> dict[str, Any] | None:
        candidates = [
            self.embedding_config_path,
            self.project_root / "aiconfig" / "embedding.json.example",
        ]
        for candidate in candidates:
            try:
                raw = json.loads(Path(candidate).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(raw, dict):
                return {
                    "model_name": str(
                        raw.get("Model", raw.get("model", "auto")) or "auto"
                    ).strip(),
                    "provider": str(
                        raw.get("Provider", raw.get("provider", "openai"))
                        or "openai"
                    ).strip().lower(),
                    "api_key": str(
                        raw.get("ApiKey", raw.get("api_key", "")) or ""
                    ).strip(),
                    "base_url": str(
                        raw.get("BaseUrl", raw.get("base_url", "")) or ""
                    ).strip().rstrip("/"),
                    "dimension": max(
                        1,
                        int(
                            raw.get(
                                "Dimension",
                                raw.get("dimension", 1536),
                            )
                        ),
                    ),
                    "batch_size": int(
                        raw.get("BatchSize", raw.get("batch_size", 32))
                    ),
                    "max_concurrent": int(
                        raw.get("MaxConcurrent", raw.get("max_concurrent", 4))
                    ),
                    "timeout_seconds": float(
                        raw.get(
                            "TimeoutSeconds",
                            raw.get("timeout_seconds", 30.0),
                        )
                        or 30.0
                    ),
                    "enable_cache": bool(
                        raw.get("CacheEnabled", raw.get("cache_enabled", True))
                    ),
                    "dimension_request_mode": "explicit",
                    "fallback": {"enabled": False},
                }
        return None

    @property
    def available(self) -> bool:
        return self._store is not None

    @property
    def error(self) -> str:
        return self._error

    def close(self) -> None:
        store, self._store = self._store, None
        legacy, self._legacy_store = self._legacy_store, None
        if store is not None:
            with contextlib.suppress(Exception):
                store.close()
        if legacy is not None and legacy is not store:
            with contextlib.suppress(Exception):
                legacy.close()

    def __getattr__(self, name: str) -> Any:
        store = self.__dict__.get("_store")
        if store is None:
            raise AttributeError(name)
        return getattr(store, name)

    def _log(self, level: str, message: str) -> None:
        handler = getattr(self._logger, level, None)
        if callable(handler):
            handler(message)
