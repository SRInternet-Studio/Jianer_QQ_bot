"""Normalized persistence for JianerAI conversations and long-term memory.

The store is intentionally synchronous.  Callers that run on the bot event
loop should use ``asyncio.to_thread`` for operations that may touch disk.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


SCHEMA_VERSION = 3
GROUP_TRANSCRIPT_RETENTION_DAYS = 30
DEFAULT_MEMORY_INTERVAL_SECONDS = 6 * 3600
DEFAULT_GENERATION_RETRY_SECONDS = 5 * 60
MAX_GENERATION_RETRY_SECONDS = 6 * 3600
QQ_PROTOCOLS = frozenset({"onebot", "milky"})
CONVERSATION_KINDS = frozenset({"group", "private"})
_UNSET = object()


class MemoryStoreError(RuntimeError):
    """Base error for the JianerAI memory store."""


class IdentityAuthorizationError(MemoryStoreError):
    """Raised when a cross-identity merge has not been authorized."""


class StaleGenerationError(MemoryStoreError):
    """Raised when a background generation result belongs to an old barrier."""


@dataclass(frozen=True)
class IdentityRef:
    protocol: str
    self_id: str
    external_id: str


@dataclass(frozen=True)
class GenerationToken:
    canonical_user_id: str
    preset: str
    generation_id: str
    version: int
    started_at: int


@dataclass(frozen=True)
class MemoryEvidence:
    content: str
    conversation_pk: int | None = None
    transcript_id: int | None = None
    observed_at: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    fingerprint: str | None = None


@dataclass(frozen=True)
class GeneratedMemory:
    content: str
    weight: float = 0.3
    evidence: tuple[MemoryEvidence, ...] = ()


@dataclass(frozen=True)
class GenerationInsertResult:
    accepted: bool
    inserted: int
    updated: int
    skipped_suppressed: int
    stale_generation: bool = False

    def __int__(self) -> int:
        return self.inserted + self.updated


@dataclass(frozen=True)
class TranscriptWriteResult:
    transcript_id: int
    inserted: bool
    conversation_pk: int

    def __bool__(self) -> bool:
        return self.inserted


@dataclass(frozen=True)
class TranscriptRecord:
    transcript_id: int
    conversation_pk: int
    sender_canonical_id: str | None
    content: str
    occurred_at: int
    message_type: str

    @property
    def id(self) -> str:
        return str(self.transcript_id)

    @property
    def timestamp(self) -> int:
        return self.occurred_at


@dataclass(frozen=True)
class GenerationBatch:
    token: GenerationToken
    session_id: int | None
    canonical_user_id: str
    preset: str
    messages: tuple[TranscriptRecord, ...]
    last_transcript_id: int

    @property
    def rows(self) -> tuple[TranscriptRecord, ...]:
        return self.messages

    @property
    def barrier_token(self) -> str:
        return self.token.generation_id


@dataclass(frozen=True)
class MemoryRecord:
    fact_id: int
    canonical_user_id: str
    preset: str
    content: str
    fingerprint: str
    weight: float
    created_at: int
    updated_at: int
    evidence: tuple[MemoryEvidence, ...]

    @property
    def id(self) -> str:
        return str(self.fact_id)

    @property
    def memory_id(self) -> str:
        return str(self.fact_id)


@dataclass(frozen=True)
class SessionSettings:
    session_id: int
    conversation_pk: int
    preset: str
    model: str | None
    persona: str
    tts_enabled: bool
    ai_enabled: bool
    agent_enabled: bool | None
    memory_enabled: bool
    memory_interval_seconds: int
    last_generated_at: int
    updated_at: int


@dataclass(frozen=True)
class DueMemoryScope:
    canonical_user_id: str
    preset: str
    protocol: str
    self_id: str
    conversation_kind: str
    conversation_id: str
    enabled: bool
    interval_seconds: int
    last_generated_at: int


def _now_ts() -> int:
    return int(time.time())


def _required_text(value: Any, field_name: str) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        raise ValueError(f"{field_name} must not be empty")
    if "\x00" in text:
        raise ValueError(f"{field_name} must not contain NUL")
    return text


def _optional_text(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if "\x00" in text:
        raise ValueError("identifier must not contain NUL")
    return text


def normalize_protocol(protocol: Any) -> str:
    value = _required_text(protocol, "protocol").lower().replace("_", "-")
    aliases = {
        "onebot-v11": "onebot",
        "onebot11": "onebot",
        "ob11": "onebot",
        "lark": "feishu",
    }
    return aliases.get(value, value)


def normalize_preset(preset: Any) -> str:
    value = str(preset if preset is not None else "").strip()
    return value or "default"


def normalize_memory_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value if value is not None else ""))
    text = re.sub(r"\s+", " ", text).strip().casefold()
    return text


def memory_fingerprint(value: Any) -> str:
    normalized = normalize_memory_text(value)
    if not normalized:
        raise ValueError("memory content must not be empty")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _canonical_for_alias(protocol: str, self_id: str, external_id: str) -> str:
    if protocol in QQ_PROTOCOLS or protocol == "qq":
        return f"qq:{external_id}"
    seed = f"{protocol}\x00{self_id}\x00{external_id}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    return f"{protocol}:{digest}"


def _json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(value or {}), ensure_ascii=False, sort_keys=True)


def _coerce_evidence(value: MemoryEvidence | Mapping[str, Any] | str) -> MemoryEvidence:
    if isinstance(value, MemoryEvidence):
        return value
    if isinstance(value, str):
        return MemoryEvidence(content=value)
    if isinstance(value, Mapping):
        return MemoryEvidence(
            content=str(value.get("content") or ""),
            conversation_pk=(
                int(value["conversation_pk"])
                if value.get("conversation_pk") is not None
                else None
            ),
            transcript_id=(
                int(value["transcript_id"])
                if value.get("transcript_id") is not None
                else None
            ),
            observed_at=(
                int(value["observed_at"])
                if value.get("observed_at") is not None
                else None
            ),
            metadata=(
                value.get("metadata")
                if isinstance(value.get("metadata"), Mapping)
                else {}
            ),
            fingerprint=(
                str(value["fingerprint"])
                if value.get("fingerprint") is not None
                else None
            ),
        )
    raise TypeError(f"unsupported evidence value: {type(value)!r}")


def _coerce_memory(value: GeneratedMemory | Mapping[str, Any] | str) -> GeneratedMemory:
    if isinstance(value, GeneratedMemory):
        return value
    if isinstance(value, str):
        return GeneratedMemory(content=value)
    if isinstance(value, Mapping):
        raw_evidence = value.get("evidence") or ()
        explicit_fingerprint = str(
            value.get("evidence_fingerprint") or ""
        ).strip()
        if not raw_evidence and explicit_fingerprint:
            raw_evidence = (
                MemoryEvidence(
                    content="",
                    fingerprint=explicit_fingerprint,
                ),
            )
        if isinstance(raw_evidence, (str, Mapping, MemoryEvidence)):
            raw_evidence = (raw_evidence,)
        evidence = tuple(_coerce_evidence(item) for item in raw_evidence)
        try:
            weight = float(value.get("weight", 0.3))
        except (TypeError, ValueError):
            weight = 0.3
        return GeneratedMemory(
            content=str(value.get("content") or ""),
            weight=max(0.0, min(1.0, weight)),
            evidence=evidence,
        )
    raise TypeError(f"unsupported memory value: {type(value)!r}")


def _evidence_fingerprint(evidence: MemoryEvidence) -> str:
    explicit = str(evidence.fingerprint or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", explicit):
        return explicit
    if explicit:
        return hashlib.sha256(explicit.encode("utf-8")).hexdigest()
    return memory_fingerprint(evidence.content)


class JianerMemoryStore:
    """SQLite-backed canonical identity, transcript, and memory store."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        initialize: bool = True,
        default_memory_enabled: bool = True,
        default_memory_interval_seconds: int = DEFAULT_MEMORY_INTERVAL_SECONDS,
    ):
        self.db_path = Path(db_path)
        interval = int(default_memory_interval_seconds)
        if interval < 60:
            raise ValueError(
                "default_memory_interval_seconds must be at least 60"
            )
        self.default_memory_enabled = bool(default_memory_enabled)
        self.default_memory_interval_seconds = interval
        if initialize:
            self.initialize()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextmanager
    def _transaction(
        self, conn: sqlite3.Connection, *, immediate: bool = True
    ) -> Iterator[None]:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    def initialize(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    schema_name TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS canonical_identities (
                    canonical_id TEXT PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    merged_into TEXT,
                    FOREIGN KEY (merged_into)
                        REFERENCES canonical_identities(canonical_id)
                );

                CREATE TABLE IF NOT EXISTS identity_aliases (
                    alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    protocol TEXT NOT NULL,
                    self_id TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    canonical_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(protocol, self_id, external_id),
                    FOREIGN KEY (canonical_id)
                        REFERENCES canonical_identities(canonical_id)
                );
                CREATE INDEX IF NOT EXISTS idx_identity_aliases_canonical
                    ON identity_aliases(canonical_id);

                CREATE TABLE IF NOT EXISTS identity_authorizations (
                    authorization_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_protocol TEXT NOT NULL,
                    source_self_id TEXT NOT NULL,
                    source_external_id TEXT NOT NULL,
                    target_canonical_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    authorized_at INTEGER NOT NULL,
                    revoked_at INTEGER,
                    UNIQUE(
                        source_protocol,
                        source_self_id,
                        source_external_id,
                        target_canonical_id,
                        reason
                    ),
                    FOREIGN KEY (target_canonical_id)
                        REFERENCES canonical_identities(canonical_id)
                );

                CREATE TABLE IF NOT EXISTS identity_merge_ledger (
                    merge_key TEXT PRIMARY KEY,
                    source_canonical_id TEXT NOT NULL,
                    target_canonical_id TEXT NOT NULL,
                    source_protocol TEXT NOT NULL,
                    source_self_id TEXT NOT NULL,
                    source_external_id TEXT NOT NULL,
                    target_protocol TEXT NOT NULL,
                    target_self_id TEXT NOT NULL,
                    target_external_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    applied_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_pk INTEGER PRIMARY KEY AUTOINCREMENT,
                    protocol TEXT NOT NULL,
                    self_id TEXT NOT NULL,
                    conversation_kind TEXT NOT NULL
                        CHECK(conversation_kind IN ('group', 'private')),
                    conversation_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    UNIQUE(protocol, self_id, conversation_kind, conversation_id)
                );

                CREATE TABLE IF NOT EXISTS conversation_preferences (
                    conversation_pk INTEGER PRIMARY KEY,
                    active_preset TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY (conversation_pk)
                        REFERENCES conversations(conversation_pk)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS raw_transcript_messages (
                    transcript_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_pk INTEGER NOT NULL,
                    message_key TEXT NOT NULL,
                    external_message_id TEXT NOT NULL,
                    sender_canonical_id TEXT,
                    sender_protocol TEXT NOT NULL,
                    sender_self_id TEXT NOT NULL,
                    sender_external_id TEXT NOT NULL,
                    preset_key TEXT NOT NULL DEFAULT 'default',
                    content TEXT NOT NULL,
                    occurred_at INTEGER NOT NULL,
                    message_type TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    UNIQUE(conversation_pk, message_key),
                    FOREIGN KEY (conversation_pk)
                        REFERENCES conversations(conversation_pk)
                        ON DELETE CASCADE,
                    FOREIGN KEY (sender_canonical_id)
                        REFERENCES canonical_identities(canonical_id)
                );
                CREATE INDEX IF NOT EXISTS idx_raw_transcript_conversation_time
                    ON raw_transcript_messages(conversation_pk, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_raw_transcript_sender
                    ON raw_transcript_messages(sender_canonical_id, occurred_at);

                CREATE TABLE IF NOT EXISTS session_settings (
                    session_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_pk INTEGER NOT NULL,
                    preset_key TEXT NOT NULL,
                    model TEXT,
                    persona TEXT NOT NULL DEFAULT '',
                    tts_enabled INTEGER NOT NULL,
                    ai_enabled INTEGER NOT NULL DEFAULT 1,
                    agent_enabled INTEGER,
                    memory_enabled INTEGER NOT NULL DEFAULT 1,
                    memory_interval_seconds INTEGER NOT NULL DEFAULT 21600,
                    last_generated_at INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(conversation_pk, preset_key),
                    FOREIGN KEY (conversation_pk)
                        REFERENCES conversations(conversation_pk)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS memory_preferences (
                    canonical_id TEXT NOT NULL,
                    preset_key TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    interval_seconds INTEGER NOT NULL DEFAULT 21600,
                    last_generated_at INTEGER NOT NULL DEFAULT 0,
                    next_retry_at INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(canonical_id, preset_key),
                    FOREIGN KEY (canonical_id)
                        REFERENCES canonical_identities(canonical_id)
                );

                CREATE TABLE IF NOT EXISTS memory_facts (
                    fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_id TEXT NOT NULL,
                    preset_key TEXT NOT NULL,
                    fact_fingerprint TEXT NOT NULL,
                    content TEXT NOT NULL,
                    weight REAL NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    last_confirmed_at INTEGER NOT NULL,
                    UNIQUE(canonical_id, preset_key, fact_fingerprint),
                    FOREIGN KEY (canonical_id)
                        REFERENCES canonical_identities(canonical_id)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_facts_lookup
                    ON memory_facts(canonical_id, preset_key, updated_at DESC);

                CREATE TABLE IF NOT EXISTS memory_evidence (
                    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fact_id INTEGER NOT NULL,
                    evidence_fingerprint TEXT NOT NULL,
                    content TEXT NOT NULL,
                    conversation_pk INTEGER,
                    transcript_id INTEGER,
                    observed_at INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(fact_id, evidence_fingerprint),
                    FOREIGN KEY (fact_id)
                        REFERENCES memory_facts(fact_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (conversation_pk)
                        REFERENCES conversations(conversation_pk)
                        ON DELETE SET NULL,
                    FOREIGN KEY (transcript_id)
                        REFERENCES raw_transcript_messages(transcript_id)
                        ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS memory_suppressions (
                    canonical_id TEXT NOT NULL,
                    preset_key TEXT NOT NULL,
                    suppression_kind TEXT NOT NULL
                        CHECK(suppression_kind IN ('fact', 'evidence')),
                    fingerprint TEXT NOT NULL,
                    content_snapshot TEXT NOT NULL,
                    source_fact_id TEXT,
                    reason TEXT NOT NULL,
                    deleted_at INTEGER NOT NULL,
                    barrier_version INTEGER NOT NULL,
                    PRIMARY KEY(
                        canonical_id,
                        preset_key,
                        suppression_kind,
                        fingerprint
                    ),
                    FOREIGN KEY (canonical_id)
                        REFERENCES canonical_identities(canonical_id)
                );

                CREATE TABLE IF NOT EXISTS generation_barriers (
                    canonical_id TEXT NOT NULL,
                    preset_key TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    active_generation_id TEXT,
                    started_at INTEGER NOT NULL,
                    invalidated_at INTEGER,
                    completed_at INTEGER,
                    PRIMARY KEY(canonical_id, preset_key),
                    FOREIGN KEY (canonical_id)
                        REFERENCES canonical_identities(canonical_id)
                );

                CREATE TABLE IF NOT EXISTS generation_cursors (
                    session_id INTEGER NOT NULL,
                    canonical_id TEXT NOT NULL,
                    last_transcript_id INTEGER NOT NULL DEFAULT 0,
                    last_generated_at INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(session_id, canonical_id),
                    FOREIGN KEY (session_id)
                        REFERENCES session_settings(session_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (canonical_id)
                        REFERENCES canonical_identities(canonical_id)
                );

                CREATE TABLE IF NOT EXISTS identity_generation_cursors (
                    canonical_id TEXT NOT NULL,
                    preset_key TEXT NOT NULL,
                    last_transcript_id INTEGER NOT NULL DEFAULT 0,
                    last_generated_at INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(canonical_id, preset_key),
                    FOREIGN KEY (canonical_id)
                        REFERENCES canonical_identities(canonical_id)
                );

                CREATE TABLE IF NOT EXISTS migration_ledger (
                    migration_id TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    dry_run INTEGER NOT NULL,
                    started_at INTEGER NOT NULL,
                    completed_at INTEGER,
                    backup_path TEXT,
                    counts_json TEXT NOT NULL DEFAULT '{}',
                    verification_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS migration_quarantine (
                    quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    migration_id TEXT NOT NULL,
                    item_type TEXT NOT NULL,
                    source_table TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    UNIQUE(migration_id, source_table, source_key, reason),
                    FOREIGN KEY (migration_id)
                        REFERENCES migration_ledger(migration_id)
                        ON DELETE CASCADE
                );
                """
            )
            suppression_columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(memory_suppressions)"
                )
            }
            if "source_fact_id" not in suppression_columns:
                conn.execute(
                    """
                    ALTER TABLE memory_suppressions
                    ADD COLUMN source_fact_id TEXT
                    """
                )
            transcript_columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(raw_transcript_messages)"
                )
            }
            if "preset_key" not in transcript_columns:
                conn.execute(
                    """
                    ALTER TABLE raw_transcript_messages
                    ADD COLUMN preset_key TEXT NOT NULL DEFAULT 'default'
                    """
                )
            preference_columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(memory_preferences)"
                )
            }
            if "next_retry_at" not in preference_columns:
                conn.execute(
                    """
                    ALTER TABLE memory_preferences
                    ADD COLUMN next_retry_at INTEGER NOT NULL DEFAULT 0
                    """
                )
            if "failure_count" not in preference_columns:
                conn.execute(
                    """
                    ALTER TABLE memory_preferences
                    ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0
                    """
                )
            session_columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(session_settings)"
                )
            }
            if "agent_enabled" not in session_columns:
                conn.execute(
                    """
                    ALTER TABLE session_settings
                    ADD COLUMN agent_enabled INTEGER
                    """
                )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_raw_transcript_identity_preset
                ON raw_transcript_messages(
                    sender_canonical_id,
                    preset_key,
                    transcript_id
                )
                """
            )
            now = _now_ts()
            conn.execute(
                """
                INSERT INTO schema_meta(schema_name, version, updated_at)
                VALUES('jianer_ai_memory', ?, ?)
                ON CONFLICT(schema_name)
                DO UPDATE SET version=excluded.version, updated_at=excluded.updated_at
                """,
                (SCHEMA_VERSION, now),
            )
        finally:
            conn.close()

    def quick_check(self) -> tuple[str, ...]:
        conn = self._connect()
        try:
            return tuple(str(row[0]) for row in conn.execute("PRAGMA quick_check"))
        finally:
            conn.close()

    def foreign_key_check(self) -> tuple[tuple[Any, ...], ...]:
        conn = self._connect()
        try:
            return tuple(tuple(row) for row in conn.execute("PRAGMA foreign_key_check"))
        finally:
            conn.close()

    def _ensure_canonical_in_tx(
        self, conn: sqlite3.Connection, canonical_id: str, now: int
    ) -> str:
        canonical_id = _required_text(canonical_id, "canonical_user_id")
        conn.execute(
            """
            INSERT INTO canonical_identities(
                canonical_id, created_at, updated_at, merged_into
            )
            VALUES(?, ?, ?, NULL)
            ON CONFLICT(canonical_id)
            DO UPDATE SET updated_at=excluded.updated_at
            """,
            (canonical_id, now, now),
        )
        return self._resolve_canonical_in_tx(conn, canonical_id)

    def _resolve_canonical_in_tx(
        self, conn: sqlite3.Connection, canonical_id: str
    ) -> str:
        current = _required_text(canonical_id, "canonical_user_id")
        visited: set[str] = set()
        while True:
            if current in visited:
                raise MemoryStoreError("canonical identity merge cycle detected")
            visited.add(current)
            row = conn.execute(
                """
                SELECT merged_into
                FROM canonical_identities
                WHERE canonical_id = ?
                """,
                (current,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown canonical identity: {current}")
            parent = row["merged_into"]
            if parent is None:
                return current
            current = str(parent)

    def _ensure_identity_in_tx(
        self,
        conn: sqlite3.Connection,
        protocol: Any,
        self_id: Any,
        external_id: Any,
        now: int,
    ) -> str:
        protocol_s = normalize_protocol(protocol)
        self_id_s = _optional_text(self_id)
        external_id_s = _required_text(external_id, "external_id")
        row = conn.execute(
            """
            SELECT canonical_id
            FROM identity_aliases
            WHERE protocol = ? AND self_id = ? AND external_id = ?
            """,
            (protocol_s, self_id_s, external_id_s),
        ).fetchone()
        if row is not None:
            return self._resolve_canonical_in_tx(conn, str(row["canonical_id"]))

        canonical_id = _canonical_for_alias(protocol_s, self_id_s, external_id_s)
        canonical_id = self._ensure_canonical_in_tx(conn, canonical_id, now)
        conn.execute(
            """
            INSERT INTO identity_aliases(
                protocol, self_id, external_id, canonical_id, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(protocol, self_id, external_id)
            DO UPDATE SET updated_at=excluded.updated_at
            """,
            (
                protocol_s,
                self_id_s,
                external_id_s,
                canonical_id,
                now,
                now,
            ),
        )
        return canonical_id

    def ensure_identity(
        self, *, protocol: Any, self_id: Any, external_id: Any
    ) -> str:
        now = _now_ts()
        conn = self._connect()
        try:
            with self._transaction(conn):
                return self._ensure_identity_in_tx(
                    conn, protocol, self_id, external_id, now
                )
        finally:
            conn.close()

    def resolve_identity(
        self, protocol: Any, self_id: Any, external_id: Any
    ) -> str:
        return self.ensure_identity(
            protocol=protocol, self_id=self_id, external_id=external_id
        )

    def resolve_canonical_id(self, canonical_user_id: str) -> str:
        conn = self._connect()
        try:
            return self._resolve_canonical_in_tx(conn, canonical_user_id)
        finally:
            conn.close()

    def aliases_for(self, canonical_user_id: str) -> tuple[IdentityRef, ...]:
        conn = self._connect()
        try:
            root = self._resolve_canonical_in_tx(conn, canonical_user_id)
            rows = conn.execute(
                """
                SELECT protocol, self_id, external_id
                FROM identity_aliases
                WHERE canonical_id = ?
                ORDER BY protocol, self_id, external_id
                """,
                (root,),
            ).fetchall()
            return tuple(
                IdentityRef(
                    protocol=str(row["protocol"]),
                    self_id=str(row["self_id"]),
                    external_id=str(row["external_id"]),
                )
                for row in rows
            )
        finally:
            conn.close()

    def authorize(
        self,
        *,
        protocol: Any,
        self_id: Any,
        external_id: Any,
        canonical_user_id: Any,
        reason: str = "binding",
    ) -> bool:
        """Authorize an alias to merge into ``canonical_user_id``.

        Authorization is durable but does not itself merge data.  The separate
        merge step makes a JSON-authoritative binding/outbox retry idempotent.
        """

        now = _now_ts()
        protocol_s = normalize_protocol(protocol)
        self_id_s = _optional_text(self_id)
        external_id_s = _required_text(external_id, "external_id")
        reason_s = _required_text(reason, "reason")
        canonical_s = _required_text(canonical_user_id, "canonical_user_id")
        conn = self._connect()
        try:
            with self._transaction(conn):
                self._ensure_identity_in_tx(
                    conn, protocol_s, self_id_s, external_id_s, now
                )
                target = self._ensure_canonical_in_tx(conn, canonical_s, now)
                conn.execute(
                    """
                    UPDATE identity_authorizations
                    SET revoked_at = ?
                    WHERE source_protocol = ?
                      AND source_self_id = ?
                      AND source_external_id = ?
                      AND reason = ?
                      AND target_canonical_id <> ?
                      AND revoked_at IS NULL
                    """,
                    (
                        now,
                        protocol_s,
                        self_id_s,
                        external_id_s,
                        reason_s,
                        target,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO identity_authorizations(
                        source_protocol,
                        source_self_id,
                        source_external_id,
                        target_canonical_id,
                        reason,
                        authorized_at,
                        revoked_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, NULL)
                    ON CONFLICT(
                        source_protocol,
                        source_self_id,
                        source_external_id,
                        target_canonical_id,
                        reason
                    )
                    DO UPDATE SET authorized_at=excluded.authorized_at,
                                  revoked_at=NULL
                    """,
                    (
                        protocol_s,
                        self_id_s,
                        external_id_s,
                        target,
                        reason_s,
                        now,
                    ),
                )
            return True
        finally:
            conn.close()

    def revoke_authorization(
        self,
        *,
        protocol: Any,
        self_id: Any,
        external_id: Any,
        canonical_user_id: Any,
        reason: str = "binding",
    ) -> bool:
        protocol_s = normalize_protocol(protocol)
        self_id_s = _optional_text(self_id)
        external_id_s = _required_text(external_id, "external_id")
        canonical_s = _required_text(canonical_user_id, "canonical_user_id")
        reason_s = _required_text(reason, "reason")
        conn = self._connect()
        try:
            with self._transaction(conn):
                cursor = conn.execute(
                    """
                    UPDATE identity_authorizations
                    SET revoked_at = ?
                    WHERE source_protocol = ?
                      AND source_self_id = ?
                      AND source_external_id = ?
                      AND target_canonical_id = ?
                      AND reason = ?
                      AND revoked_at IS NULL
                    """,
                    (
                        _now_ts(),
                        protocol_s,
                        self_id_s,
                        external_id_s,
                        canonical_s,
                        reason_s,
                    ),
                )
            return cursor.rowcount > 0
        finally:
            conn.close()

    def _authorization_exists_in_tx(
        self,
        conn: sqlite3.Connection,
        *,
        source: IdentityRef,
        target_canonical_id: str,
        reason: str,
    ) -> bool:
        row = conn.execute(
            """
            SELECT 1
            FROM identity_authorizations
            WHERE source_protocol = ?
              AND source_self_id = ?
              AND source_external_id = ?
              AND target_canonical_id = ?
              AND reason = ?
              AND revoked_at IS NULL
            LIMIT 1
            """,
            (
                source.protocol,
                source.self_id,
                source.external_id,
                target_canonical_id,
                reason,
            ),
        ).fetchone()
        return row is not None

    def _invalidate_barrier_in_tx(
        self,
        conn: sqlite3.Connection,
        canonical_id: str,
        preset: str,
        now: int,
    ) -> int:
        conn.execute(
            """
            INSERT INTO generation_barriers(
                canonical_id,
                preset_key,
                version,
                active_generation_id,
                started_at,
                invalidated_at,
                completed_at
            )
            VALUES(?, ?, 1, NULL, ?, ?, NULL)
            ON CONFLICT(canonical_id, preset_key)
            DO UPDATE SET
                version=generation_barriers.version + 1,
                active_generation_id=NULL,
                invalidated_at=excluded.invalidated_at,
                completed_at=NULL
            """,
            (canonical_id, preset, now, now),
        )
        row = conn.execute(
            """
            SELECT version
            FROM generation_barriers
            WHERE canonical_id = ? AND preset_key = ?
            """,
            (canonical_id, preset),
        ).fetchone()
        return int(row["version"])

    def _merge_facts_in_tx(
        self,
        conn: sqlite3.Connection,
        source: str,
        target: str,
        now: int,
    ) -> None:
        source_facts = conn.execute(
            """
            SELECT *
            FROM memory_facts
            WHERE canonical_id = ?
            ORDER BY fact_id
            """,
            (source,),
        ).fetchall()
        for fact in source_facts:
            existing = conn.execute(
                """
                SELECT fact_id, weight, created_at, last_confirmed_at
                FROM memory_facts
                WHERE canonical_id = ?
                  AND preset_key = ?
                  AND fact_fingerprint = ?
                """,
                (
                    target,
                    str(fact["preset_key"]),
                    str(fact["fact_fingerprint"]),
                ),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    UPDATE memory_facts
                    SET canonical_id = ?, updated_at = ?
                    WHERE fact_id = ?
                    """,
                    (target, now, int(fact["fact_id"])),
                )
                continue

            target_fact_id = int(existing["fact_id"])
            evidence_rows = conn.execute(
                "SELECT * FROM memory_evidence WHERE fact_id = ?",
                (int(fact["fact_id"]),),
            ).fetchall()
            for evidence in evidence_rows:
                conn.execute(
                    """
                    INSERT INTO memory_evidence(
                        fact_id,
                        evidence_fingerprint,
                        content,
                        conversation_pk,
                        transcript_id,
                        observed_at,
                        metadata_json
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(fact_id, evidence_fingerprint) DO NOTHING
                    """,
                    (
                        target_fact_id,
                        str(evidence["evidence_fingerprint"]),
                        str(evidence["content"]),
                        evidence["conversation_pk"],
                        evidence["transcript_id"],
                        int(evidence["observed_at"]),
                        str(evidence["metadata_json"]),
                    ),
                )
            conn.execute(
                """
                UPDATE memory_facts
                SET weight = ?,
                    created_at = ?,
                    updated_at = ?,
                    last_confirmed_at = ?
                WHERE fact_id = ?
                """,
                (
                    max(float(existing["weight"]), float(fact["weight"])),
                    min(int(existing["created_at"]), int(fact["created_at"])),
                    now,
                    max(
                        int(existing["last_confirmed_at"]),
                        int(fact["last_confirmed_at"]),
                    ),
                    target_fact_id,
                ),
            )
            conn.execute(
                "DELETE FROM memory_facts WHERE fact_id = ?",
                (int(fact["fact_id"]),),
            )

    def _merge_suppressions_in_tx(
        self,
        conn: sqlite3.Connection,
        source: str,
        target: str,
    ) -> None:
        rows = conn.execute(
            "SELECT * FROM memory_suppressions WHERE canonical_id = ?",
            (source,),
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                INSERT INTO memory_suppressions(
                    canonical_id,
                    preset_key,
                    suppression_kind,
                    fingerprint,
                    content_snapshot,
                    source_fact_id,
                    reason,
                    deleted_at,
                    barrier_version
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    canonical_id,
                    preset_key,
                    suppression_kind,
                    fingerprint
                )
                DO UPDATE SET
                    deleted_at=MAX(
                        memory_suppressions.deleted_at,
                        excluded.deleted_at
                    ),
                    barrier_version=MAX(
                        memory_suppressions.barrier_version,
                        excluded.barrier_version
                    )
                """,
                (
                    target,
                    str(row["preset_key"]),
                    str(row["suppression_kind"]),
                    str(row["fingerprint"]),
                    str(row["content_snapshot"]),
                    row["source_fact_id"],
                    str(row["reason"]),
                    int(row["deleted_at"]),
                    int(row["barrier_version"]),
                ),
            )
        conn.execute(
            "DELETE FROM memory_suppressions WHERE canonical_id = ?",
            (source,),
        )

    def _merge_barriers_in_tx(
        self,
        conn: sqlite3.Connection,
        source: str,
        target: str,
        now: int,
    ) -> None:
        presets = {
            str(row["preset_key"])
            for row in conn.execute(
                """
                SELECT preset_key
                FROM generation_barriers
                WHERE canonical_id IN (?, ?)
                """,
                (source, target),
            )
        }
        for preset in presets:
            versions = [
                int(row["version"])
                for row in conn.execute(
                    """
                    SELECT version
                    FROM generation_barriers
                    WHERE canonical_id IN (?, ?) AND preset_key = ?
                    """,
                    (source, target, preset),
                )
            ]
            next_version = max(versions, default=0) + 1
            conn.execute(
                """
                INSERT INTO generation_barriers(
                    canonical_id,
                    preset_key,
                    version,
                    active_generation_id,
                    started_at,
                    invalidated_at,
                    completed_at
                )
                VALUES(?, ?, ?, NULL, ?, ?, NULL)
                ON CONFLICT(canonical_id, preset_key)
                DO UPDATE SET
                    version=excluded.version,
                    active_generation_id=NULL,
                    invalidated_at=excluded.invalidated_at,
                    completed_at=NULL
                """,
                (target, preset, next_version, now, now),
            )
        conn.execute(
            "DELETE FROM generation_barriers WHERE canonical_id = ?",
            (source,),
        )

    def _merge_cursors_in_tx(
        self,
        conn: sqlite3.Connection,
        source: str,
        target: str,
        now: int,
    ) -> None:
        rows = conn.execute(
            "SELECT * FROM generation_cursors WHERE canonical_id = ?",
            (source,),
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                INSERT INTO generation_cursors(
                    session_id,
                    canonical_id,
                    last_transcript_id,
                    last_generated_at,
                    updated_at
                )
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(session_id, canonical_id)
                DO UPDATE SET
                    last_transcript_id=MAX(
                        generation_cursors.last_transcript_id,
                        excluded.last_transcript_id
                    ),
                    last_generated_at=MAX(
                        generation_cursors.last_generated_at,
                        excluded.last_generated_at
                    ),
                    updated_at=excluded.updated_at
                """,
                (
                    int(row["session_id"]),
                    target,
                    int(row["last_transcript_id"]),
                    int(row["last_generated_at"]),
                    now,
                ),
            )
        conn.execute(
            "DELETE FROM generation_cursors WHERE canonical_id = ?",
            (source,),
        )
        identity_rows = conn.execute(
            """
            SELECT *
            FROM identity_generation_cursors
            WHERE canonical_id = ?
            """,
            (source,),
        ).fetchall()
        for row in identity_rows:
            conn.execute(
                """
                INSERT INTO identity_generation_cursors(
                    canonical_id,
                    preset_key,
                    last_transcript_id,
                    last_generated_at,
                    updated_at
                )
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(canonical_id, preset_key)
                DO UPDATE SET
                    last_transcript_id=MAX(
                        identity_generation_cursors.last_transcript_id,
                        excluded.last_transcript_id
                    ),
                    last_generated_at=MAX(
                        identity_generation_cursors.last_generated_at,
                        excluded.last_generated_at
                    ),
                    updated_at=excluded.updated_at
                """,
                (
                    target,
                    str(row["preset_key"]),
                    int(row["last_transcript_id"]),
                    int(row["last_generated_at"]),
                    now,
                ),
            )
        conn.execute(
            """
            DELETE FROM identity_generation_cursors
            WHERE canonical_id = ?
            """,
            (source,),
        )

        preference_rows = conn.execute(
            "SELECT * FROM memory_preferences WHERE canonical_id = ?",
            (source,),
        ).fetchall()
        for row in preference_rows:
            existing = conn.execute(
                """
                SELECT *
                FROM memory_preferences
                WHERE canonical_id = ? AND preset_key = ?
                """,
                (target, str(row["preset_key"])),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    UPDATE memory_preferences
                    SET canonical_id = ?, updated_at = ?
                    WHERE canonical_id = ? AND preset_key = ?
                    """,
                    (target, now, source, str(row["preset_key"])),
                )
                continue
            source_newer = int(row["updated_at"]) >= int(
                existing["updated_at"]
            )
            conn.execute(
                """
                UPDATE memory_preferences
                SET enabled = ?,
                    interval_seconds = ?,
                    last_generated_at = ?,
                    next_retry_at = ?,
                    failure_count = ?,
                    updated_at = ?
                WHERE canonical_id = ? AND preset_key = ?
                """,
                (
                    (
                        int(row["enabled"])
                        if source_newer
                        else int(existing["enabled"])
                    ),
                    (
                        int(row["interval_seconds"])
                        if source_newer
                        else int(existing["interval_seconds"])
                    ),
                    max(
                        int(row["last_generated_at"]),
                        int(existing["last_generated_at"]),
                    ),
                    max(
                        int(row["next_retry_at"]),
                        int(existing["next_retry_at"]),
                    ),
                    max(
                        int(row["failure_count"]),
                        int(existing["failure_count"]),
                    ),
                    now,
                    target,
                    str(row["preset_key"]),
                ),
            )
            conn.execute(
                """
                DELETE FROM memory_preferences
                WHERE canonical_id = ? AND preset_key = ?
                """,
                (source, str(row["preset_key"])),
            )

    def merge_identity(
        self,
        *,
        source_protocol: Any,
        source_self_id: Any,
        source_external_id: Any,
        target_protocol: Any = "qq",
        target_self_id: Any = "",
        target_external_id: Any,
        reason: str = "binding",
    ) -> bool:
        """Idempotently merge one alias into another canonical identity.

        OneBot and Milky aliases for the same QQ number are canonicalized to
        ``qq:<id>`` automatically.  Any other merge requires a preceding
        :meth:`authorize` call for the source alias and target canonical ID.
        """

        now = _now_ts()
        source = IdentityRef(
            protocol=normalize_protocol(source_protocol),
            self_id=_optional_text(source_self_id),
            external_id=_required_text(source_external_id, "source_external_id"),
        )
        target = IdentityRef(
            protocol=normalize_protocol(target_protocol),
            self_id=_optional_text(target_self_id),
            external_id=_required_text(target_external_id, "target_external_id"),
        )
        reason_s = _required_text(reason, "reason")
        conn = self._connect()
        try:
            with self._transaction(conn):
                source_canonical = self._ensure_identity_in_tx(
                    conn,
                    source.protocol,
                    source.self_id,
                    source.external_id,
                    now,
                )
                target_canonical = self._ensure_identity_in_tx(
                    conn,
                    target.protocol,
                    target.self_id,
                    target.external_id,
                    now,
                )
                source_canonical = self._resolve_canonical_in_tx(
                    conn, source_canonical
                )
                target_canonical = self._resolve_canonical_in_tx(
                    conn, target_canonical
                )

                automatic_qq = (
                    source.protocol in QQ_PROTOCOLS | {"qq"}
                    and target.protocol in QQ_PROTOCOLS | {"qq"}
                    and source.external_id == target.external_id
                )
                if source_canonical != target_canonical and not automatic_qq:
                    if not self._authorization_exists_in_tx(
                        conn,
                        source=source,
                        target_canonical_id=target_canonical,
                        reason=reason_s,
                    ):
                        raise IdentityAuthorizationError(
                            "cross-protocol identity merge requires authorize()"
                        )

                merge_key_payload = "\x00".join(
                    (
                        source.protocol,
                        source.self_id,
                        source.external_id,
                        target.protocol,
                        target.self_id,
                        target.external_id,
                        reason_s,
                    )
                )
                merge_key = hashlib.sha256(
                    merge_key_payload.encode("utf-8")
                ).hexdigest()
                feishu_rebind = False
                if (
                    source_canonical != target_canonical
                    and source.protocol == "feishu"
                    and reason_s == "feishu_binding"
                ):
                    other_alias = conn.execute(
                        """
                        SELECT 1
                        FROM identity_aliases
                        WHERE canonical_id = ?
                          AND NOT (
                              protocol = ?
                              AND self_id = ?
                              AND external_id = ?
                          )
                        LIMIT 1
                        """,
                        (
                            source_canonical,
                            source.protocol,
                            source.self_id,
                            source.external_id,
                        ),
                    ).fetchone()
                    feishu_rebind = other_alias is not None

                if feishu_rebind:
                    conn.execute(
                        """
                        UPDATE identity_aliases
                        SET canonical_id = ?, updated_at = ?
                        WHERE protocol = ?
                          AND self_id = ?
                          AND external_id = ?
                        """,
                        (
                            target_canonical,
                            now,
                            source.protocol,
                            source.self_id,
                            source.external_id,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO identity_merge_ledger(
                            merge_key,
                            source_canonical_id,
                            target_canonical_id,
                            source_protocol,
                            source_self_id,
                            source_external_id,
                            target_protocol,
                            target_self_id,
                            target_external_id,
                            reason,
                            outcome,
                            applied_at
                        )
                        VALUES(
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            'alias_reassigned', ?
                        )
                        ON CONFLICT(merge_key) DO UPDATE SET
                            source_canonical_id=excluded.source_canonical_id,
                            target_canonical_id=excluded.target_canonical_id,
                            outcome=excluded.outcome,
                            applied_at=excluded.applied_at
                        """,
                        (
                            merge_key,
                            source_canonical,
                            target_canonical,
                            source.protocol,
                            source.self_id,
                            source.external_id,
                            target.protocol,
                            target.self_id,
                            target.external_id,
                            reason_s,
                            now,
                        ),
                    )
                    return True

                if source_canonical == target_canonical:
                    conn.execute(
                        """
                        INSERT INTO identity_merge_ledger(
                            merge_key,
                            source_canonical_id,
                            target_canonical_id,
                            source_protocol,
                            source_self_id,
                            source_external_id,
                            target_protocol,
                            target_self_id,
                            target_external_id,
                            reason,
                            outcome,
                            applied_at
                        )
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'already_merged', ?)
                        ON CONFLICT(merge_key) DO NOTHING
                        """,
                        (
                            merge_key,
                            source_canonical,
                            target_canonical,
                            source.protocol,
                            source.self_id,
                            source.external_id,
                            target.protocol,
                            target.self_id,
                            target.external_id,
                            reason_s,
                            now,
                        ),
                    )
                    return True

                self._merge_facts_in_tx(
                    conn, source_canonical, target_canonical, now
                )
                self._merge_suppressions_in_tx(
                    conn, source_canonical, target_canonical
                )
                self._merge_barriers_in_tx(
                    conn, source_canonical, target_canonical, now
                )
                self._merge_cursors_in_tx(
                    conn, source_canonical, target_canonical, now
                )
                conn.execute(
                    """
                    UPDATE raw_transcript_messages
                    SET sender_canonical_id = ?
                    WHERE sender_canonical_id = ?
                    """,
                    (target_canonical, source_canonical),
                )
                conn.execute(
                    """
                    UPDATE identity_aliases
                    SET canonical_id = ?, updated_at = ?
                    WHERE canonical_id = ?
                    """,
                    (target_canonical, now, source_canonical),
                )
                conn.execute(
                    """
                    UPDATE canonical_identities
                    SET merged_into = ?, updated_at = ?
                    WHERE canonical_id = ?
                    """,
                    (target_canonical, now, source_canonical),
                )
                conn.execute(
                    """
                    INSERT INTO identity_merge_ledger(
                        merge_key,
                        source_canonical_id,
                        target_canonical_id,
                        source_protocol,
                        source_self_id,
                        source_external_id,
                        target_protocol,
                        target_self_id,
                        target_external_id,
                        reason,
                        outcome,
                        applied_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'merged', ?)
                    ON CONFLICT(merge_key) DO NOTHING
                    """,
                    (
                        merge_key,
                        source_canonical,
                        target_canonical,
                        source.protocol,
                        source.self_id,
                        source.external_id,
                        target.protocol,
                        target.self_id,
                        target.external_id,
                        reason_s,
                        now,
                    ),
                )
            return True
        finally:
            conn.close()

    def _ensure_conversation_in_tx(
        self,
        conn: sqlite3.Connection,
        *,
        protocol: Any,
        self_id: Any,
        conversation_kind: Any,
        conversation_id: Any,
        now: int,
    ) -> int:
        protocol_s = normalize_protocol(protocol)
        self_id_s = _required_text(self_id, "self_id")
        kind_s = _required_text(conversation_kind, "conversation_kind").lower()
        if kind_s not in CONVERSATION_KINDS:
            raise ValueError("conversation_kind must be 'group' or 'private'")
        conversation_id_s = _required_text(
            conversation_id, "conversation_id"
        )
        conn.execute(
            """
            INSERT INTO conversations(
                protocol,
                self_id,
                conversation_kind,
                conversation_id,
                created_at,
                last_seen_at
            )
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(protocol, self_id, conversation_kind, conversation_id)
            DO UPDATE SET last_seen_at=MAX(
                conversations.last_seen_at,
                excluded.last_seen_at
            )
            """,
            (
                protocol_s,
                self_id_s,
                kind_s,
                conversation_id_s,
                now,
                now,
            ),
        )
        row = conn.execute(
            """
            SELECT conversation_pk
            FROM conversations
            WHERE protocol = ?
              AND self_id = ?
              AND conversation_kind = ?
              AND conversation_id = ?
            """,
            (protocol_s, self_id_s, kind_s, conversation_id_s),
        ).fetchone()
        return int(row["conversation_pk"])

    def ensure_conversation(
        self,
        *,
        protocol: Any,
        self_id: Any,
        conversation_kind: Any,
        conversation_id: Any,
    ) -> int:
        now = _now_ts()
        conn = self._connect()
        try:
            with self._transaction(conn):
                return self._ensure_conversation_in_tx(
                    conn,
                    protocol=protocol,
                    self_id=self_id,
                    conversation_kind=conversation_kind,
                    conversation_id=conversation_id,
                    now=now,
                )
        finally:
            conn.close()

    def set_active_preset(
        self,
        *,
        protocol: Any,
        self_id: Any,
        conversation_kind: Any,
        conversation_id: Any,
        preset: Any,
    ) -> str:
        preset_s = normalize_preset(preset)
        now = _now_ts()
        conn = self._connect()
        try:
            with self._transaction(conn):
                conversation_pk = self._ensure_conversation_in_tx(
                    conn,
                    protocol=protocol,
                    self_id=self_id,
                    conversation_kind=conversation_kind,
                    conversation_id=conversation_id,
                    now=now,
                )
                conn.execute(
                    """
                    INSERT INTO conversation_preferences(
                        conversation_pk, active_preset, updated_at
                    )
                    VALUES(?, ?, ?)
                    ON CONFLICT(conversation_pk)
                    DO UPDATE SET active_preset=excluded.active_preset,
                                  updated_at=excluded.updated_at
                    """,
                    (conversation_pk, preset_s, now),
                )
            return preset_s
        finally:
            conn.close()

    def get_active_preset(
        self,
        *,
        protocol: Any,
        self_id: Any,
        conversation_kind: Any,
        conversation_id: Any,
    ) -> str | None:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT p.active_preset
                FROM conversation_preferences p
                JOIN conversations c
                  ON c.conversation_pk = p.conversation_pk
                WHERE c.protocol = ?
                  AND c.self_id = ?
                  AND c.conversation_kind = ?
                  AND c.conversation_id = ?
                """,
                (
                    normalize_protocol(protocol),
                    _required_text(self_id, "self_id"),
                    _required_text(
                        conversation_kind, "conversation_kind"
                    ).lower(),
                    _required_text(conversation_id, "conversation_id"),
                ),
            ).fetchone()
            if row is None:
                return None
            value = str(row["active_preset"] or "").strip()
            return value or None
        finally:
            conn.close()

    def _record_transcript_in_tx(
        self,
        conn: sqlite3.Connection,
        *,
        protocol: Any,
        self_id: Any,
        conversation_kind: Any,
        conversation_id: Any,
        message_id: Any,
        sender_protocol: Any | None,
        sender_self_id: Any | None,
        sender_external_id: Any | None,
        sender_canonical_id: str | None,
        preset: Any,
        content: Any,
        occurred_at: int | None,
        message_type: Any,
        now: int,
    ) -> TranscriptWriteResult:
        content_s = _required_text(content, "content")
        preset_s = normalize_preset(preset)
        occurred_at_i = int(occurred_at if occurred_at is not None else now)
        message_type_s = _required_text(message_type or "text", "message_type")
        conversation_pk = self._ensure_conversation_in_tx(
            conn,
            protocol=protocol,
            self_id=self_id,
            conversation_kind=conversation_kind,
            conversation_id=conversation_id,
            now=occurred_at_i,
        )
        if sender_canonical_id is not None:
            sender_canonical = self._ensure_canonical_in_tx(
                conn, sender_canonical_id, now
            )
            sender_protocol_s = (
                normalize_protocol(sender_protocol)
                if sender_protocol is not None
                else sender_canonical.split(":", 1)[0]
            )
            sender_self_id_s = _optional_text(sender_self_id)
            sender_external_id_s = (
                _required_text(sender_external_id, "sender_external_id")
                if sender_external_id is not None
                else sender_canonical.split(":", 1)[-1]
            )
        else:
            sender_protocol_s = normalize_protocol(sender_protocol)
            sender_self_id_s = _optional_text(sender_self_id)
            sender_external_id_s = _required_text(
                sender_external_id, "sender_external_id"
            )
            sender_canonical = self._ensure_identity_in_tx(
                conn,
                sender_protocol_s,
                sender_self_id_s,
                sender_external_id_s,
                now,
            )
        external_message_id = _optional_text(message_id)
        payload_seed = "\x00".join(
            (
                sender_protocol_s,
                sender_self_id_s,
                sender_external_id_s,
                str(occurred_at_i),
                message_type_s,
                content_s,
            )
        )
        payload_hash = hashlib.sha256(payload_seed.encode("utf-8")).hexdigest()
        message_key = (
            f"id:{external_message_id}"
            if external_message_id
            else f"fp:{payload_hash}"
        )
        cursor = conn.execute(
            """
            INSERT INTO raw_transcript_messages(
                conversation_pk,
                message_key,
                external_message_id,
                sender_canonical_id,
                sender_protocol,
                sender_self_id,
                sender_external_id,
                preset_key,
                content,
                occurred_at,
                message_type,
                payload_hash,
                created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_pk, message_key) DO NOTHING
            """,
            (
                conversation_pk,
                message_key,
                external_message_id,
                sender_canonical,
                sender_protocol_s,
                sender_self_id_s,
                sender_external_id_s,
                preset_s,
                content_s,
                occurred_at_i,
                message_type_s,
                payload_hash,
                now,
            ),
        )
        inserted = cursor.rowcount > 0
        row = conn.execute(
            """
            SELECT transcript_id
            FROM raw_transcript_messages
            WHERE conversation_pk = ? AND message_key = ?
            """,
            (conversation_pk, message_key),
        ).fetchone()
        return TranscriptWriteResult(
            transcript_id=int(row["transcript_id"]),
            inserted=inserted,
            conversation_pk=conversation_pk,
        )

    def record_transcript(
        self,
        *,
        protocol: Any,
        self_id: Any,
        conversation_kind: Any | None = None,
        kind: Any | None = None,
        conversation_id: Any,
        message_id: Any,
        sender_protocol: Any | None = None,
        sender_self_id: Any | None = None,
        sender_external_id: Any | None = None,
        sender_canonical: str | None = None,
        sender_canonical_id: str | None = None,
        content: Any,
        occurred_at: int | None = None,
        timestamp: int | None = None,
        message_type: Any = "text",
        preset: Any | None = None,
    ) -> TranscriptWriteResult:
        """Store one raw message.

        The active ``preset`` is stored on the physical message but is not part
        of its uniqueness key.  A retry therefore cannot duplicate or
        reclassify a message after the active preset has changed.
        """

        now = _now_ts()
        conversation_kind_value = (
            conversation_kind if conversation_kind is not None else kind
        )
        occurred_at_value = (
            occurred_at if occurred_at is not None else timestamp
        )
        conn = self._connect()
        try:
            with self._transaction(conn):
                canonical_value = (
                    sender_canonical_id
                    if sender_canonical_id is not None
                    else sender_canonical
                )
                result = self._record_transcript_in_tx(
                    conn,
                    protocol=protocol,
                    self_id=self_id,
                    conversation_kind=conversation_kind_value,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    sender_protocol=sender_protocol,
                    sender_self_id=sender_self_id,
                    sender_external_id=sender_external_id,
                    sender_canonical_id=canonical_value,
                    preset=preset,
                    content=content,
                    occurred_at=occurred_at_value,
                    message_type=message_type,
                    now=now,
                )
                if canonical_value is not None:
                    canonical = self._resolve_canonical_in_tx(
                        conn, canonical_value
                    )
                else:
                    canonical = self._ensure_identity_in_tx(
                        conn,
                        sender_protocol,
                        sender_self_id,
                        sender_external_id,
                        now,
                    )
                self._ensure_memory_preference_in_tx(
                    conn,
                    canonical_id=canonical,
                    preset=normalize_preset(preset),
                    now=now,
                )
                return result
        finally:
            conn.close()

    def redact_transcript_values(
        self,
        *,
        protocol: Any,
        self_id: Any,
        conversation_kind: Any,
        conversation_id: Any,
        message_id: Any,
        values: Iterable[Any],
        replacement: str = "[REDACTED]",
    ) -> bool:
        """Replace sensitive values in one already-recorded raw message."""

        external_message_id = _required_text(message_id, "message_id")
        secrets = tuple(
            sorted(
                {
                    str(value)
                    for value in values
                    if str(value)
                },
                key=len,
                reverse=True,
            )
        )
        if not secrets:
            return False
        conn = self._connect()
        try:
            with self._transaction(conn):
                row = conn.execute(
                    """
                    SELECT
                        r.transcript_id,
                        r.content,
                        r.sender_protocol,
                        r.sender_self_id,
                        r.sender_external_id,
                        r.occurred_at,
                        r.message_type
                    FROM raw_transcript_messages r
                    JOIN conversations c
                      ON c.conversation_pk = r.conversation_pk
                    WHERE c.protocol = ?
                      AND c.self_id = ?
                      AND c.conversation_kind = ?
                      AND c.conversation_id = ?
                      AND r.external_message_id = ?
                    ORDER BY r.transcript_id DESC
                    LIMIT 1
                    """,
                    (
                        normalize_protocol(protocol),
                        _required_text(self_id, "self_id"),
                        _required_text(
                            conversation_kind, "conversation_kind"
                        ).lower(),
                        _required_text(conversation_id, "conversation_id"),
                        external_message_id,
                    ),
                ).fetchone()
                if row is None:
                    return False
                content = str(row["content"])
                redacted = content
                for secret in secrets:
                    redacted = redacted.replace(secret, replacement)
                if redacted == content:
                    return False
                payload_seed = "\x00".join(
                    (
                        str(row["sender_protocol"] or ""),
                        str(row["sender_self_id"] or ""),
                        str(row["sender_external_id"] or ""),
                        str(int(row["occurred_at"])),
                        str(row["message_type"] or "text"),
                        redacted,
                    )
                )
                payload_hash = hashlib.sha256(
                    payload_seed.encode("utf-8")
                ).hexdigest()
                conn.execute(
                    """
                    UPDATE raw_transcript_messages
                    SET content = ?, payload_hash = ?
                    WHERE transcript_id = ?
                    """,
                    (redacted, payload_hash, int(row["transcript_id"])),
                )
                return True
        finally:
            conn.close()

    def count_transcripts(
        self,
        *,
        protocol: Any | None = None,
        self_id: Any | None = None,
        conversation_kind: Any | None = None,
        conversation_id: Any | None = None,
        preset: Any | None = None,
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if protocol is not None:
            clauses.append("c.protocol = ?")
            params.append(normalize_protocol(protocol))
        if self_id is not None:
            clauses.append("c.self_id = ?")
            params.append(_required_text(self_id, "self_id"))
        if conversation_kind is not None:
            clauses.append("c.conversation_kind = ?")
            params.append(
                _required_text(conversation_kind, "conversation_kind").lower()
            )
        if conversation_id is not None:
            clauses.append("c.conversation_id = ?")
            params.append(_required_text(conversation_id, "conversation_id"))
        if preset is not None:
            clauses.append("r.preset_key = ?")
            params.append(normalize_preset(preset))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = self._connect()
        try:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM raw_transcript_messages r
                JOIN conversations c
                  ON c.conversation_pk = r.conversation_pk
                {where}
                """,
                params,
            ).fetchone()
            return int(row["count"])
        finally:
            conn.close()

    def list_conversation_sender_ids(
        self,
        *,
        protocol: Any,
        self_id: Any,
        conversation_kind: Any,
        conversation_id: Any,
        limit: int = 200,
    ) -> tuple[str, ...]:
        """Return recently seen sender IDs for one concrete conversation."""

        limit_i = max(1, min(int(limit), 500))
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT r.sender_external_id, MAX(r.occurred_at) AS last_seen
                FROM raw_transcript_messages r
                JOIN conversations c
                  ON c.conversation_pk = r.conversation_pk
                WHERE c.protocol = ?
                  AND c.self_id = ?
                  AND c.conversation_kind = ?
                  AND c.conversation_id = ?
                  AND r.sender_external_id != ''
                GROUP BY r.sender_external_id
                ORDER BY last_seen DESC
                LIMIT ?
                """,
                (
                    normalize_protocol(protocol),
                    _required_text(self_id, "self_id"),
                    _required_text(
                        conversation_kind, "conversation_kind"
                    ).lower(),
                    _required_text(conversation_id, "conversation_id"),
                    limit_i,
                ),
            ).fetchall()
            return tuple(str(row["sender_external_id"]) for row in rows)
        finally:
            conn.close()

    def prune_group_transcripts(
        self,
        *,
        now: int | None = None,
        retention_days: int = GROUP_TRANSCRIPT_RETENTION_DAYS,
    ) -> int:
        retention_days_i = int(retention_days)
        if retention_days_i < 1:
            raise ValueError("retention_days must be at least 1")
        cutoff = int(now if now is not None else _now_ts()) - (
            retention_days_i * 86400
        )
        conn = self._connect()
        try:
            with self._transaction(conn):
                cursor = conn.execute(
                    """
                    DELETE FROM raw_transcript_messages
                    WHERE occurred_at < ?
                      AND conversation_pk IN (
                          SELECT conversation_pk
                          FROM conversations
                          WHERE conversation_kind = 'group'
                      )
                    """,
                    (cutoff,),
                )
            return int(cursor.rowcount)
        finally:
            conn.close()

    def purge_transcripts(
        self,
        *,
        now: int | None = None,
        retention_days: int = GROUP_TRANSCRIPT_RETENTION_DAYS,
    ) -> int:
        """Service-facing alias for the fixed group transcript retention."""

        return self.prune_group_transcripts(
            now=now, retention_days=retention_days
        )

    def _ensure_session_in_tx(
        self,
        conn: sqlite3.Connection,
        *,
        protocol: Any,
        self_id: Any,
        conversation_kind: Any,
        conversation_id: Any,
        preset: Any,
        now: int,
    ) -> int:
        kind = _required_text(conversation_kind, "conversation_kind").lower()
        conversation_pk = self._ensure_conversation_in_tx(
            conn,
            protocol=protocol,
            self_id=self_id,
            conversation_kind=kind,
            conversation_id=conversation_id,
            now=now,
        )
        preset_s = normalize_preset(preset)
        default_tts = 1 if kind == "group" else 0
        conn.execute(
            """
            INSERT INTO session_settings(
                conversation_pk,
                preset_key,
                model,
                persona,
                tts_enabled,
                ai_enabled,
                memory_enabled,
                memory_interval_seconds,
                last_generated_at,
                updated_at
            )
            VALUES(?, ?, NULL, '', ?, 1, 1, 21600, 0, ?)
            ON CONFLICT(conversation_pk, preset_key) DO NOTHING
            """,
            (conversation_pk, preset_s, default_tts, now),
        )
        row = conn.execute(
            """
            SELECT session_id
            FROM session_settings
            WHERE conversation_pk = ? AND preset_key = ?
            """,
            (conversation_pk, preset_s),
        ).fetchone()
        return int(row["session_id"])

    def ensure_session(
        self,
        *,
        protocol: Any,
        self_id: Any,
        conversation_kind: Any,
        conversation_id: Any,
        preset: Any = "default",
    ) -> int:
        now = _now_ts()
        conn = self._connect()
        try:
            with self._transaction(conn):
                return self._ensure_session_in_tx(
                    conn,
                    protocol=protocol,
                    self_id=self_id,
                    conversation_kind=conversation_kind,
                    conversation_id=conversation_id,
                    preset=preset,
                    now=now,
                )
        finally:
            conn.close()

    def update_session_settings(
        self,
        *,
        protocol: Any,
        self_id: Any,
        conversation_kind: Any,
        conversation_id: Any,
        preset: Any = "default",
        model: str | None = None,
        persona: str | None = None,
        tts_enabled: bool | None = None,
        ai_enabled: bool | None = None,
        agent_enabled: Any = _UNSET,
        memory_enabled: bool | None = None,
        memory_interval_seconds: int | None = None,
        last_generated_at: int | None = None,
    ) -> SessionSettings:
        now = _now_ts()
        conn = self._connect()
        try:
            with self._transaction(conn):
                session_id = self._ensure_session_in_tx(
                    conn,
                    protocol=protocol,
                    self_id=self_id,
                    conversation_kind=conversation_kind,
                    conversation_id=conversation_id,
                    preset=preset,
                    now=now,
                )
                assignments: list[str] = ["updated_at = ?"]
                params: list[Any] = [now]
                values = (
                    ("model", model),
                    ("persona", persona),
                    (
                        "tts_enabled",
                        None if tts_enabled is None else int(bool(tts_enabled)),
                    ),
                    (
                        "ai_enabled",
                        None if ai_enabled is None else int(bool(ai_enabled)),
                    ),
                    (
                        "memory_enabled",
                        (
                            None
                            if memory_enabled is None
                            else int(bool(memory_enabled))
                        ),
                    ),
                    ("last_generated_at", last_generated_at),
                )
                for column, value in values:
                    if value is not None:
                        assignments.append(f"{column} = ?")
                        params.append(value)
                if agent_enabled is not _UNSET:
                    assignments.append("agent_enabled = ?")
                    params.append(
                        None
                        if agent_enabled is None
                        else int(bool(agent_enabled))
                    )
                if memory_interval_seconds is not None:
                    seconds = int(memory_interval_seconds)
                    if seconds < 60:
                        raise ValueError(
                            "memory_interval_seconds must be at least 60"
                        )
                    assignments.append("memory_interval_seconds = ?")
                    params.append(seconds)
                params.append(session_id)
                conn.execute(
                    f"""
                    UPDATE session_settings
                    SET {", ".join(assignments)}
                    WHERE session_id = ?
                    """,
                    params,
                )
                row = conn.execute(
                    "SELECT * FROM session_settings WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            return self._session_from_row(row)
        finally:
            conn.close()

    def set_session_settings(
        self,
        *,
        protocol: Any,
        self_id: Any,
        conversation_kind: Any | None = None,
        kind: Any | None = None,
        conversation_id: Any,
        preset: Any = "default",
        model: str | None = None,
        persona: str | None = None,
        tts: bool | None = None,
        tts_enabled: bool | None = None,
        enabled: bool | None = None,
        ai_enabled: bool | None = None,
        agent_enabled: Any = _UNSET,
        memory_enabled: bool | None = None,
        interval: int | None = None,
        memory_interval_seconds: int | None = None,
        last_generated: int | None = None,
        last_generated_at: int | None = None,
    ) -> SessionSettings:
        """Service-facing spelling for :meth:`update_session_settings`."""

        return self.update_session_settings(
            protocol=protocol,
            self_id=self_id,
            conversation_kind=(
                conversation_kind
                if conversation_kind is not None
                else kind
            ),
            conversation_id=conversation_id,
            preset=preset,
            model=model,
            persona=persona,
            tts_enabled=(
                tts_enabled if tts_enabled is not None else tts
            ),
            ai_enabled=(
                ai_enabled if ai_enabled is not None else enabled
            ),
            agent_enabled=agent_enabled,
            memory_enabled=memory_enabled,
            memory_interval_seconds=(
                memory_interval_seconds
                if memory_interval_seconds is not None
                else interval
            ),
            last_generated_at=(
                last_generated_at
                if last_generated_at is not None
                else last_generated
            ),
        )

    def get_session_settings(
        self,
        *,
        protocol: Any,
        self_id: Any,
        conversation_kind: Any,
        conversation_id: Any,
        preset: Any = "default",
    ) -> SessionSettings:
        session_id = self.ensure_session(
            protocol=protocol,
            self_id=self_id,
            conversation_kind=conversation_kind,
            conversation_id=conversation_id,
            preset=preset,
        )
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM session_settings WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return self._session_from_row(row)
        finally:
            conn.close()

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> SessionSettings:
        return SessionSettings(
            session_id=int(row["session_id"]),
            conversation_pk=int(row["conversation_pk"]),
            preset=str(row["preset_key"]),
            model=str(row["model"]) if row["model"] is not None else None,
            persona=str(row["persona"]),
            tts_enabled=bool(row["tts_enabled"]),
            ai_enabled=bool(row["ai_enabled"]),
            agent_enabled=(
                None
                if row["agent_enabled"] is None
                else bool(row["agent_enabled"])
            ),
            memory_enabled=bool(row["memory_enabled"]),
            memory_interval_seconds=int(row["memory_interval_seconds"]),
            last_generated_at=int(row["last_generated_at"]),
            updated_at=int(row["updated_at"]),
        )

    def _ensure_memory_preference_in_tx(
        self,
        conn: sqlite3.Connection,
        *,
        canonical_id: str,
        preset: str,
        now: int,
    ) -> sqlite3.Row:
        canonical = self._resolve_canonical_in_tx(conn, canonical_id)
        preset_s = normalize_preset(preset)
        conn.execute(
            """
            INSERT INTO memory_preferences(
                canonical_id,
                preset_key,
                enabled,
                interval_seconds,
                last_generated_at,
                next_retry_at,
                failure_count,
                updated_at
            )
            VALUES(?, ?, ?, ?, 0, 0, 0, ?)
            ON CONFLICT(canonical_id, preset_key) DO NOTHING
            """,
            (
                canonical,
                preset_s,
                int(self.default_memory_enabled),
                self.default_memory_interval_seconds,
                now,
            ),
        )
        return conn.execute(
            """
            SELECT *
            FROM memory_preferences
            WHERE canonical_id = ? AND preset_key = ?
            """,
            (canonical, preset_s),
        ).fetchone()

    def set_memory_settings(
        self,
        *,
        canonical_user_id: str,
        preset: Any = "default",
        enabled: bool | None = None,
        interval_seconds: int | None = None,
    ) -> Mapping[str, Any]:
        now = _now_ts()
        preset_s = normalize_preset(preset)
        conn = self._connect()
        try:
            with self._transaction(conn):
                canonical = self._resolve_canonical_in_tx(
                    conn, canonical_user_id
                )
                self._ensure_memory_preference_in_tx(
                    conn,
                    canonical_id=canonical,
                    preset=preset_s,
                    now=now,
                )
                assignments = ["updated_at = ?"]
                params: list[Any] = [now]
                if enabled is not None:
                    assignments.append("enabled = ?")
                    params.append(int(bool(enabled)))
                if interval_seconds is not None:
                    interval = int(interval_seconds)
                    if interval < 60:
                        raise ValueError(
                            "interval_seconds must be at least 60"
                        )
                    assignments.append("interval_seconds = ?")
                    params.append(interval)
                params.extend((canonical, preset_s))
                conn.execute(
                    f"""
                    UPDATE memory_preferences
                    SET {", ".join(assignments)}
                    WHERE canonical_id = ? AND preset_key = ?
                    """,
                    params,
                )
                row = conn.execute(
                    """
                    SELECT *
                    FROM memory_preferences
                    WHERE canonical_id = ? AND preset_key = ?
                    """,
                    (canonical, preset_s),
                ).fetchone()
            return {
                "canonical_user_id": canonical,
                "preset": preset_s,
                "enabled": bool(row["enabled"]),
                "interval_seconds": int(row["interval_seconds"]),
                "last_generated_at": int(row["last_generated_at"]),
                "next_retry_at": int(row["next_retry_at"]),
                "failure_count": int(row["failure_count"]),
                "updated_at": int(row["updated_at"]),
            }
        finally:
            conn.close()

    def get_memory_status(
        self,
        *,
        canonical_user_id: str,
        preset: Any = "default",
    ) -> Mapping[str, Any]:
        now = _now_ts()
        preset_s = normalize_preset(preset)
        conn = self._connect()
        try:
            with self._transaction(conn):
                canonical = self._resolve_canonical_in_tx(
                    conn, canonical_user_id
                )
                preference = self._ensure_memory_preference_in_tx(
                    conn,
                    canonical_id=canonical,
                    preset=preset_s,
                    now=now,
                )
            cursor = conn.execute(
                """
                SELECT last_transcript_id, last_generated_at
                FROM identity_generation_cursors
                WHERE canonical_id = ? AND preset_key = ?
                """,
                (canonical, preset_s),
            ).fetchone()
            last_transcript_id = (
                int(cursor["last_transcript_id"])
                if cursor is not None
                else 0
            )
            raw_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM raw_transcript_messages
                    WHERE sender_canonical_id = ? AND preset_key = ?
                    """,
                    (canonical, preset_s),
                ).fetchone()[0]
            )
            new_raw_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM raw_transcript_messages
                    WHERE sender_canonical_id = ?
                      AND preset_key = ?
                      AND transcript_id > ?
                    """,
                    (canonical, preset_s, last_transcript_id),
                ).fetchone()[0]
            )
            memory_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM memory_facts
                    WHERE canonical_id = ? AND preset_key = ?
                    """,
                    (canonical, preset_s),
                ).fetchone()[0]
            )
            suppression_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM memory_suppressions
                    WHERE canonical_id = ? AND preset_key = ?
                    """,
                    (canonical, preset_s),
                ).fetchone()[0]
            )
            return {
                "canonical_user_id": canonical,
                "preset": preset_s,
                "enabled": bool(preference["enabled"]),
                "interval_seconds": int(preference["interval_seconds"]),
                "last_generated_at": max(
                    int(preference["last_generated_at"]),
                    (
                        int(cursor["last_generated_at"])
                        if cursor is not None
                        else 0
                    ),
                ),
                "next_retry_at": int(preference["next_retry_at"]),
                "failure_count": int(preference["failure_count"]),
                "raw_count": raw_count,
                "new_raw_count": new_raw_count,
                "memory_count": memory_count,
                "mem_count": memory_count,
                "suppression_count": suppression_count,
            }
        finally:
            conn.close()

    def list_due_memory_scopes(
        self,
        *,
        now: int | None = None,
        limit: int = 20,
    ) -> tuple[DueMemoryScope, ...]:
        now_i = int(now if now is not None else _now_ts())
        conn = self._connect()
        try:
            preferences = conn.execute(
                """
                SELECT *
                FROM memory_preferences
                WHERE enabled = 1
                  AND last_generated_at + interval_seconds <= ?
                  AND next_retry_at <= ?
                ORDER BY last_generated_at, updated_at
                LIMIT ?
                """,
                (now_i, now_i, max(1, int(limit) * 4)),
            ).fetchall()
            scopes: list[DueMemoryScope] = []
            for preference in preferences:
                canonical = self._resolve_canonical_in_tx(
                    conn, str(preference["canonical_id"])
                )
                cursor = conn.execute(
                    """
                    SELECT last_transcript_id
                    FROM identity_generation_cursors
                    WHERE canonical_id = ? AND preset_key = ?
                    """,
                    (canonical, str(preference["preset_key"])),
                ).fetchone()
                after_id = (
                    int(cursor["last_transcript_id"])
                    if cursor is not None
                    else 0
                )
                latest = conn.execute(
                    """
                    SELECT
                        c.protocol,
                        c.self_id,
                        c.conversation_kind,
                        c.conversation_id
                    FROM raw_transcript_messages r
                    JOIN conversations c
                      ON c.conversation_pk = r.conversation_pk
                    WHERE r.sender_canonical_id = ?
                      AND r.preset_key = ?
                      AND r.transcript_id > ?
                    ORDER BY r.transcript_id DESC
                    LIMIT 1
                    """,
                    (
                        canonical,
                        str(preference["preset_key"]),
                        after_id,
                    ),
                ).fetchone()
                if latest is None:
                    continue
                scopes.append(
                    DueMemoryScope(
                        canonical_user_id=canonical,
                        preset=str(preference["preset_key"]),
                        protocol=str(latest["protocol"]),
                        self_id=str(latest["self_id"]),
                        conversation_kind=str(latest["conversation_kind"]),
                        conversation_id=str(latest["conversation_id"]),
                        enabled=bool(preference["enabled"]),
                        interval_seconds=int(
                            preference["interval_seconds"]
                        ),
                        last_generated_at=int(
                            preference["last_generated_at"]
                        ),
                    )
                )
                if len(scopes) >= max(1, int(limit)):
                    break
            return tuple(scopes)
        finally:
            conn.close()

    def begin_generation(
        self, *, canonical_user_id: str, preset: Any = "default"
    ) -> GenerationToken:
        now = _now_ts()
        preset_s = normalize_preset(preset)
        generation_id = uuid.uuid4().hex
        conn = self._connect()
        try:
            with self._transaction(conn):
                canonical = self._resolve_canonical_in_tx(
                    conn, canonical_user_id
                )
                conn.execute(
                    """
                    INSERT INTO generation_barriers(
                        canonical_id,
                        preset_key,
                        version,
                        active_generation_id,
                        started_at,
                        invalidated_at,
                        completed_at
                    )
                    VALUES(?, ?, 1, ?, ?, NULL, NULL)
                    ON CONFLICT(canonical_id, preset_key)
                    DO UPDATE SET
                        version=generation_barriers.version + 1,
                        active_generation_id=excluded.active_generation_id,
                        started_at=excluded.started_at,
                        invalidated_at=NULL,
                        completed_at=NULL
                    """,
                    (canonical, preset_s, generation_id, now),
                )
                row = conn.execute(
                    """
                    SELECT version
                    FROM generation_barriers
                    WHERE canonical_id = ? AND preset_key = ?
                    """,
                    (canonical, preset_s),
                ).fetchone()
            return GenerationToken(
                canonical_user_id=canonical,
                preset=preset_s,
                generation_id=generation_id,
                version=int(row["version"]),
                started_at=now,
            )
        finally:
            conn.close()

    def _generation_token_current_in_tx(
        self, conn: sqlite3.Connection, token: GenerationToken
    ) -> tuple[bool, str]:
        canonical = self._resolve_canonical_in_tx(
            conn, token.canonical_user_id
        )
        row = conn.execute(
            """
            SELECT version, active_generation_id
            FROM generation_barriers
            WHERE canonical_id = ? AND preset_key = ?
            """,
            (canonical, normalize_preset(token.preset)),
        ).fetchone()
        current = bool(
            row is not None
            and int(row["version"]) == int(token.version)
            and str(row["active_generation_id"] or "") == token.generation_id
        )
        return current, canonical

    def fetch_generation_batch(
        self,
        *,
        canonical_user_id: str,
        protocol: Any | None = None,
        self_id: Any | None = None,
        conversation_kind: Any | None = None,
        kind: Any | None = None,
        conversation_id: Any | None = None,
        preset: Any = "default",
        min_rows: int = 1,
        limit: int = 200,
    ) -> GenerationBatch | None:
        """Fetch a transcript batch and atomically open its generation barrier.

        Supplying the conversation fields uses a per-session cursor.  Omitting
        them uses the canonical-user cursor expected by the plugin service and
        shares progress across private chats and groups.
        """

        limit_i = max(1, min(2_000, int(limit)))
        min_rows_i = max(1, int(min_rows))
        now = _now_ts()
        kind_value = (
            conversation_kind if conversation_kind is not None else kind
        )
        preset_s = normalize_preset(preset)
        conn = self._connect()
        try:
            with self._transaction(conn):
                canonical = self._resolve_canonical_in_tx(
                    conn, canonical_user_id
                )
                self._ensure_memory_preference_in_tx(
                    conn,
                    canonical_id=canonical,
                    preset=preset_s,
                    now=now,
                )
                use_session = all(
                    value is not None
                    for value in (
                        protocol,
                        self_id,
                        kind_value,
                        conversation_id,
                    )
                )
                if use_session:
                    session_id: int | None = self._ensure_session_in_tx(
                        conn,
                        protocol=protocol,
                        self_id=self_id,
                        conversation_kind=kind_value,
                        conversation_id=conversation_id,
                        preset=preset_s,
                        now=now,
                    )
                    session_row = conn.execute(
                        """
                        SELECT conversation_pk
                        FROM session_settings
                        WHERE session_id = ?
                        """,
                        (session_id,),
                    ).fetchone()
                    cursor_row = conn.execute(
                        """
                        SELECT last_transcript_id
                        FROM generation_cursors
                        WHERE session_id = ? AND canonical_id = ?
                        """,
                        (session_id, canonical),
                    ).fetchone()
                    after_id = (
                        int(cursor_row["last_transcript_id"])
                        if cursor_row is not None
                        else 0
                    )
                    rows = conn.execute(
                        """
                        SELECT
                            transcript_id,
                            conversation_pk,
                            sender_canonical_id,
                            content,
                            occurred_at,
                            message_type
                        FROM raw_transcript_messages
                        WHERE conversation_pk = ?
                          AND sender_canonical_id = ?
                          AND preset_key = ?
                          AND transcript_id > ?
                        ORDER BY transcript_id
                        LIMIT ?
                        """,
                        (
                            int(session_row["conversation_pk"]),
                            canonical,
                            preset_s,
                            after_id,
                            limit_i,
                        ),
                    ).fetchall()
                else:
                    session_id = None
                    cursor_row = conn.execute(
                        """
                        SELECT last_transcript_id
                        FROM identity_generation_cursors
                        WHERE canonical_id = ? AND preset_key = ?
                        """,
                        (canonical, preset_s),
                    ).fetchone()
                    after_id = (
                        int(cursor_row["last_transcript_id"])
                        if cursor_row is not None
                        else 0
                    )
                    rows = conn.execute(
                        """
                        SELECT
                            transcript_id,
                            conversation_pk,
                            sender_canonical_id,
                            content,
                            occurred_at,
                            message_type
                        FROM raw_transcript_messages
                        WHERE sender_canonical_id = ?
                          AND preset_key = ?
                          AND transcript_id > ?
                        ORDER BY transcript_id
                        LIMIT ?
                        """,
                        (canonical, preset_s, after_id, limit_i),
                    ).fetchall()
                if len(rows) < min_rows_i:
                    return None
                generation_id = uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO generation_barriers(
                        canonical_id,
                        preset_key,
                        version,
                        active_generation_id,
                        started_at,
                        invalidated_at,
                        completed_at
                    )
                    VALUES(?, ?, 1, ?, ?, NULL, NULL)
                    ON CONFLICT(canonical_id, preset_key)
                    DO UPDATE SET
                        version=generation_barriers.version + 1,
                        active_generation_id=excluded.active_generation_id,
                        started_at=excluded.started_at,
                        invalidated_at=NULL,
                        completed_at=NULL
                    """,
                    (canonical, preset_s, generation_id, now),
                )
                barrier_row = conn.execute(
                    """
                    SELECT version
                    FROM generation_barriers
                    WHERE canonical_id = ? AND preset_key = ?
                    """,
                    (canonical, preset_s),
                ).fetchone()
            messages = tuple(
                TranscriptRecord(
                    transcript_id=int(row["transcript_id"]),
                    conversation_pk=int(row["conversation_pk"]),
                    sender_canonical_id=(
                        str(row["sender_canonical_id"])
                        if row["sender_canonical_id"] is not None
                        else None
                    ),
                    content=str(row["content"]),
                    occurred_at=int(row["occurred_at"]),
                    message_type=str(row["message_type"]),
                )
                for row in rows
            )
            return GenerationBatch(
                token=GenerationToken(
                    canonical_user_id=canonical,
                    preset=preset_s,
                    generation_id=generation_id,
                    version=int(barrier_row["version"]),
                    started_at=now,
                ),
                session_id=session_id,
                canonical_user_id=canonical,
                preset=preset_s,
                messages=messages,
                last_transcript_id=messages[-1].transcript_id,
            )
        finally:
            conn.close()

    def _mark_generation_progress_in_tx(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: int,
        canonical_id: str,
        last_transcript_id: int,
        generated_at: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO generation_cursors(
                session_id,
                canonical_id,
                last_transcript_id,
                last_generated_at,
                updated_at
            )
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(session_id, canonical_id)
            DO UPDATE SET
                last_transcript_id=MAX(
                    generation_cursors.last_transcript_id,
                    excluded.last_transcript_id
                ),
                last_generated_at=MAX(
                    generation_cursors.last_generated_at,
                    excluded.last_generated_at
                ),
                updated_at=excluded.updated_at
            """,
            (
                int(session_id),
                canonical_id,
                int(last_transcript_id),
                int(generated_at),
                int(generated_at),
            ),
        )
        conn.execute(
            """
            UPDATE session_settings
            SET last_generated_at=MAX(last_generated_at, ?), updated_at=?
            WHERE session_id = ?
            """,
            (int(generated_at), int(generated_at), int(session_id)),
        )

    def mark_generation_progress(
        self,
        *,
        session_id: int,
        canonical_user_id: str,
        last_transcript_id: int,
        generated_at: int | None = None,
    ) -> None:
        now = int(generated_at if generated_at is not None else _now_ts())
        conn = self._connect()
        try:
            with self._transaction(conn):
                canonical = self._resolve_canonical_in_tx(
                    conn, canonical_user_id
                )
                self._mark_generation_progress_in_tx(
                    conn,
                    session_id=session_id,
                    canonical_id=canonical,
                    last_transcript_id=last_transcript_id,
                    generated_at=now,
                )
        finally:
            conn.close()

    def _is_suppressed_in_tx(
        self,
        conn: sqlite3.Connection,
        *,
        canonical_id: str,
        preset: str,
        kind: str,
        fingerprint: str,
    ) -> bool:
        row = conn.execute(
            """
            SELECT 1
            FROM memory_suppressions
            WHERE canonical_id = ?
              AND preset_key = ?
              AND suppression_kind = ?
              AND fingerprint = ?
            LIMIT 1
            """,
            (canonical_id, preset, kind, fingerprint),
        ).fetchone()
        return row is not None

    def _upsert_memory_in_tx(
        self,
        conn: sqlite3.Connection,
        *,
        canonical_id: str,
        preset: str,
        memory: GeneratedMemory,
        now: int,
        honor_suppressions: bool = True,
    ) -> tuple[str, int | None]:
        content = str(memory.content or "").strip()
        if not content:
            return "empty", None
        fact_fingerprint = memory_fingerprint(content)
        evidence_values = tuple(
            evidence
            for evidence in memory.evidence
            if str(evidence.content or "").strip()
            or str(evidence.fingerprint or "").strip()
        )
        evidence_fingerprints = tuple(
            _evidence_fingerprint(evidence) for evidence in evidence_values
        )
        if honor_suppressions:
            if self._is_suppressed_in_tx(
                conn,
                canonical_id=canonical_id,
                preset=preset,
                kind="fact",
                fingerprint=fact_fingerprint,
            ):
                return "suppressed", None
            if any(
                self._is_suppressed_in_tx(
                    conn,
                    canonical_id=canonical_id,
                    preset=preset,
                    kind="evidence",
                    fingerprint=fingerprint,
                )
                for fingerprint in evidence_fingerprints
            ):
                return "suppressed", None

        existing = conn.execute(
            """
            SELECT fact_id, weight
            FROM memory_facts
            WHERE canonical_id = ?
              AND preset_key = ?
              AND fact_fingerprint = ?
            """,
            (canonical_id, preset, fact_fingerprint),
        ).fetchone()
        weight = max(0.0, min(1.0, float(memory.weight)))
        if existing is None:
            cursor = conn.execute(
                """
                INSERT INTO memory_facts(
                    canonical_id,
                    preset_key,
                    fact_fingerprint,
                    content,
                    weight,
                    created_at,
                    updated_at,
                    last_confirmed_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical_id,
                    preset,
                    fact_fingerprint,
                    content,
                    weight,
                    now,
                    now,
                    now,
                ),
            )
            fact_id = int(cursor.lastrowid)
            outcome = "inserted"
        else:
            fact_id = int(existing["fact_id"])
            conn.execute(
                """
                UPDATE memory_facts
                SET content = ?,
                    weight = ?,
                    updated_at = ?,
                    last_confirmed_at = ?
                WHERE fact_id = ?
                """,
                (
                    content,
                    max(float(existing["weight"]), weight),
                    now,
                    now,
                    fact_id,
                ),
            )
            outcome = "updated"

        for evidence, fingerprint in zip(
            evidence_values, evidence_fingerprints
        ):
            conn.execute(
                """
                INSERT INTO memory_evidence(
                    fact_id,
                    evidence_fingerprint,
                    content,
                    conversation_pk,
                    transcript_id,
                    observed_at,
                    metadata_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fact_id, evidence_fingerprint)
                DO UPDATE SET
                    observed_at=MAX(
                        memory_evidence.observed_at,
                        excluded.observed_at
                    ),
                    metadata_json=excluded.metadata_json
                """,
                (
                    fact_id,
                    fingerprint,
                    str(evidence.content).strip(),
                    evidence.conversation_pk,
                    evidence.transcript_id,
                    int(evidence.observed_at or now),
                    _json(evidence.metadata),
                ),
            )
        return outcome, fact_id

    def insert_generated_memories(
        self,
        token: GenerationToken | None = None,
        memories: Iterable[GeneratedMemory | Mapping[str, Any] | str] = (),
        *,
        canonical_user_id: str | None = None,
        preset: Any = "default",
        generation_barrier: str | None = None,
        transcript_ids: Iterable[Any] | None = None,
        session_id: int | None = None,
        last_transcript_id: int | None = None,
    ) -> GenerationInsertResult:
        """Persist a completed generation after rechecking its barrier.

        The barrier and every fact/evidence tombstone are checked again under
        the same ``BEGIN IMMEDIATE`` transaction used for insertion.  A delete
        or identity merge that happened while the model was running therefore
        makes the old result stale before it can write.
        """

        items = tuple(_coerce_memory(item) for item in memories)
        now = _now_ts()
        conn = self._connect()
        try:
            with self._transaction(conn):
                if token is None:
                    if canonical_user_id is None or not generation_barrier:
                        raise ValueError(
                            "token or canonical_user_id/generation_barrier "
                            "is required"
                        )
                    canonical_for_token = self._resolve_canonical_in_tx(
                        conn, canonical_user_id
                    )
                    preset_for_token = normalize_preset(preset)
                    barrier_row = conn.execute(
                        """
                        SELECT version, started_at, active_generation_id
                        FROM generation_barriers
                        WHERE canonical_id = ? AND preset_key = ?
                        """,
                        (canonical_for_token, preset_for_token),
                    ).fetchone()
                    if (
                        barrier_row is None
                        or str(
                            barrier_row["active_generation_id"] or ""
                        )
                        != str(generation_barrier)
                    ):
                        return GenerationInsertResult(
                            accepted=False,
                            inserted=0,
                            updated=0,
                            skipped_suppressed=0,
                            stale_generation=True,
                        )
                    token = GenerationToken(
                        canonical_user_id=canonical_for_token,
                        preset=preset_for_token,
                        generation_id=str(generation_barrier),
                        version=int(barrier_row["version"]),
                        started_at=int(barrier_row["started_at"]),
                    )
                current, canonical = self._generation_token_current_in_tx(
                    conn, token
                )
                if not current:
                    return GenerationInsertResult(
                        accepted=False,
                        inserted=0,
                        updated=0,
                        skipped_suppressed=0,
                        stale_generation=True,
                    )
                preset = normalize_preset(token.preset)
                inserted = 0
                updated = 0
                suppressed = 0
                for item in items:
                    outcome, _ = self._upsert_memory_in_tx(
                        conn,
                        canonical_id=canonical,
                        preset=preset,
                        memory=item,
                        now=now,
                        honor_suppressions=True,
                    )
                    if outcome == "inserted":
                        inserted += 1
                    elif outcome == "updated":
                        updated += 1
                    elif outcome == "suppressed":
                        suppressed += 1

                # Deliberate second read before commit; the write transaction
                # prevents another deletion from interleaving after this.
                current, _ = self._generation_token_current_in_tx(conn, token)
                if not current:
                    raise StaleGenerationError(
                        "generation barrier changed during insertion"
                    )
                conn.execute(
                    """
                    UPDATE generation_barriers
                    SET active_generation_id=NULL, completed_at=?
                    WHERE canonical_id = ?
                      AND preset_key = ?
                      AND version = ?
                      AND active_generation_id = ?
                    """,
                    (
                        now,
                        canonical,
                        preset,
                        int(token.version),
                        token.generation_id,
                    ),
                )
                if session_id is not None and last_transcript_id is not None:
                    self._mark_generation_progress_in_tx(
                        conn,
                        session_id=int(session_id),
                        canonical_id=canonical,
                        last_transcript_id=int(last_transcript_id),
                        generated_at=now,
                    )
                transcript_id_values = [
                    int(value)
                    for value in (transcript_ids or ())
                    if str(value).isdigit()
                ]
                global_last_id = (
                    max(transcript_id_values)
                    if transcript_id_values
                    else (
                        int(last_transcript_id)
                        if last_transcript_id is not None
                        else None
                    )
                )
                if global_last_id is not None and session_id is None:
                    conn.execute(
                        """
                        INSERT INTO identity_generation_cursors(
                            canonical_id,
                            preset_key,
                            last_transcript_id,
                            last_generated_at,
                            updated_at
                        )
                        VALUES(?, ?, ?, ?, ?)
                        ON CONFLICT(canonical_id, preset_key)
                        DO UPDATE SET
                            last_transcript_id=MAX(
                                identity_generation_cursors.last_transcript_id,
                                excluded.last_transcript_id
                            ),
                            last_generated_at=MAX(
                                identity_generation_cursors.last_generated_at,
                                excluded.last_generated_at
                            ),
                            updated_at=excluded.updated_at
                        """,
                        (canonical, preset, global_last_id, now, now),
                    )
                self._ensure_memory_preference_in_tx(
                    conn,
                    canonical_id=canonical,
                    preset=preset,
                    now=now,
                )
                conn.execute(
                    """
                    UPDATE memory_preferences
                    SET last_generated_at=MAX(last_generated_at, ?),
                        next_retry_at=0,
                        failure_count=0,
                        updated_at=?
                    WHERE canonical_id = ? AND preset_key = ?
                    """,
                    (now, now, canonical, preset),
                )
            return GenerationInsertResult(
                accepted=True,
                inserted=inserted,
                updated=updated,
                skipped_suppressed=suppressed,
                stale_generation=False,
            )
        finally:
            conn.close()

    def defer_generation(
        self,
        token: GenerationToken,
        *,
        retry_base_seconds: int = DEFAULT_GENERATION_RETRY_SECONDS,
        retry_max_seconds: int = MAX_GENERATION_RETRY_SECONDS,
    ) -> Mapping[str, Any]:
        """Persist a bounded exponential retry after a failed model result."""

        base = max(5, int(retry_base_seconds))
        maximum = max(base, int(retry_max_seconds))
        now = _now_ts()
        conn = self._connect()
        try:
            with self._transaction(conn):
                current, canonical = self._generation_token_current_in_tx(
                    conn, token
                )
                if not current:
                    return {
                        "accepted": False,
                        "stale_generation": True,
                        "failure_count": 0,
                        "next_retry_at": 0,
                    }
                preset = normalize_preset(token.preset)
                preference = self._ensure_memory_preference_in_tx(
                    conn,
                    canonical_id=canonical,
                    preset=preset,
                    now=now,
                )
                failure_count = int(preference["failure_count"]) + 1
                delay = min(
                    maximum,
                    base * (2 ** min(failure_count - 1, 8)),
                )
                next_retry_at = now + delay
                conn.execute(
                    """
                    UPDATE generation_barriers
                    SET active_generation_id=NULL,
                        invalidated_at=?,
                        completed_at=?
                    WHERE canonical_id = ?
                      AND preset_key = ?
                      AND version = ?
                      AND active_generation_id = ?
                    """,
                    (
                        now,
                        now,
                        canonical,
                        preset,
                        int(token.version),
                        token.generation_id,
                    ),
                )
                conn.execute(
                    """
                    UPDATE memory_preferences
                    SET failure_count=?,
                        next_retry_at=?,
                        updated_at=?
                    WHERE canonical_id = ? AND preset_key = ?
                    """,
                    (
                        failure_count,
                        next_retry_at,
                        now,
                        canonical,
                        preset,
                    ),
                )
            return {
                "accepted": True,
                "stale_generation": False,
                "failure_count": failure_count,
                "next_retry_at": next_retry_at,
            }
        finally:
            conn.close()

    def list_memories(
        self,
        *,
        canonical_user_id: str,
        preset: Any = "default",
        limit: int = 100,
    ) -> tuple[MemoryRecord, ...]:
        preset_s = normalize_preset(preset)
        conn = self._connect()
        try:
            canonical = self._resolve_canonical_in_tx(
                conn, canonical_user_id
            )
            rows = conn.execute(
                """
                SELECT *
                FROM memory_facts
                WHERE canonical_id = ? AND preset_key = ?
                ORDER BY weight DESC, updated_at DESC, fact_id DESC
                LIMIT ?
                """,
                (canonical, preset_s, max(1, int(limit))),
            ).fetchall()
            records: list[MemoryRecord] = []
            for row in rows:
                evidence_rows = conn.execute(
                    """
                    SELECT *
                    FROM memory_evidence
                    WHERE fact_id = ?
                    ORDER BY observed_at DESC, evidence_id DESC
                    """,
                    (int(row["fact_id"]),),
                ).fetchall()
                evidence = tuple(
                    MemoryEvidence(
                        content=str(item["content"]),
                        conversation_pk=(
                            int(item["conversation_pk"])
                            if item["conversation_pk"] is not None
                            else None
                        ),
                        transcript_id=(
                            int(item["transcript_id"])
                            if item["transcript_id"] is not None
                            else None
                        ),
                        observed_at=int(item["observed_at"]),
                        metadata=json.loads(str(item["metadata_json"]) or "{}"),
                    )
                    for item in evidence_rows
                )
                records.append(
                    MemoryRecord(
                        fact_id=int(row["fact_id"]),
                        canonical_user_id=canonical,
                        preset=preset_s,
                        content=str(row["content"]),
                        fingerprint=str(row["fact_fingerprint"]),
                        weight=float(row["weight"]),
                        created_at=int(row["created_at"]),
                        updated_at=int(row["updated_at"]),
                        evidence=evidence,
                    )
                )
            return tuple(records)
        finally:
            conn.close()

    def query_memories(
        self,
        *,
        canonical_user_id: str,
        preset: Any = "default",
        query: str = "",
        limit: int = 6,
    ) -> tuple[MemoryRecord, ...]:
        """Return preset-isolated memories ranked by weight, age, and overlap."""

        candidates = self.list_memories(
            canonical_user_id=canonical_user_id,
            preset=preset,
            limit=max(60, int(limit) * 10),
        )
        query_tokens = set(
            re.findall(
                r"[a-z0-9_]{2,}|[\u4e00-\u9fff]",
                normalize_memory_text(query),
            )
        )
        now = _now_ts()

        def score(record: MemoryRecord) -> tuple[float, int]:
            content_tokens = set(
                re.findall(
                    r"[a-z0-9_]{2,}|[\u4e00-\u9fff]",
                    normalize_memory_text(record.content),
                )
            )
            overlap = (
                len(query_tokens & content_tokens) / max(1, len(query_tokens))
                if query_tokens
                else 0.0
            )
            age_days = max(0.0, (now - record.updated_at) / 86400.0)
            recency = 1.0 / (1.0 + age_days / 7.0)
            return (
                (0.55 * record.weight) + (0.30 * recency) + (0.15 * overlap),
                record.updated_at,
            )

        ranked = sorted(candidates, key=score, reverse=True)
        return tuple(ranked[: max(1, int(limit))])

    def _suppress_fact_in_tx(
        self,
        conn: sqlite3.Connection,
        *,
        canonical_id: str,
        preset: str,
        fact_fingerprint: str,
        fact_content: str,
        evidence: Sequence[tuple[str, str]],
        source_fact_id: str | None,
        reason: str,
        now: int,
    ) -> None:
        barrier_version = self._invalidate_barrier_in_tx(
            conn, canonical_id, preset, now
        )
        conn.execute(
            """
            INSERT INTO memory_suppressions(
                canonical_id,
                preset_key,
                suppression_kind,
                fingerprint,
                content_snapshot,
                source_fact_id,
                reason,
                deleted_at,
                barrier_version
            )
            VALUES(?, ?, 'fact', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                canonical_id,
                preset_key,
                suppression_kind,
                fingerprint
            )
            DO UPDATE SET
                content_snapshot=excluded.content_snapshot,
                source_fact_id=excluded.source_fact_id,
                reason=excluded.reason,
                deleted_at=excluded.deleted_at,
                barrier_version=excluded.barrier_version
            """,
            (
                canonical_id,
                preset,
                fact_fingerprint,
                fact_content,
                source_fact_id,
                reason,
                now,
                barrier_version,
            ),
        )
        for evidence_fingerprint, evidence_content in evidence:
            conn.execute(
                """
                INSERT INTO memory_suppressions(
                    canonical_id,
                    preset_key,
                    suppression_kind,
                    fingerprint,
                    content_snapshot,
                    source_fact_id,
                    reason,
                    deleted_at,
                    barrier_version
                )
                VALUES(?, ?, 'evidence', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    canonical_id,
                    preset_key,
                    suppression_kind,
                    fingerprint
                )
                DO UPDATE SET
                    content_snapshot=excluded.content_snapshot,
                    source_fact_id=excluded.source_fact_id,
                    reason=excluded.reason,
                    deleted_at=excluded.deleted_at,
                    barrier_version=excluded.barrier_version
                """,
                (
                    canonical_id,
                    preset,
                    evidence_fingerprint,
                    evidence_content,
                    source_fact_id,
                    reason,
                    now,
                    barrier_version,
                ),
            )

    def delete_memory(
        self,
        *,
        canonical_user_id: str,
        preset: Any = "default",
        fact_id: int | None = None,
        memory_id: str | int | None = None,
        content: str | None = None,
        reason: str = "user_deleted",
    ) -> bool:
        if fact_id is None and memory_id is not None:
            try:
                fact_id = int(memory_id)
            except (TypeError, ValueError):
                return False
        if fact_id is None and not str(content or "").strip():
            raise ValueError("fact_id or content is required")
        preset_s = normalize_preset(preset)
        reason_s = _required_text(reason, "reason")
        now = _now_ts()
        conn = self._connect()
        try:
            with self._transaction(conn):
                canonical = self._resolve_canonical_in_tx(
                    conn, canonical_user_id
                )
                if fact_id is not None:
                    row = conn.execute(
                        """
                        SELECT *
                        FROM memory_facts
                        WHERE fact_id = ?
                          AND canonical_id = ?
                          AND preset_key = ?
                        """,
                        (int(fact_id), canonical, preset_s),
                    ).fetchone()
                else:
                    fingerprint = memory_fingerprint(content)
                    row = conn.execute(
                        """
                        SELECT *
                        FROM memory_facts
                        WHERE canonical_id = ?
                          AND preset_key = ?
                          AND fact_fingerprint = ?
                        """,
                        (canonical, preset_s, fingerprint),
                    ).fetchone()

                if row is None:
                    if content is None:
                        return False
                    fact_content = str(content).strip()
                    self._suppress_fact_in_tx(
                        conn,
                        canonical_id=canonical,
                        preset=preset_s,
                        fact_fingerprint=memory_fingerprint(fact_content),
                        fact_content=fact_content,
                        evidence=(),
                        source_fact_id=None,
                        reason=reason_s,
                        now=now,
                    )
                    return True

                evidence_rows = conn.execute(
                    """
                    SELECT evidence_fingerprint, content
                    FROM memory_evidence
                    WHERE fact_id = ?
                    """,
                    (int(row["fact_id"]),),
                ).fetchall()
                self._suppress_fact_in_tx(
                    conn,
                    canonical_id=canonical,
                    preset=preset_s,
                    fact_fingerprint=str(row["fact_fingerprint"]),
                    fact_content=str(row["content"]),
                    evidence=tuple(
                        (
                            str(item["evidence_fingerprint"]),
                            str(item["content"]),
                        )
                        for item in evidence_rows
                    ),
                    source_fact_id=str(row["fact_id"]),
                    reason=reason_s,
                    now=now,
                )
                conn.execute(
                    "DELETE FROM memory_facts WHERE fact_id = ?",
                    (int(row["fact_id"]),),
                )
            return True
        finally:
            conn.close()

    def clear_memories(
        self,
        *,
        canonical_user_id: str,
        preset: Any = "default",
        reason: str = "user_cleared",
    ) -> int:
        records = self.list_memories(
            canonical_user_id=canonical_user_id,
            preset=preset,
            limit=1_000_000,
        )
        deleted = 0
        for record in records:
            if self.delete_memory(
                canonical_user_id=canonical_user_id,
                preset=preset,
                fact_id=record.fact_id,
                reason=reason,
            ):
                deleted += 1
        if not records:
            conn = self._connect()
            try:
                with self._transaction(conn):
                    canonical = self._resolve_canonical_in_tx(
                        conn, canonical_user_id
                    )
                    self._invalidate_barrier_in_tx(
                        conn, canonical, normalize_preset(preset), _now_ts()
                    )
            finally:
                conn.close()
        return deleted

    def restore_suppression(
        self,
        *,
        canonical_user_id: str,
        preset: Any = "default",
        content: str,
        include_evidence: bool = False,
    ) -> int:
        canonical = self.resolve_canonical_id(canonical_user_id)
        preset_s = normalize_preset(preset)
        fingerprint = memory_fingerprint(content)
        kinds = ("fact", "evidence") if include_evidence else ("fact",)
        placeholders = ",".join("?" for _ in kinds)
        conn = self._connect()
        try:
            with self._transaction(conn):
                cursor = conn.execute(
                    f"""
                    DELETE FROM memory_suppressions
                    WHERE canonical_id = ?
                      AND preset_key = ?
                      AND suppression_kind IN ({placeholders})
                      AND fingerprint = ?
                    """,
                    (canonical, preset_s, *kinds, fingerprint),
                )
                if cursor.rowcount:
                    self._invalidate_barrier_in_tx(
                        conn, canonical, preset_s, _now_ts()
                    )
            return int(cursor.rowcount)
        finally:
            conn.close()

    def restore_memory(
        self,
        *,
        canonical_user_id: str,
        preset: Any = "default",
        memory_id: str | int | None = None,
        fact_id: str | int | None = None,
        content: str | None = None,
        include_evidence: bool = False,
    ) -> bool:
        stable_id = fact_id if fact_id is not None else memory_id
        if stable_id is not None:
            canonical = self.resolve_canonical_id(canonical_user_id)
            preset_s = normalize_preset(preset)
            conn = self._connect()
            try:
                with self._transaction(conn):
                    cursor = conn.execute(
                        """
                        DELETE FROM memory_suppressions
                        WHERE canonical_id = ?
                          AND preset_key = ?
                          AND source_fact_id = ?
                        """,
                        (canonical, preset_s, str(stable_id)),
                    )
                    if cursor.rowcount:
                        self._invalidate_barrier_in_tx(
                            conn, canonical, preset_s, _now_ts()
                        )
                return cursor.rowcount > 0
            finally:
                conn.close()
        if not str(content or "").strip():
            return False
        return (
            self.restore_suppression(
                canonical_user_id=canonical_user_id,
                preset=preset,
                content=str(content),
                include_evidence=include_evidence,
            )
            > 0
        )

    def list_suppressions(
        self,
        *,
        canonical_user_id: str,
        preset: Any = "default",
    ) -> tuple[dict[str, Any], ...]:
        conn = self._connect()
        try:
            canonical = self._resolve_canonical_in_tx(
                conn, canonical_user_id
            )
            rows = conn.execute(
                """
                SELECT *
                FROM memory_suppressions
                WHERE canonical_id = ? AND preset_key = ?
                ORDER BY deleted_at DESC, suppression_kind, fingerprint
                """,
                (canonical, normalize_preset(preset)),
            ).fetchall()
            values: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                item["memory_id"] = item.get("source_fact_id")
                values.append(item)
            return tuple(values)
        finally:
            conn.close()


def authorize(
    store: JianerMemoryStore,
    *,
    protocol: Any,
    self_id: Any,
    external_id: Any,
    canonical_user_id: Any,
    reason: str = "binding",
) -> bool:
    """Host-facing fixed authorization API."""

    return store.authorize(
        protocol=protocol,
        self_id=self_id,
        external_id=external_id,
        canonical_user_id=canonical_user_id,
        reason=reason,
    )


def merge_identity(
    store: JianerMemoryStore,
    *,
    source_protocol: Any,
    source_self_id: Any,
    source_external_id: Any,
    target_protocol: Any = "qq",
    target_self_id: Any = "",
    target_external_id: Any,
    reason: str = "binding",
) -> bool:
    """Host-facing fixed identity merge API."""

    return store.merge_identity(
        source_protocol=source_protocol,
        source_self_id=source_self_id,
        source_external_id=source_external_id,
        target_protocol=target_protocol,
        target_self_id=target_self_id,
        target_external_id=target_external_id,
        reason=reason,
    )
