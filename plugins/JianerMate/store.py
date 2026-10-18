"""SQLite storage for JianerMate conversations, messages, memes, decisions."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


KEEP_DECISIONS_PER_CONVERSATION = 200


SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    ref INTEGER PRIMARY KEY AUTOINCREMENT,
    protocol TEXT NOT NULL,
    self_id TEXT NOT NULL,
    conversation_kind TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    talk_value REAL,
    quiet_hours TEXT,
    min_interval_seconds INTEGER,
    custom_prompt TEXT NOT NULL DEFAULT '',
    debug_dry_run INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(protocol, self_id, conversation_kind, conversation_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_ref INTEGER NOT NULL,
    platform_message_id TEXT,
    sender_id TEXT,
    sender_name TEXT,
    direction TEXT NOT NULL,
    content TEXT NOT NULL,
    message_type TEXT NOT NULL DEFAULT 'text',
    segments_json TEXT NOT NULL DEFAULT '[]',
    has_image INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_ref, id);

CREATE TABLE IF NOT EXISTS memes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    mime TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    size INTEGER NOT NULL,
    source_group TEXT,
    source_user TEXT,
    tags TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    use_count INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL,
    last_used_at INTEGER
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_ref INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    gate_passed INTEGER NOT NULL DEFAULT 0,
    gate_reason TEXT NOT NULL DEFAULT '',
    planner_json TEXT NOT NULL DEFAULT '',
    spoke INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_decisions_conversation
    ON decisions(conversation_ref, id);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class ConversationRecord:
    ref: int
    protocol: str
    self_id: str
    kind: str
    conversation_id: str
    enabled: bool
    talk_value: float | None
    quiet_hours: str | None
    min_interval_seconds: int | None
    custom_prompt: str
    debug_dry_run: bool
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class MessageRecord:
    id: int
    platform_message_id: str
    sender_id: str
    sender_name: str
    direction: str
    content: str
    message_type: str
    has_image: bool
    created_at: int


@dataclass(frozen=True, slots=True)
class MemeRecord:
    id: int
    sha256: str
    path: str
    mime: str
    width: int
    height: int
    size: int
    source_group: str
    source_user: str
    tags: str
    enabled: bool
    use_count: int
    added_at: int
    last_used_at: int | None


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    id: int
    created_at: int
    gate_passed: bool
    gate_reason: str
    planner_json: str
    spoke: bool
    error: str


class MateStore:
    """Small synchronous SQLite store guarded by one re-entrant lock."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.executescript(SCHEMA)
            self._connection.commit()

    # ------------------------------------------------------------------ #
    # conversations
    # ------------------------------------------------------------------ #
    def get_conversation(
        self,
        protocol: str,
        self_id: str,
        kind: str,
        conversation_id: str,
    ) -> ConversationRecord | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM conversations
                WHERE protocol = ? AND self_id = ?
                  AND conversation_kind = ? AND conversation_id = ?
                """,
                (str(protocol), str(self_id), str(kind), str(conversation_id)),
            ).fetchone()
        return _conversation(row) if row is not None else None

    def ensure_conversation(
        self,
        protocol: str,
        self_id: str,
        kind: str,
        conversation_id: str,
        *,
        now: int | None = None,
    ) -> ConversationRecord:
        timestamp = int(now if now is not None else time.time())
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO conversations (
                    protocol, self_id, conversation_kind, conversation_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(protocol, self_id, conversation_kind, conversation_id)
                DO UPDATE SET updated_at = excluded.updated_at
                """,
                (
                    str(protocol),
                    str(self_id),
                    str(kind),
                    str(conversation_id),
                    timestamp,
                    timestamp,
                ),
            )
            self._connection.commit()
        record = self.get_conversation(protocol, self_id, kind, conversation_id)
        if record is None:  # pragma: no cover - defensive
            raise RuntimeError("无法创建会话记录")
        return record

    def update_conversation(
        self,
        ref: int,
        *,
        now: int | None = None,
        **fields: Any,
    ) -> ConversationRecord | None:
        allowed = {
            "enabled",
            "talk_value",
            "quiet_hours",
            "min_interval_seconds",
            "custom_prompt",
            "debug_dry_run",
        }
        updates: list[str] = []
        values: list[Any] = []
        for name, value in fields.items():
            if name not in allowed:
                continue
            if name in {"enabled", "debug_dry_run"}:
                value = 1 if value else 0
            updates.append(f"{name} = ?")
            values.append(value)
        if not updates:
            return self.get_conversation_by_ref(ref)
        timestamp = int(now if now is not None else time.time())
        updates.append("updated_at = ?")
        values.append(timestamp)
        values.append(int(ref))
        with self._lock:
            self._connection.execute(
                f"UPDATE conversations SET {', '.join(updates)} WHERE ref = ?",
                tuple(values),
            )
            self._connection.commit()
        return self.get_conversation_by_ref(ref)

    def get_conversation_by_ref(self, ref: int) -> ConversationRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM conversations WHERE ref = ?",
                (int(ref),),
            ).fetchone()
        return _conversation(row) if row is not None else None

    def list_conversations(self, *, enabled_only: bool = False) -> tuple[ConversationRecord, ...]:
        clause = "WHERE enabled = 1" if enabled_only else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM conversations {clause} ORDER BY updated_at DESC"
            ).fetchall()
        return tuple(_conversation(row) for row in rows)

    # ------------------------------------------------------------------ #
    # messages
    # ------------------------------------------------------------------ #
    def append_message(
        self,
        conversation_ref: int,
        *,
        direction: str,
        content: str,
        platform_message_id: str = "",
        sender_id: str = "",
        sender_name: str = "",
        message_type: str = "text",
        segments_json: str = "[]",
        has_image: bool = False,
        now: int | None = None,
    ) -> int:
        timestamp = int(now if now is not None else time.time())
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT INTO messages (
                    conversation_ref, platform_message_id, sender_id, sender_name,
                    direction, content, message_type, segments_json, has_image,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(conversation_ref),
                    str(platform_message_id or ""),
                    str(sender_id or ""),
                    str(sender_name or ""),
                    str(direction),
                    str(content or ""),
                    str(message_type),
                    str(segments_json or "[]"),
                    1 if has_image else 0,
                    timestamp,
                ),
            )
            self._connection.commit()
            return int(cursor.lastrowid)

    def recent_messages(
        self,
        conversation_ref: int,
        *,
        limit: int = 40,
    ) -> tuple[MessageRecord, ...]:
        limit_i = max(1, min(int(limit), 500))
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM messages
                WHERE conversation_ref = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(conversation_ref), limit_i),
            ).fetchall()
        records = [_message(row) for row in rows]
        records.reverse()
        return tuple(records)

    def last_outgoing_at(self, conversation_ref: int) -> int | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT created_at FROM messages
                WHERE conversation_ref = ? AND direction = 'outgoing'
                ORDER BY id DESC LIMIT 1
                """,
                (int(conversation_ref),),
            ).fetchone()
        return int(row["created_at"]) if row is not None else None

    def prune_messages(
        self,
        conversation_ref: int,
        *,
        keep: int = 300,
        retention_days: int = 7,
        now: int | None = None,
    ) -> int:
        timestamp = int(now if now is not None else time.time())
        cutoff = timestamp - max(1, int(retention_days)) * 86400
        with self._lock:
            cursor = self._connection.execute(
                """
                DELETE FROM messages
                WHERE conversation_ref = ?
                  AND (
                    created_at < ?
                    OR id NOT IN (
                        SELECT id FROM messages
                        WHERE conversation_ref = ?
                        ORDER BY id DESC LIMIT ?
                    )
                  )
                """,
                (
                    int(conversation_ref),
                    cutoff,
                    int(conversation_ref),
                    max(1, int(keep)),
                ),
            )
            self._connection.commit()
            return int(cursor.rowcount or 0)

    # ------------------------------------------------------------------ #
    # memes
    # ------------------------------------------------------------------ #
    def add_meme(
        self,
        *,
        sha256: str,
        path: str,
        mime: str,
        width: int,
        height: int,
        size: int,
        source_group: str = "",
        source_user: str = "",
        now: int | None = None,
    ) -> int | None:
        timestamp = int(now if now is not None else time.time())
        with self._lock:
            try:
                cursor = self._connection.execute(
                    """
                    INSERT INTO memes (
                        sha256, path, mime, width, height, size,
                        source_group, source_user, added_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(sha256),
                        str(path),
                        str(mime),
                        int(width),
                        int(height),
                        int(size),
                        str(source_group or ""),
                        str(source_user or ""),
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError:
                return None
            self._connection.commit()
            return int(cursor.lastrowid)

    def get_meme(self, meme_id: int) -> MemeRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM memes WHERE id = ?",
                (int(meme_id),),
            ).fetchone()
        return _meme(row) if row is not None else None

    def get_meme_by_sha(self, sha256: str) -> MemeRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM memes WHERE sha256 = ?",
                (str(sha256),),
            ).fetchone()
        return _meme(row) if row is not None else None

    def count_memes(self, *, enabled_only: bool = False) -> int:
        clause = "WHERE enabled = 1" if enabled_only else ""
        with self._lock:
            row = self._connection.execute(
                f"SELECT COUNT(*) AS total FROM memes {clause}"
            ).fetchone()
        return int(row["total"]) if row is not None else 0

    def total_meme_bytes(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(SUM(size), 0) AS total FROM memes"
            ).fetchone()
        return int(row["total"]) if row is not None else 0

    def list_memes(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        enabled_only: bool = False,
    ) -> tuple[MemeRecord, ...]:
        clause = "WHERE enabled = 1" if enabled_only else ""
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT * FROM memes {clause}
                ORDER BY id DESC LIMIT ? OFFSET ?
                """,
                (max(1, min(int(limit), 200)), max(0, int(offset))),
            ).fetchall()
        return tuple(_meme(row) for row in rows)

    def meme_candidates(
        self,
        *,
        limit: int,
        now: int,
        exclude_used_after: int | None = None,
    ) -> tuple[MemeRecord, ...]:
        clause = "WHERE enabled = 1"
        params: list[Any] = []
        if exclude_used_after is not None:
            clause += " AND (last_used_at IS NULL OR last_used_at < ?)"
            params.append(int(exclude_used_after))
        params.append(max(1, min(int(limit), 64)))
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT * FROM memes {clause}
                ORDER BY
                    CASE WHEN last_used_at IS NULL THEN 0 ELSE 1 END,
                    COALESCE(last_used_at, added_at) ASC,
                    use_count ASC,
                    id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        selected = [_meme(row) for row in rows]
        if selected:
            return tuple(selected)
        # Every candidate was used recently: relax the cooldown instead of
        # starving the picker.
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM memes WHERE enabled = 1
                ORDER BY COALESCE(last_used_at, added_at) ASC, use_count ASC
                LIMIT ?
                """,
                (max(1, min(int(limit), 64)),),
            ).fetchall()
        return tuple(_meme(row) for row in rows)

    def all_meme_paths(self) -> tuple[str, ...]:
        with self._lock:
            rows = self._connection.execute("SELECT path FROM memes").fetchall()
        return tuple(str(row["path"]) for row in rows)

    def mark_meme_used(self, meme_id: int, *, now: int | None = None) -> None:
        timestamp = int(now if now is not None else time.time())
        with self._lock:
            self._connection.execute(
                """
                UPDATE memes
                SET use_count = use_count + 1, last_used_at = ?
                WHERE id = ?
                """,
                (timestamp, int(meme_id)),
            )
            self._connection.commit()

    def update_meme_tags(self, meme_id: int, tags: str) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE memes SET tags = ? WHERE id = ?",
                (str(tags or ""), int(meme_id)),
            )
            self._connection.commit()
        return bool(cursor.rowcount)

    def set_meme_enabled(self, meme_id: int, enabled: bool) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE memes SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, int(meme_id)),
            )
            self._connection.commit()
        return bool(cursor.rowcount)

    def delete_meme(self, meme_id: int) -> MemeRecord | None:
        record = self.get_meme(meme_id)
        if record is None:
            return None
        with self._lock:
            self._connection.execute(
                "DELETE FROM memes WHERE id = ?",
                (int(meme_id),),
            )
            self._connection.commit()
        return record

    def clear_memes(self) -> tuple[MemeRecord, ...]:
        records = self.list_memes(limit=200)
        with self._lock:
            self._connection.execute("DELETE FROM memes")
            self._connection.commit()
        return records

    def overflow_memes(self, keep: int) -> tuple[MemeRecord, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM memes
                ORDER BY COALESCE(last_used_at, added_at) DESC, id DESC
                LIMIT -1 OFFSET ?
                """,
                (max(0, int(keep)),),
            ).fetchall()
        return tuple(_meme(row) for row in rows)

    # ------------------------------------------------------------------ #
    # decisions and state
    # ------------------------------------------------------------------ #
    def add_decision(
        self,
        conversation_ref: int,
        *,
        gate_passed: bool,
        gate_reason: str = "",
        planner_json: str = "",
        spoke: bool = False,
        error: str = "",
        now: int | None = None,
    ) -> int:
        timestamp = int(now if now is not None else time.time())
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT INTO decisions (
                    conversation_ref, created_at, gate_passed,
                    gate_reason, planner_json, spoke, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(conversation_ref),
                    timestamp,
                    1 if gate_passed else 0,
                    str(gate_reason or "")[:500],
                    str(planner_json or "")[:4000],
                    1 if spoke else 0,
                    str(error or "")[:1000],
                ),
            )
            self._connection.execute(
                """
                DELETE FROM decisions
                WHERE conversation_ref = ?
                  AND id NOT IN (
                      SELECT id FROM decisions
                      WHERE conversation_ref = ?
                      ORDER BY id DESC LIMIT ?
                  )
                """,
                (
                    int(conversation_ref),
                    int(conversation_ref),
                    KEEP_DECISIONS_PER_CONVERSATION,
                ),
            )
            self._connection.commit()
            return int(cursor.lastrowid)

    def count_decisions(self, conversation_ref: int) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS total FROM decisions WHERE conversation_ref = ?",
                (int(conversation_ref),),
            ).fetchone()
        return int(row["total"]) if row is not None else 0

    def recent_decisions(
        self,
        conversation_ref: int,
        *,
        limit: int = 5,
    ) -> tuple[DecisionRecord, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM decisions
                WHERE conversation_ref = ?
                ORDER BY id DESC LIMIT ?
                """,
                (int(conversation_ref), max(1, min(int(limit), 50))),
            ).fetchall()
        return tuple(
            DecisionRecord(
                id=int(row["id"]),
                created_at=int(row["created_at"]),
                gate_passed=bool(row["gate_passed"]),
                gate_reason=str(row["gate_reason"] or ""),
                planner_json=str(row["planner_json"] or ""),
                spoke=bool(row["spoke"]),
                error=str(row["error"] or ""),
            )
            for row in rows
        )

    def set_state(self, key: str, value: str) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(key), str(value)),
            )
            self._connection.commit()

    def get_state(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM state WHERE key = ?",
                (str(key),),
            ).fetchone()
        return str(row["value"]) if row is not None else default

    def close(self) -> None:
        with self._lock:
            connection, self._connection = getattr(self, "_connection", None), None
        if connection is not None:
            connection.close()


def _conversation(row: sqlite3.Row) -> ConversationRecord:
    return ConversationRecord(
        ref=int(row["ref"]),
        protocol=str(row["protocol"]),
        self_id=str(row["self_id"]),
        kind=str(row["conversation_kind"]),
        conversation_id=str(row["conversation_id"]),
        enabled=bool(row["enabled"]),
        talk_value=None if row["talk_value"] is None else float(row["talk_value"]),
        quiet_hours=(
            None if row["quiet_hours"] is None else str(row["quiet_hours"])
        ),
        min_interval_seconds=(
            None
            if row["min_interval_seconds"] is None
            else int(row["min_interval_seconds"])
        ),
        custom_prompt=str(row["custom_prompt"] or ""),
        debug_dry_run=bool(row["debug_dry_run"]),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


def _message(row: sqlite3.Row) -> MessageRecord:
    return MessageRecord(
        id=int(row["id"]),
        platform_message_id=str(row["platform_message_id"] or ""),
        sender_id=str(row["sender_id"] or ""),
        sender_name=str(row["sender_name"] or ""),
        direction=str(row["direction"]),
        content=str(row["content"]),
        message_type=str(row["message_type"]),
        has_image=bool(row["has_image"]),
        created_at=int(row["created_at"]),
    )


def _meme(row: sqlite3.Row) -> MemeRecord:
    return MemeRecord(
        id=int(row["id"]),
        sha256=str(row["sha256"]),
        path=str(row["path"]),
        mime=str(row["mime"]),
        width=int(row["width"]),
        height=int(row["height"]),
        size=int(row["size"]),
        source_group=str(row["source_group"] or ""),
        source_user=str(row["source_user"] or ""),
        tags=str(row["tags"] or ""),
        enabled=bool(row["enabled"]),
        use_count=int(row["use_count"]),
        added_at=int(row["added_at"]),
        last_used_at=(
            None if row["last_used_at"] is None else int(row["last_used_at"])
        ),
    )


def conversation_key_tuple(
    protocol: Any,
    self_id: Any,
    kind: Any,
    conversation_id: Any,
) -> tuple[str, str, str, str]:
    return (
        str(protocol or ""),
        str(self_id or ""),
        str(getattr(kind, "value", kind) or ""),
        str(conversation_id or ""),
    )
