"""Atomic JSON persistence for Agent context state."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from plugins.JianerAI.agent_runtime.models import SessionKey


class ContextStore:
    """Persist one context's history, task list, and metadata."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def context_dir(self, key: SessionKey) -> Path:
        if key.scene_type == "system":
            return self.root / "system"
        return self.root / "contexts" / key.directory_name

    def history_path(self, key: SessionKey) -> Path:
        return self.context_dir(key) / "history.json"

    def tasks_path(self, key: SessionKey) -> Path:
        return self.context_dir(key) / "tasks.json"

    def metadata_path(self, key: SessionKey) -> Path:
        return self.context_dir(key) / "metadata.json"

    def list_sessions(self) -> tuple[SessionKey, ...]:
        root = self.root / "contexts"
        if not root.is_dir():
            return ()
        output: list[SessionKey] = []
        for metadata in root.glob("*/metadata.json"):
            try:
                with metadata.open("r", encoding="utf-8") as stream:
                    raw = json.load(stream)
                scene_type = str(raw.get("scene_type") or "")
                if scene_type not in {"group", "private"}:
                    continue
                output.append(
                    SessionKey(
                        scene_type=scene_type,  # type: ignore[arg-type]
                        scene_id=str(raw.get("scene_id") or ""),
                        protocol=str(raw.get("protocol") or "unknown"),
                        self_id=str(raw.get("self_id") or "unknown"),
                    )
                )
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
        return tuple(output)

    async def load_history(self, key: SessionKey) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._load_list, self.history_path(key))

    async def save_history(
        self,
        key: SessionKey,
        history: list[dict[str, Any]],
    ) -> None:
        await asyncio.to_thread(self._write_json, self.history_path(key), history)
        await asyncio.to_thread(
            self._write_json,
            self.metadata_path(key),
            {
                "session": key.value,
                "scene_type": key.scene_type,
                "scene_id": key.scene_id,
                "protocol": key.protocol,
                "self_id": key.self_id,
                "updated_at": int(time.time()),
            },
        )

    async def load_tasks(self, key: SessionKey) -> list[str]:
        raw = await asyncio.to_thread(self._load_list, self.tasks_path(key))
        return [str(item) for item in raw if str(item).strip()]

    async def save_tasks(self, key: SessionKey, tasks: list[str]) -> None:
        await asyncio.to_thread(
            self._write_json,
            self.tasks_path(key),
            [str(item) for item in tasks if str(item).strip()],
        )

    def _load_list(self, path: Path) -> list[Any]:
        try:
            with path.open("r", encoding="utf-8") as stream:
                value = json.load(stream)
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError):
            self._backup_corrupt(path)
            return []
        if not isinstance(value, list):
            self._backup_corrupt(path)
            return []
        return value

    def _write_json(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    @staticmethod
    def _backup_corrupt(path: Path) -> None:
        if not path.exists():
            return
        backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            os.replace(path, backup)
        except OSError:
            pass
