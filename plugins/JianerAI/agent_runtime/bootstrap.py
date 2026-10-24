"""One-time import of legacy conversation transcripts into Agent contexts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from plugins.JianerAI.agent_runtime.models import SessionKey
from plugins.JianerAI.agent_runtime.store import ContextStore


async def import_legacy_contexts(
    memory: Any,
    store: ContextStore,
    *,
    marker: str | Path,
    limit: int = 40,
) -> int:
    marker_path = Path(marker)
    if marker_path.exists():
        return 0
    partitions = getattr(memory, "list_chat_partitions", None)
    recent = getattr(memory, "query_recent_chat", None)
    if not callable(partitions) or not callable(recent):
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text("unsupported\n", encoding="utf-8")
        return 0
    imported = 0
    for partition in partitions():
        kind = str(getattr(partition, "conversation_kind", ""))
        if kind not in {"group", "private"}:
            continue
        key = SessionKey(
            scene_type=kind,  # type: ignore[arg-type]
            scene_id=str(getattr(partition, "conversation_id", "")),
            protocol=str(getattr(partition, "protocol", "unknown")),
            self_id=str(getattr(partition, "self_id", "unknown")),
        )
        existing = await store.load_history(key)
        if existing:
            continue
        try:
            rows = recent(
                protocol=key.protocol,
                self_id=key.self_id,
                conversation_kind=key.scene_type,
                conversation_id=key.scene_id,
                limit=max(1, int(limit)),
                max_characters=16000,
            )
        except Exception:
            continue
        history: list[dict[str, Any]] = []
        for row in reversed(tuple(rows or ())):
            content = str(getattr(row, "content", "") or "").strip()
            if not content:
                continue
            direction = str(getattr(row, "direction", "incoming"))
            history.append(
                {
                    "role": "assistant" if direction == "outgoing" else "user",
                    "content": content,
                    "imported_from": "legacy_transcript",
                }
            )
        if history:
            await store.save_history(key, history[-max(2, int(limit)) :])
            imported += 1
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps(
            {
                "imported_contexts": imported,
                "limit_per_context": int(limit),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return imported
