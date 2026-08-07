"""Persona- and conversation-partitioned persistence for JianerAI.

Global control tables keep only registrations, settings, jobs, and audits.
Each persona owns five physical memory tables, while every group or private
conversation owns one physical objective-chat table.
The store is intentionally synchronous.  Event-loop callers should use
``asyncio.to_thread`` for operations that may touch disk.
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


SCHEMA_VERSION = 5
CHAT_TRANSCRIPT_RETENTION_DAYS = 90
GROUP_TRANSCRIPT_RETENTION_DAYS = CHAT_TRANSCRIPT_RETENTION_DAYS
DEFAULT_MEMORY_INTERVAL_SECONDS = 6 * 3600
DEFAULT_GENERATION_RETRY_SECONDS = 5 * 60
MAX_GENERATION_RETRY_SECONDS = 6 * 3600
QQ_PROTOCOLS = frozenset({"onebot", "milky"})
CONVERSATION_KINDS = frozenset({"group", "private"})
MEMORY_SCOPES = frozenset({"person", "group"})
_UNSET = object()


class MemoryStoreError(RuntimeError):
    """Base error for the JianerAI memory store."""


class IdentityAuthorizationError(MemoryStoreError):
    """Raised when a cross-identity merge has not been authorized."""


class StaleGenerationError(MemoryStoreError):
    """Raised when a background generation result belongs to an old barrier."""


class MemoryConflictError(MemoryStoreError):
    """Raised when a direct edit would duplicate another memory in the scope."""


class MemoryMigrationRequiredError(MemoryStoreError):
    """Raised when a legacy content database needs explicit v5 migration."""


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
    scope: str = "person"
    subject_id: str = ""
    canonical_fact: str = ""
    confidence: float = 1.0
    source_count: int = 0

    @property
    def id(self) -> str:
        return str(self.fact_id)

    @property
    def memory_id(self) -> str:
        return str(self.fact_id)


@dataclass(frozen=True)
class MemoryWriteResult:
    fact_id: int
    content: str
    weight: float
    outcome: str
    scope: str = "person"
    subject_id: str = ""

    @property
    def id(self) -> str:
        return str(self.fact_id)

    @property
    def memory_id(self) -> str:
        return str(self.fact_id)


@dataclass(frozen=True)
class ConversationEpisode:
    episode_id: int
    preset: str
    conversation_pk: int
    protocol: str
    self_id: str
    conversation_kind: str
    conversation_id: str
    speaker_canonical_id: str
    user_content: str
    assistant_content: str
    occurred_at: int
    updated_at: int
    send_state: str = "sent"
    review_state: str = "pending"
    reviewed_at: int | None = None
    review_error: str | None = None

    @property
    def id(self) -> str:
        return str(self.episode_id)


@dataclass(frozen=True)
class PersonaMemoryPartition:
    preset: str
    partition_id: str
    people_table: str
    groups_table: str
    evidence_table: str
    episodes_table: str
    suppressions_table: str
    persona_id: int = 0
    slug: str = "persona"


@dataclass(frozen=True)
class ChatPartition:
    conversation_pk: int
    protocol: str
    self_id: str
    conversation_kind: str
    conversation_id: str
    table_name: str
    retention_days: int


@dataclass(frozen=True)
class ChatMessage:
    message_uid: int
    conversation_pk: int
    message_key: str
    direction: str
    sender_canonical_id: str | None
    sender_name: str
    content: str
    occurred_at: int
    message_type: str
    active_preset: str

    @property
    def id(self) -> str:
        return str(self.message_uid)


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


def persona_partition_id(preset: Any) -> str:
    """Return the SQL-safe, stable physical partition ID for a persona."""

    value = normalize_preset(preset)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_identifier_slug(value: Any, *, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "")).casefold()
    normalized = normalized.encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    if not normalized:
        normalized = fallback
    return normalized[:24]


def _persona_table_names(
    persona_id: int,
    slug: str,
) -> tuple[str, str, str, str, str]:
    persona_number = int(persona_id)
    if persona_number <= 0:
        raise MemoryStoreError("invalid persona ID")
    safe_slug = _safe_identifier_slug(slug, fallback="persona")
    prefix = f"mem_p{persona_number:04d}_{safe_slug}"
    if re.fullmatch(r"[a-z0-9_]+", prefix) is None:
        raise MemoryStoreError("invalid persona memory table prefix")
    return (
        f"{prefix}_people",
        f"{prefix}_groups",
        f"{prefix}_evidence",
        f"{prefix}_episodes",
        f"{prefix}_deleted",
    )


def _chat_table_name(
    conversation_pk: int,
    *,
    protocol: str,
    conversation_kind: str,
    conversation_id: str,
) -> str:
    numeric_id = int(conversation_pk)
    if numeric_id <= 0:
        raise MemoryStoreError("invalid conversation ID")
    kind = "g" if conversation_kind == "group" else "u"
    external_id = str(conversation_id).strip()
    protocol_slug = _safe_identifier_slug(protocol, fallback="protocol")[:8]
    if external_id.isdecimal():
        if conversation_kind == "group":
            visible = external_id
        elif normalize_protocol(protocol) in QQ_PROTOCOLS:
            visible = f"qq{external_id}"
        else:
            visible = f"{protocol_slug}{external_id}"
    else:
        # Non-numeric adapter IDs can contain untrusted or very long values.
        # Keep only a short protocol cue and a stable digest in SQL names.
        digest = hashlib.sha256(external_id.encode("utf-8")).hexdigest()[:8]
        visible = f"{protocol_slug}_{digest}"
    safe = _safe_identifier_slug(visible, fallback="chat")
    return f"chat_{kind}{numeric_id:06d}_{safe}"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (str(table),),
    ).fetchone()
    return row is not None


def _memory_scope(value: Any) -> str:
    scope = str(value or "person").strip().casefold()
    if scope not in MEMORY_SCOPES:
        raise ValueError("memory scope must be 'person' or 'group'")
    return scope


def _group_subject_key(protocol: Any, self_id: Any, group_id: Any) -> str:
    protocol_s = normalize_protocol(protocol)
    self_id_s = _required_text(self_id, "self_id")
    group_id_s = _required_text(group_id, "group_id")
    seed = "\x00".join((protocol_s, self_id_s, group_id_s))
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"group:{digest}"


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

    def _ensure_persona_partition_in_tx(
        self,
        conn: sqlite3.Connection,
        preset: Any,
        now: int,
    ) -> PersonaMemoryPartition:
        preset_s = normalize_preset(preset)
        slug = _safe_identifier_slug(
            preset_s,
            fallback=(
                "persona_"
                + hashlib.sha256(preset_s.encode("utf-8")).hexdigest()[:8]
            ),
        )
        persona_row = conn.execute(
            """
            SELECT persona_id, slug, revision
            FROM sys_personas
            WHERE preset_key = ?
            """,
            (preset_s,),
        ).fetchone()
        if persona_row is None:
            conn.execute(
                """
                INSERT INTO sys_personas(
                    preset_key,
                    display_name,
                    slug,
                    revision,
                    created_at,
                    updated_at
                )
                VALUES(?, ?, ?, 1, ?, ?)
                """,
                (preset_s, preset_s, slug, now, now),
            )
        else:
            conn.execute(
                """
                UPDATE sys_personas
                SET display_name = ?, updated_at = ?
                WHERE preset_key = ?
                """,
                (preset_s, now, preset_s),
            )
        persona_row = conn.execute(
            """
            SELECT persona_id, slug, revision
            FROM sys_personas
            WHERE preset_key = ?
            """,
            (preset_s,),
        ).fetchone()
        if persona_row is None:
            raise MemoryStoreError("failed to register persona")
        persona_id = int(persona_row["persona_id"])
        slug = str(persona_row["slug"])
        partition_id = f"p{persona_id:04d}_{slug}"
        people, groups, evidence, episodes, suppressions = _persona_table_names(
            persona_id,
            slug,
        )
        existing = conn.execute(
            """
            SELECT *
            FROM sys_persona_partitions
            WHERE persona_id = ? OR preset_key = ? OR partition_id = ?
            """,
            (persona_id, preset_s, partition_id),
        ).fetchone()
        if existing is not None:
            expected = (
                preset_s,
                partition_id,
                people,
                groups,
                evidence,
                episodes,
                suppressions,
            )
            actual = (
                str(existing["preset_key"]),
                str(existing["partition_id"]),
                str(existing["people_table"]),
                str(existing["groups_table"]),
                str(existing["evidence_table"]),
                str(existing["episodes_table"]),
                str(existing["suppressions_table"]),
            )
            if actual != expected:
                raise MemoryStoreError(
                    "persona memory partition registry is inconsistent"
                )

        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {people} (
                memory_id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id TEXT NOT NULL,
                memory_text TEXT NOT NULL,
                importance REAL NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0,
                updated_at INTEGER NOT NULL,
                canonical_fact TEXT NOT NULL,
                semantic_hash TEXT NOT NULL,
                source_count INTEGER NOT NULL DEFAULT 0,
                persona_revision INTEGER NOT NULL DEFAULT 1,
                first_seen_at INTEGER NOT NULL,
                last_confirmed_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                origin TEXT NOT NULL DEFAULT 'persona',
                UNIQUE(person_id, semantic_hash),
                FOREIGN KEY (person_id)
                    REFERENCES sys_identities(canonical_id)
            )
            """
        )
        conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_p{persona_id:04d}_people_lookup
            ON {people}(person_id, updated_at DESC)
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {groups} (
                memory_id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_ref TEXT NOT NULL,
                memory_text TEXT NOT NULL,
                importance REAL NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0,
                updated_at INTEGER NOT NULL,
                canonical_fact TEXT NOT NULL,
                semantic_hash TEXT NOT NULL,
                source_count INTEGER NOT NULL DEFAULT 0,
                persona_revision INTEGER NOT NULL DEFAULT 1,
                first_seen_at INTEGER NOT NULL,
                last_confirmed_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                last_writer_person_id TEXT,
                UNIQUE(group_ref, semantic_hash),
                FOREIGN KEY (last_writer_person_id)
                    REFERENCES sys_identities(canonical_id)
            )
            """
        )
        conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_p{persona_id:04d}_groups_lookup
            ON {groups}(group_ref, updated_at DESC)
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {evidence} (
                evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_scope TEXT NOT NULL
                    CHECK(memory_scope IN ('person', 'group')),
                memory_id INTEGER NOT NULL,
                conversation_ref INTEGER,
                message_key TEXT,
                excerpt TEXT NOT NULL,
                observed_at INTEGER NOT NULL,
                evidence_hash TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{{}}',
                UNIQUE(memory_scope, memory_id, evidence_hash),
                FOREIGN KEY (conversation_ref)
                    REFERENCES sys_conversations(conversation_pk)
                    ON DELETE SET NULL
            )
            """
        )
        conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_p{persona_id:04d}_evidence_memory
            ON {evidence}(memory_scope, memory_id, observed_at DESC)
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {episodes} (
                episode_id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_ref INTEGER NOT NULL,
                exchange_key TEXT NOT NULL,
                person_id TEXT NOT NULL,
                user_text TEXT NOT NULL,
                assistant_text TEXT NOT NULL,
                occurred_at INTEGER NOT NULL,
                send_state TEXT NOT NULL DEFAULT 'sent'
                    CHECK(send_state IN ('pending', 'sent', 'failed')),
                review_state TEXT NOT NULL DEFAULT 'pending'
                    CHECK(review_state IN (
                        'pending', 'reviewing', 'completed', 'failed'
                    )),
                reviewed_at INTEGER,
                review_error TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(conversation_ref, exchange_key),
                FOREIGN KEY (conversation_ref)
                    REFERENCES sys_conversations(conversation_pk)
                    ON DELETE CASCADE,
                FOREIGN KEY (person_id)
                    REFERENCES sys_identities(canonical_id)
            )
            """
        )
        conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_p{persona_id:04d}_episodes_lookup
            ON {episodes}(conversation_ref, occurred_at DESC)
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {suppressions} (
                scope TEXT NOT NULL
                    CHECK(scope IN ('person', 'group')),
                subject_ref TEXT NOT NULL,
                deletion_kind TEXT NOT NULL
                    CHECK(deletion_kind IN ('fact', 'evidence')),
                semantic_hash TEXT NOT NULL,
                snapshot TEXT NOT NULL,
                source_memory_id TEXT,
                reason TEXT NOT NULL,
                deleted_at INTEGER NOT NULL,
                PRIMARY KEY(
                    scope,
                    subject_ref,
                    deletion_kind,
                    semantic_hash
                )
            )
            """
        )
        conn.execute(
            """
            INSERT INTO sys_persona_partitions(
                persona_id,
                preset_key,
                partition_id,
                people_table,
                groups_table,
                evidence_table,
                episodes_table,
                suppressions_table,
                created_at,
                updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(preset_key)
            DO UPDATE SET updated_at=excluded.updated_at
            """,
            (
                persona_id,
                preset_s,
                partition_id,
                people,
                groups,
                evidence,
                episodes,
                suppressions,
                now,
                now,
            ),
        )
        return PersonaMemoryPartition(
            preset=preset_s,
            partition_id=partition_id,
            people_table=people,
            groups_table=groups,
            evidence_table=evidence,
            episodes_table=episodes,
            suppressions_table=suppressions,
            persona_id=persona_id,
            slug=slug,
        )

    def _sync_legacy_people_partition_in_tx(
        self,
        conn: sqlite3.Connection,
        *,
        partition: PersonaMemoryPartition,
        canonical_ids: Sequence[str] | None = None,
    ) -> None:
        """Move v3/v4 global person memory into one v5 persona partition.

        This method is migration-only.  Runtime writes never mirror data back
        to the legacy global tables.
        """

        if not _table_exists(conn, "memory_facts"):
            return
        canonical_values = tuple(
            dict.fromkeys(str(value) for value in (canonical_ids or ()))
        )
        if canonical_values:
            placeholders = ",".join("?" for _ in canonical_values)
            facts = conn.execute(
                f"""
                SELECT *
                FROM memory_facts
                WHERE preset_key = ?
                  AND canonical_id IN ({placeholders})
                ORDER BY fact_id
                """,
                (partition.preset, *canonical_values),
            ).fetchall()
        else:
            facts = conn.execute(
                """
                SELECT *
                FROM memory_facts
                WHERE preset_key = ?
                ORDER BY fact_id
                """,
                (partition.preset,),
            ).fetchall()
        suppressed_rows: Sequence[sqlite3.Row] = ()
        if _table_exists(conn, "memory_suppressions"):
            if canonical_values:
                placeholders = ",".join("?" for _ in canonical_values)
                suppressed_rows = conn.execute(
                    f"""
                    SELECT *
                    FROM memory_suppressions
                    WHERE preset_key = ?
                      AND canonical_id IN ({placeholders})
                    """,
                    (partition.preset, *canonical_values),
                ).fetchall()
            else:
                suppressed_rows = conn.execute(
                    """
                    SELECT *
                    FROM memory_suppressions
                    WHERE preset_key = ?
                    """,
                    (partition.preset,),
                ).fetchall()
        for row in facts:
            conn.execute(
                f"""
                INSERT INTO {partition.people_table}(
                    memory_id,
                    person_id,
                    memory_text,
                    importance,
                    confidence,
                    updated_at,
                    canonical_fact,
                    semantic_hash,
                    source_count,
                    persona_revision,
                    first_seen_at,
                    last_confirmed_at,
                    created_at,
                    origin
                )
                VALUES(?, ?, ?, ?, 1.0, ?, ?, ?, 0, 1, ?, ?, ?, 'legacy')
                ON CONFLICT(person_id, semantic_hash)
                DO UPDATE SET
                    memory_text=excluded.memory_text,
                    importance=MAX(
                        {partition.people_table}.importance,
                        excluded.importance
                    ),
                    updated_at=MAX(
                        {partition.people_table}.updated_at,
                        excluded.updated_at
                    ),
                    last_confirmed_at=MAX(
                        {partition.people_table}.last_confirmed_at,
                        excluded.last_confirmed_at
                    )
                """,
                (
                    int(row["fact_id"]),
                    str(row["canonical_id"]),
                    str(row["content"]),
                    float(row["weight"]),
                    int(row["updated_at"]),
                    str(row["content"]),
                    str(row["fact_fingerprint"]),
                    int(row["created_at"]),
                    int(row["last_confirmed_at"]),
                    int(row["created_at"]),
                ),
            )
            migrated = conn.execute(
                f"""
                SELECT memory_id
                FROM {partition.people_table}
                WHERE person_id = ? AND semantic_hash = ?
                """,
                (
                    str(row["canonical_id"]),
                    str(row["fact_fingerprint"]),
                ),
            ).fetchone()
            if migrated is None or not _table_exists(conn, "memory_evidence"):
                continue
            evidence_rows = conn.execute(
                """
                SELECT *
                FROM memory_evidence
                WHERE fact_id = ?
                ORDER BY evidence_id
                """,
                (int(row["fact_id"]),),
            ).fetchall()
            for evidence_row in evidence_rows:
                transcript_id = evidence_row["transcript_id"]
                conn.execute(
                    f"""
                    INSERT INTO {partition.evidence_table}(
                        memory_scope,
                        memory_id,
                        conversation_ref,
                        message_key,
                        excerpt,
                        observed_at,
                        evidence_hash,
                        metadata_json
                    )
                    VALUES('person', ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(memory_scope, memory_id, evidence_hash)
                    DO NOTHING
                    """,
                    (
                        int(migrated["memory_id"]),
                        evidence_row["conversation_pk"],
                        (
                            f"legacy:{int(transcript_id)}"
                            if transcript_id is not None
                            else None
                        ),
                        str(evidence_row["content"]),
                        int(evidence_row["observed_at"]),
                        str(evidence_row["evidence_fingerprint"]),
                        str(evidence_row["metadata_json"]),
                    ),
                )
            conn.execute(
                f"""
                UPDATE {partition.people_table}
                SET source_count = (
                    SELECT COUNT(*)
                    FROM {partition.evidence_table}
                    WHERE memory_scope = 'person'
                      AND memory_id = ?
                )
                WHERE memory_id = ?
                """,
                (int(migrated["memory_id"]), int(migrated["memory_id"])),
            )
        for row in suppressed_rows:
            conn.execute(
                f"""
                INSERT INTO {partition.suppressions_table}(
                    scope,
                    subject_ref,
                    deletion_kind,
                    semantic_hash,
                    snapshot,
                    source_memory_id,
                    reason,
                    deleted_at
                )
                VALUES('person', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    scope,
                    subject_ref,
                    deletion_kind,
                    semantic_hash
                )
                DO UPDATE SET
                    snapshot=excluded.snapshot,
                    source_memory_id=excluded.source_memory_id,
                    reason=excluded.reason,
                    deleted_at=excluded.deleted_at
                """,
                (
                    str(row["canonical_id"]),
                    str(row["suppression_kind"]),
                    str(row["fingerprint"]),
                    str(row["content_snapshot"]),
                    (
                        row["source_fact_id"]
                        if "source_fact_id" in row.keys()
                        else row["source_memory_id"]
                        if "source_memory_id" in row.keys()
                        else None
                    ),
                    str(row["reason"]),
                    int(row["deleted_at"]),
                ),
            )

    def _mirror_legacy_fact_in_tx(
        self,
        conn: sqlite3.Connection,
        *,
        preset: str,
        fact_id: int,
        now: int,
    ) -> None:
        if not _table_exists(conn, "memory_facts"):
            return
        partition = self._ensure_persona_partition_in_tx(conn, preset, now)
        self._sync_legacy_people_partition_in_tx(
            conn,
            partition=partition,
        )

    def ensure_persona_partition(
        self,
        preset: Any,
        *,
        sync_legacy_people: bool = True,
    ) -> PersonaMemoryPartition:
        now = _now_ts()
        conn = self._connect()
        try:
            with self._transaction(conn):
                partition = self._ensure_persona_partition_in_tx(
                    conn, preset, now
                )
                if sync_legacy_people and _table_exists(
                    conn, "memory_facts"
                ):
                    self._sync_legacy_people_partition_in_tx(
                        conn,
                        partition=partition,
                    )
            return partition
        finally:
            conn.close()

    def list_persona_partitions(self) -> tuple[PersonaMemoryPartition, ...]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT *
                FROM sys_persona_partitions
                ORDER BY preset_key
                """
            ).fetchall()
            return tuple(
                PersonaMemoryPartition(
                    preset=str(row["preset_key"]),
                    partition_id=str(row["partition_id"]),
                    people_table=str(row["people_table"]),
                    groups_table=str(row["groups_table"]),
                    evidence_table=str(row["evidence_table"]),
                    episodes_table=str(row["episodes_table"]),
                    suppressions_table=str(row["suppressions_table"]),
                    persona_id=int(row["persona_id"]),
                    slug=str(row["partition_id"]).split("_", 1)[-1],
                )
                for row in rows
            )
        finally:
            conn.close()

    def _ensure_chat_partition_in_tx(
        self,
        conn: sqlite3.Connection,
        conversation_pk: int,
        now: int,
    ) -> ChatPartition:
        conversation = conn.execute(
            """
            SELECT *
            FROM sys_conversations
            WHERE conversation_pk = ?
            """,
            (int(conversation_pk),),
        ).fetchone()
        if conversation is None:
            raise MemoryStoreError("conversation does not exist")
        protocol = str(conversation["protocol"])
        self_id = str(conversation["self_id"])
        kind = str(conversation["conversation_kind"])
        conversation_id = str(conversation["conversation_id"])
        table_name = _chat_table_name(
            int(conversation_pk),
            protocol=protocol,
            conversation_kind=kind,
            conversation_id=conversation_id,
        )
        if re.fullmatch(r"chat_[a-z0-9_]+", table_name) is None:
            raise MemoryStoreError("invalid chat table name")
        existing = conn.execute(
            """
            SELECT *
            FROM sys_chat_partitions
            WHERE conversation_ref = ?
            """,
            (int(conversation_pk),),
        ).fetchone()
        if existing is not None and str(existing["table_name"]) != table_name:
            raise MemoryStoreError("chat partition registry is inconsistent")

        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                message_uid INTEGER NOT NULL UNIQUE,
                message_key TEXT NOT NULL UNIQUE,
                direction TEXT NOT NULL
                    CHECK(direction IN ('incoming', 'outgoing')),
                sender_person_id TEXT,
                sender_name TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL,
                occurred_at INTEGER NOT NULL,
                reply_to TEXT,
                message_type TEXT NOT NULL,
                active_persona_id INTEGER,
                segments_json TEXT NOT NULL DEFAULT '[]',
                content_hash TEXT NOT NULL,
                captured_at INTEGER NOT NULL,
                external_message_id TEXT NOT NULL DEFAULT '',
                sender_protocol TEXT NOT NULL DEFAULT '',
                sender_self_id TEXT NOT NULL DEFAULT '',
                sender_external_id TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (sender_person_id)
                    REFERENCES sys_identities(canonical_id),
                FOREIGN KEY (active_persona_id)
                    REFERENCES sys_personas(persona_id)
            )
            """
        )
        conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_chat_{conversation_pk}_time
            ON {table_name}(occurred_at DESC)
            """
        )
        conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_chat_{conversation_pk}_sender
            ON {table_name}(sender_person_id, occurred_at DESC)
            """
        )
        conn.execute(
            """
            INSERT INTO sys_chat_partitions(
                conversation_ref,
                protocol,
                self_id,
                conversation_kind,
                conversation_id,
                table_name,
                retention_days,
                last_pruned_at,
                created_at,
                updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(conversation_ref)
            DO UPDATE SET updated_at=excluded.updated_at
            """,
            (
                int(conversation_pk),
                protocol,
                self_id,
                kind,
                conversation_id,
                table_name,
                CHAT_TRANSCRIPT_RETENTION_DAYS,
                now,
                now,
            ),
        )
        return ChatPartition(
            conversation_pk=int(conversation_pk),
            protocol=protocol,
            self_id=self_id,
            conversation_kind=kind,
            conversation_id=conversation_id,
            table_name=table_name,
            retention_days=CHAT_TRANSCRIPT_RETENTION_DAYS,
        )

    def ensure_chat_partition(
        self,
        *,
        protocol: Any,
        self_id: Any,
        conversation_kind: Any,
        conversation_id: Any,
    ) -> ChatPartition:
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
                return self._ensure_chat_partition_in_tx(
                    conn,
                    conversation_pk,
                    now,
                )
        finally:
            conn.close()

    def list_chat_partitions(self) -> tuple[ChatPartition, ...]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT *
                FROM sys_chat_partitions
                ORDER BY conversation_ref
                """
            ).fetchall()
            return tuple(
                ChatPartition(
                    conversation_pk=int(row["conversation_ref"]),
                    protocol=str(row["protocol"]),
                    self_id=str(row["self_id"]),
                    conversation_kind=str(row["conversation_kind"]),
                    conversation_id=str(row["conversation_id"]),
                    table_name=str(row["table_name"]),
                    retention_days=int(row["retention_days"]),
                )
                for row in rows
            )
        finally:
            conn.close()

    def _migrate_v4_persona_partition_in_tx(
        self,
        conn: sqlite3.Connection,
        *,
        partition: PersonaMemoryPartition,
        now: int,
    ) -> None:
        legacy_id = persona_partition_id(partition.preset)
        legacy_prefix = f"persona_{legacy_id}"
        legacy_people = f"{legacy_prefix}_people"
        legacy_groups = f"{legacy_prefix}_groups"
        legacy_episodes = f"{legacy_prefix}_episodes"
        legacy_deleted = f"{legacy_prefix}_suppressions"

        if _table_exists(conn, legacy_people):
            for row in conn.execute(
                f"SELECT * FROM {legacy_people} ORDER BY memory_id"
            ).fetchall():
                conn.execute(
                    f"""
                    INSERT INTO {partition.people_table}(
                        memory_id,
                        person_id,
                        memory_text,
                        importance,
                        confidence,
                        updated_at,
                        canonical_fact,
                        semantic_hash,
                        source_count,
                        persona_revision,
                        first_seen_at,
                        last_confirmed_at,
                        created_at,
                        origin
                    )
                    VALUES(?, ?, ?, ?, 1.0, ?, ?, ?, 0, 1, ?, ?, ?, ?)
                    ON CONFLICT(person_id, semantic_hash)
                    DO UPDATE SET
                        memory_text=excluded.memory_text,
                        importance=MAX(
                            {partition.people_table}.importance,
                            excluded.importance
                        ),
                        updated_at=MAX(
                            {partition.people_table}.updated_at,
                            excluded.updated_at
                        )
                    """,
                    (
                        int(row["memory_id"]),
                        str(row["canonical_id"]),
                        str(row["content"]),
                        float(row["weight"]),
                        int(row["updated_at"]),
                        str(row["content"]),
                        str(row["fingerprint"]),
                        int(row["created_at"]),
                        int(row["last_confirmed_at"]),
                        int(row["created_at"]),
                        str(row["origin"]),
                    ),
                )

        if _table_exists(conn, legacy_groups):
            for row in conn.execute(
                f"SELECT * FROM {legacy_groups} ORDER BY memory_id"
            ).fetchall():
                conn.execute(
                    f"""
                    INSERT INTO {partition.groups_table}(
                        memory_id,
                        group_ref,
                        memory_text,
                        importance,
                        confidence,
                        updated_at,
                        canonical_fact,
                        semantic_hash,
                        source_count,
                        persona_revision,
                        first_seen_at,
                        last_confirmed_at,
                        created_at,
                        last_writer_person_id
                    )
                    VALUES(?, ?, ?, ?, 1.0, ?, ?, ?, 0, 1, ?, ?, ?, ?)
                    ON CONFLICT(group_ref, semantic_hash)
                    DO UPDATE SET
                        memory_text=excluded.memory_text,
                        importance=MAX(
                            {partition.groups_table}.importance,
                            excluded.importance
                        ),
                        updated_at=MAX(
                            {partition.groups_table}.updated_at,
                            excluded.updated_at
                        )
                    """,
                    (
                        int(row["memory_id"]),
                        str(row["group_key"]),
                        str(row["content"]),
                        float(row["weight"]),
                        int(row["updated_at"]),
                        str(row["content"]),
                        str(row["fingerprint"]),
                        int(row["created_at"]),
                        int(row["last_confirmed_at"]),
                        int(row["created_at"]),
                        row["last_writer_canonical_id"],
                    ),
                )

        if _table_exists(conn, legacy_episodes):
            for row in conn.execute(
                f"SELECT * FROM {legacy_episodes} ORDER BY episode_id"
            ).fetchall():
                conn.execute(
                    f"""
                    INSERT INTO {partition.episodes_table}(
                        episode_id,
                        conversation_ref,
                        exchange_key,
                        person_id,
                        user_text,
                        assistant_text,
                        occurred_at,
                        send_state,
                        review_state,
                        created_at,
                        updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, 'sent', 'completed', ?, ?)
                    ON CONFLICT(conversation_ref, exchange_key)
                    DO NOTHING
                    """,
                    (
                        int(row["episode_id"]),
                        int(row["conversation_pk"]),
                        str(row["exchange_key"]),
                        str(row["speaker_canonical_id"]),
                        str(row["user_content"]),
                        str(row["assistant_content"]),
                        int(row["occurred_at"]),
                        int(row["created_at"]),
                        int(row["updated_at"]),
                    ),
                )

        if _table_exists(conn, legacy_deleted):
            for row in conn.execute(
                f"SELECT * FROM {legacy_deleted}"
            ).fetchall():
                conn.execute(
                    f"""
                    INSERT INTO {partition.suppressions_table}(
                        scope,
                        subject_ref,
                        deletion_kind,
                        semantic_hash,
                        snapshot,
                        source_memory_id,
                        reason,
                        deleted_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(
                        scope,
                        subject_ref,
                        deletion_kind,
                        semantic_hash
                    )
                    DO UPDATE SET
                        snapshot=excluded.snapshot,
                        source_memory_id=excluded.source_memory_id,
                        reason=excluded.reason,
                        deleted_at=MAX(
                            {partition.suppressions_table}.deleted_at,
                            excluded.deleted_at
                        )
                    """,
                    (
                        str(row["scope"]),
                        str(row["subject_id"]),
                        str(row["suppression_kind"]),
                        str(row["fingerprint"]),
                        str(row["content_snapshot"]),
                        row["source_memory_id"],
                        str(row["reason"]),
                        int(row["deleted_at"]),
                    ),
                )

    def _migrate_legacy_transcripts_in_tx(
        self,
        conn: sqlite3.Connection,
        *,
        now: int,
    ) -> None:
        if not _table_exists(conn, "raw_transcript_messages"):
            return
        columns = {
            str(row["name"])
            for row in conn.execute(
                "PRAGMA table_info(raw_transcript_messages)"
            )
        }
        cutoff = now - (CHAT_TRANSCRIPT_RETENTION_DAYS * 86400)
        rows = conn.execute(
            """
            SELECT *
            FROM raw_transcript_messages
            WHERE occurred_at >= ?
            ORDER BY transcript_id
            """,
            (cutoff,),
        ).fetchall()
        for row in rows:
            conversation_pk = int(row["conversation_pk"])
            chat = self._ensure_chat_partition_in_tx(
                conn,
                conversation_pk,
                now,
            )
            preset = (
                normalize_preset(row["preset_key"])
                if "preset_key" in columns
                else "default"
            )
            persona = self._ensure_persona_partition_in_tx(
                conn,
                preset,
                now,
            )
            message_uid = int(row["transcript_id"])
            message_key = str(row["message_key"])
            content = str(row["content"])
            occurred_at = int(row["occurred_at"])
            message_type = str(row["message_type"])
            sender_protocol = str(row["sender_protocol"])
            sender_self_id = str(row["sender_self_id"])
            sender_external_id = str(row["sender_external_id"])
            payload_seed = "\x00".join(
                (
                    sender_protocol,
                    sender_self_id,
                    sender_external_id,
                    str(occurred_at),
                    message_type,
                    content,
                )
            )
            content_hash = hashlib.sha256(
                payload_seed.encode("utf-8")
            ).hexdigest()
            conn.execute(
                """
                INSERT INTO sys_chat_message_index(
                    message_uid,
                    conversation_ref,
                    table_name,
                    message_key,
                    direction,
                    sender_person_id,
                    active_persona_id,
                    occurred_at,
                    message_type
                )
                VALUES(?, ?, ?, ?, 'incoming', ?, ?, ?, ?)
                ON CONFLICT(conversation_ref, message_key) DO NOTHING
                """,
                (
                    message_uid,
                    conversation_pk,
                    chat.table_name,
                    message_key,
                    row["sender_canonical_id"],
                    persona.persona_id,
                    occurred_at,
                    message_type,
                ),
            )
            index_row = conn.execute(
                """
                SELECT message_uid
                FROM sys_chat_message_index
                WHERE conversation_ref = ? AND message_key = ?
                """,
                (conversation_pk, message_key),
            ).fetchone()
            if index_row is None:
                raise MemoryStoreError("failed to migrate chat message index")
            stable_uid = int(index_row["message_uid"])
            conn.execute(
                f"""
                INSERT INTO {chat.table_name}(
                    message_uid,
                    message_key,
                    direction,
                    sender_person_id,
                    sender_name,
                    text,
                    occurred_at,
                    reply_to,
                    message_type,
                    active_persona_id,
                    segments_json,
                    content_hash,
                    captured_at,
                    external_message_id,
                    sender_protocol,
                    sender_self_id,
                    sender_external_id
                )
                VALUES(?, ?, 'incoming', ?, '', ?, ?, NULL, ?, ?, '[]', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_key) DO NOTHING
                """,
                (
                    stable_uid,
                    message_key,
                    row["sender_canonical_id"],
                    content,
                    occurred_at,
                    message_type,
                    persona.persona_id,
                    content_hash,
                    int(row["created_at"]),
                    str(row["external_message_id"]),
                    sender_protocol,
                    sender_self_id,
                    sender_external_id,
                ),
            )
            local = conn.execute(
                f"SELECT seq FROM {chat.table_name} WHERE message_key = ?",
                (message_key,),
            ).fetchone()
            if local is not None:
                conn.execute(
                    """
                    UPDATE sys_chat_message_index
                    SET local_seq = ?
                    WHERE message_uid = ?
                    """,
                    (int(local["seq"]), stable_uid),
                )
            for persona_row in conn.execute(
                "SELECT evidence_table FROM sys_persona_partitions"
            ).fetchall():
                evidence_table = str(persona_row["evidence_table"])
                conn.execute(
                    f"""
                    UPDATE {evidence_table}
                    SET message_key = ?
                    WHERE message_key = ?
                    """,
                    (message_key, f"legacy:{message_uid}"),
                )

    @staticmethod
    def _copy_legacy_table_in_tx(
        conn: sqlite3.Connection,
        source: str,
        target: str,
        columns: Sequence[str],
    ) -> None:
        if not _table_exists(conn, source):
            return
        source_columns = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({source})")
        }
        target_columns = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({target})")
        }
        shared = tuple(
            column
            for column in columns
            if column in source_columns and column in target_columns
        )
        if not shared:
            return
        column_sql = ", ".join(shared)
        conn.execute(
            f"INSERT OR IGNORE INTO {target}({column_sql}) "
            f"SELECT {column_sql} FROM {source}"
        )

    def _migrate_legacy_control_tables_in_tx(
        self,
        conn: sqlite3.Connection,
    ) -> None:
        """Copy v4 control rows into their prefixed v5 counterparts."""

        mappings = (
            (
                "canonical_identities",
                "sys_identities",
                ("canonical_id", "created_at", "updated_at", "merged_into"),
            ),
            (
                "identity_aliases",
                "sys_identity_aliases",
                (
                    "alias_id",
                    "protocol",
                    "self_id",
                    "external_id",
                    "canonical_id",
                    "created_at",
                    "updated_at",
                ),
            ),
            (
                "identity_authorizations",
                "sys_identity_authorizations",
                (
                    "authorization_id",
                    "source_protocol",
                    "source_self_id",
                    "source_external_id",
                    "target_canonical_id",
                    "reason",
                    "authorized_at",
                    "revoked_at",
                ),
            ),
            (
                "identity_merge_ledger",
                "audit_identity_merges",
                (
                    "merge_key",
                    "source_canonical_id",
                    "target_canonical_id",
                    "source_protocol",
                    "source_self_id",
                    "source_external_id",
                    "target_protocol",
                    "target_self_id",
                    "target_external_id",
                    "reason",
                    "outcome",
                    "applied_at",
                ),
            ),
            (
                "conversations",
                "sys_conversations",
                (
                    "conversation_pk",
                    "protocol",
                    "self_id",
                    "conversation_kind",
                    "conversation_id",
                    "created_at",
                    "last_seen_at",
                ),
            ),
            (
                "conversation_preferences",
                "cfg_conversation_settings",
                ("conversation_pk", "active_preset", "updated_at"),
            ),
            (
                "session_settings",
                "cfg_session_settings",
                (
                    "session_id",
                    "conversation_pk",
                    "preset_key",
                    "model",
                    "persona",
                    "tts_enabled",
                    "ai_enabled",
                    "agent_enabled",
                    "memory_enabled",
                    "memory_interval_seconds",
                    "last_generated_at",
                    "updated_at",
                ),
            ),
            (
                "memory_preferences",
                "cfg_memory_settings",
                (
                    "canonical_id",
                    "preset_key",
                    "enabled",
                    "interval_seconds",
                    "last_generated_at",
                    "next_retry_at",
                    "failure_count",
                    "updated_at",
                ),
            ),
            (
                "generation_barriers",
                "job_generation_barriers",
                (
                    "canonical_id",
                    "preset_key",
                    "version",
                    "active_generation_id",
                    "started_at",
                    "invalidated_at",
                    "completed_at",
                ),
            ),
            (
                "generation_cursors",
                "job_generation_cursors",
                (
                    "session_id",
                    "canonical_id",
                    "last_transcript_id",
                    "last_generated_at",
                    "updated_at",
                ),
            ),
            (
                "identity_generation_cursors",
                "job_identity_generation_cursors",
                (
                    "canonical_id",
                    "preset_key",
                    "last_transcript_id",
                    "last_generated_at",
                    "updated_at",
                ),
            ),
            (
                "migration_ledger",
                "audit_legacy_migrations",
                (
                    "migration_id",
                    "source_path",
                    "source_sha256",
                    "status",
                    "dry_run",
                    "started_at",
                    "completed_at",
                    "backup_path",
                    "counts_json",
                    "verification_json",
                    "error",
                ),
            ),
            (
                "migration_quarantine",
                "audit_migration_quarantine",
                (
                    "quarantine_id",
                    "migration_id",
                    "item_type",
                    "source_table",
                    "source_key",
                    "payload_json",
                    "reason",
                    "created_at",
                ),
            ),
        )
        for source, target, columns in mappings:
            self._copy_legacy_table_in_tx(
                conn,
                source,
                target,
                columns,
            )

    def _drop_legacy_content_tables_in_tx(
        self,
        conn: sqlite3.Connection,
    ) -> None:
        legacy_dynamic: list[str] = []
        if _table_exists(conn, "persona_memory_partitions"):
            for row in conn.execute(
                "SELECT * FROM persona_memory_partitions"
            ).fetchall():
                legacy_dynamic.extend(
                    (
                        str(row["episodes_table"]),
                        str(row["groups_table"]),
                        str(row["people_table"]),
                        str(row["suppressions_table"]),
                    )
                )
        for table in dict.fromkeys(legacy_dynamic):
            if re.fullmatch(r"persona_[0-9a-f]{64}_[a-z]+", table):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("DROP TABLE IF EXISTS memory_evidence")
        conn.execute("DROP TABLE IF EXISTS memory_facts")
        conn.execute("DROP TABLE IF EXISTS memory_suppressions")
        conn.execute("DROP TABLE IF EXISTS raw_transcript_messages")
        conn.execute("DROP TABLE IF EXISTS persona_memory_partitions")
        conn.execute("DROP TABLE IF EXISTS schema_meta")
        for table in (
            "migration_quarantine",
            "generation_cursors",
            "identity_generation_cursors",
            "session_settings",
            "conversation_preferences",
            "memory_preferences",
            "generation_barriers",
            "identity_authorizations",
            "identity_aliases",
            "identity_merge_ledger",
            "conversations",
            "migration_ledger",
            "canonical_identities",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")

    @staticmethod
    def _legacy_migration_counts(
        conn: sqlite3.Connection,
        *,
        now: int,
    ) -> dict[str, int]:
        counts = {
            "people_unique": 0,
            "evidence_unique": 0,
            "deleted_unique": 0,
            "recent_chat_unique": 0,
        }
        if _table_exists(conn, "memory_facts"):
            counts["people_unique"] = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM (
                        SELECT 1
                        FROM memory_facts
                        GROUP BY preset_key, canonical_id, fact_fingerprint
                    )
                    """
                ).fetchone()[0]
            )
        if _table_exists(conn, "memory_evidence"):
            counts["evidence_unique"] = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM (
                        SELECT 1
                        FROM memory_evidence
                        GROUP BY fact_id, evidence_fingerprint
                    )
                    """
                ).fetchone()[0]
            )
        if _table_exists(conn, "memory_suppressions"):
            counts["deleted_unique"] = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM (
                        SELECT 1
                        FROM memory_suppressions
                        GROUP BY canonical_id, preset_key,
                                 suppression_kind, fingerprint
                    )
                    """
                ).fetchone()[0]
            )
        if _table_exists(conn, "raw_transcript_messages"):
            cutoff = int(now) - (
                CHAT_TRANSCRIPT_RETENTION_DAYS * 86400
            )
            counts["recent_chat_unique"] = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM (
                        SELECT 1
                        FROM raw_transcript_messages
                        WHERE occurred_at >= ?
                        GROUP BY conversation_pk, message_key
                    )
                    """,
                    (cutoff,),
                ).fetchone()[0]
            )
        return counts

    def _verify_v5_partitions(
        self,
        *,
        source_counts: Mapping[str, int],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        conn = self._connect()
        try:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            views = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='view'"
                )
            }
            forbidden = {
                "memory_facts",
                "memory_evidence",
                "memory_suppressions",
                "raw_transcript_messages",
                "persona_memory_partitions",
            }
            issues: list[str] = []
            if forbidden & tables:
                issues.append(
                    "legacy tables remain: "
                    + ",".join(sorted(forbidden & tables))
                )
            if forbidden & views:
                issues.append(
                    "legacy compatibility views remain: "
                    + ",".join(sorted(forbidden & views))
                )

            target_counts: dict[str, Any] = {
                "people": 0,
                "groups": 0,
                "evidence": 0,
                "episodes": 0,
                "deleted": 0,
                "chat": 0,
                "persona_tables": {},
                "chat_tables": {},
            }
            persona_rows = conn.execute(
                "SELECT * FROM sys_persona_partitions ORDER BY persona_id"
            ).fetchall()
            for persona in persona_rows:
                names = {
                    "people": str(persona["people_table"]),
                    "groups": str(persona["groups_table"]),
                    "evidence": str(persona["evidence_table"]),
                    "episodes": str(persona["episodes_table"]),
                    "deleted": str(persona["suppressions_table"]),
                }
                if any(
                    re.fullmatch(r"mem_p[0-9]{4,}_[a-z0-9_]+", name)
                    is None
                    for name in names.values()
                ):
                    issues.append(
                        f"unsafe persona table registry for {persona['persona_id']}"
                    )
                    continue
                per_persona: dict[str, int] = {}
                for kind, table_name in names.items():
                    if table_name not in tables:
                        issues.append(f"missing persona table {table_name}")
                        continue
                    count = int(
                        conn.execute(
                            f"SELECT COUNT(*) FROM {table_name}"
                        ).fetchone()[0]
                    )
                    per_persona[kind] = count
                    target_counts[kind] += count
                target_counts["persona_tables"][
                    str(persona["preset_key"])
                ] = per_persona
                for table_name, subject_column in (
                    (names["people"], "person_id"),
                    (names["groups"], "group_ref"),
                ):
                    duplicate_count = int(
                        conn.execute(
                            f"""
                            SELECT COUNT(*) FROM (
                                SELECT 1 FROM {table_name}
                                GROUP BY {subject_column}, semantic_hash
                                HAVING COUNT(*) > 1
                            )
                            """
                        ).fetchone()[0]
                    )
                    empty_hashes = int(
                        conn.execute(
                            f"""
                            SELECT COUNT(*) FROM {table_name}
                            WHERE semantic_hash = '' OR semantic_hash IS NULL
                            """
                        ).fetchone()[0]
                    )
                    if duplicate_count or empty_hashes:
                        issues.append(
                            f"invalid memory hashes in {table_name}: "
                            f"duplicates={duplicate_count}, empty={empty_hashes}"
                        )
                orphan_evidence = int(
                    conn.execute(
                        f"""
                        SELECT COUNT(*)
                        FROM {names['evidence']} e
                        LEFT JOIN {names['people']} p
                          ON e.memory_scope='person'
                         AND p.memory_id=e.memory_id
                        LEFT JOIN {names['groups']} g
                          ON e.memory_scope='group'
                         AND g.memory_id=e.memory_id
                        WHERE (e.memory_scope='person' AND p.memory_id IS NULL)
                           OR (e.memory_scope='group' AND g.memory_id IS NULL)
                        """
                    ).fetchone()[0]
                )
                if orphan_evidence:
                    issues.append(
                        f"orphan evidence in {names['evidence']}: "
                        f"{orphan_evidence}"
                    )

            chat_rows = conn.execute(
                "SELECT * FROM sys_chat_partitions ORDER BY conversation_ref"
            ).fetchall()
            for chat in chat_rows:
                table_name = str(chat["table_name"])
                if (
                    re.fullmatch(r"chat_[gu][0-9]{6,}_[a-z0-9_]+", table_name)
                    is None
                    or table_name not in tables
                ):
                    issues.append(f"invalid chat partition {table_name}")
                    continue
                rows = conn.execute(
                    f"SELECT * FROM {table_name} ORDER BY seq"
                ).fetchall()
                target_counts["chat_tables"][table_name] = len(rows)
                target_counts["chat"] += len(rows)
                distinct_keys = len({str(row["message_key"]) for row in rows})
                if distinct_keys != len(rows):
                    issues.append(f"duplicate message keys in {table_name}")
                for row in rows:
                    payload_seed = "\x00".join(
                        (
                            str(row["sender_protocol"]),
                            str(row["sender_self_id"]),
                            str(row["sender_external_id"]),
                            str(int(row["occurred_at"])),
                            str(row["message_type"]),
                            str(row["text"]),
                        )
                    )
                    expected_hash = hashlib.sha256(
                        payload_seed.encode("utf-8")
                    ).hexdigest()
                    if str(row["content_hash"]) != expected_hash:
                        issues.append(
                            f"content hash mismatch in {table_name} "
                            f"message_uid={row['message_uid']}"
                        )
                        break
                index_count = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM sys_chat_message_index
                        WHERE conversation_ref = ? AND table_name = ?
                        """,
                        (int(chat["conversation_ref"]), table_name),
                    ).fetchone()[0]
                )
                if index_count != len(rows):
                    issues.append(
                        f"chat index count mismatch for {table_name}: "
                        f"index={index_count}, rows={len(rows)}"
                    )

            comparisons = {
                "recent_chat": {
                    "source": int(source_counts["recent_chat_unique"]),
                    "target": int(target_counts["chat"]),
                    "equal": int(source_counts["recent_chat_unique"])
                    == int(target_counts["chat"]),
                },
                "people": {
                    "source_minimum": int(source_counts["people_unique"]),
                    "target": int(target_counts["people"]),
                    "satisfied": int(target_counts["people"])
                    >= int(source_counts["people_unique"]),
                },
                "evidence": {
                    "source_minimum": int(source_counts["evidence_unique"]),
                    "target": int(target_counts["evidence"]),
                    "satisfied": int(target_counts["evidence"])
                    >= int(source_counts["evidence_unique"]),
                },
                "deleted": {
                    "source_minimum": int(source_counts["deleted_unique"]),
                    "target": int(target_counts["deleted"]),
                    "satisfied": int(target_counts["deleted"])
                    >= int(source_counts["deleted_unique"]),
                },
            }
            for name, comparison in comparisons.items():
                passed = comparison.get("equal", comparison.get("satisfied"))
                if not passed:
                    issues.append(
                        f"source/target migration count mismatch for {name}"
                    )
            verification = {
                "quick_check": "ok",
                "foreign_key_errors": 0,
                "legacy_tables": [],
                "legacy_views": [],
                "comparisons": comparisons,
                "issues": issues,
            }
            if issues:
                raise MemoryStoreError(
                    "v5 partition verification failed: "
                    + "; ".join(issues[:10])
                )
            return (
                {"source": dict(source_counts), "target": target_counts},
                verification,
            )
        finally:
            conn.close()

    def migrate_to_v5(self) -> Path | None:
        """Explicitly and atomically replace a legacy DB with verified v5.

        Normal initialization never invokes this operation.  The original is
        backed up first; all DDL and data movement runs against a staging copy.
        """

        source_path = self.db_path.resolve()
        if not source_path.exists():
            self.initialize()
            return None
        migration_started_at = _now_ts()
        source_counts: dict[str, int]
        source = self._connect()
        try:
            legacy = any(
                _table_exists(source, table)
                for table in (
                    "memory_facts",
                    "memory_evidence",
                    "memory_suppressions",
                    "raw_transcript_messages",
                    "persona_memory_partitions",
                    "schema_meta",
                    "canonical_identities",
                    "identity_aliases",
                    "conversations",
                    "session_settings",
                )
            )
            if not legacy:
                self.initialize()
                return None
            quick = tuple(
                str(row[0]) for row in source.execute("PRAGMA quick_check")
            )
            if quick != ("ok",):
                raise MemoryStoreError(
                    "legacy database failed quick_check; migration aborted"
                )
            foreign_key_errors = tuple(
                tuple(row)
                for row in source.execute("PRAGMA foreign_key_check")
            )
            if foreign_key_errors:
                raise MemoryStoreError(
                    "legacy database failed foreign_key_check; "
                    "migration aborted"
                )
            source_counts = self._legacy_migration_counts(
                source,
                now=migration_started_at,
            )
            source.execute("PRAGMA wal_checkpoint(FULL)")
            stamp = f"{_now_ts()}-{uuid.uuid4().hex[:8]}"
            backup_path = source_path.with_name(
                f"{source_path.name}.v4-backup-{stamp}"
            )
            staging_path = source_path.with_name(
                f"{source_path.name}.v5-staging-{stamp}"
            )
            backup_conn = sqlite3.connect(str(backup_path))
            staging_conn = sqlite3.connect(str(staging_path))
            try:
                source.backup(backup_conn)
                source.backup(staging_conn)
            finally:
                backup_conn.close()
                staging_conn.close()
        finally:
            source.close()

        staged = JianerMemoryStore(staging_path, initialize=False)
        staged._explicit_v5_migration = True
        staged._migration_backup_path = backup_path
        staged._migration_started_at = migration_started_at
        migration_succeeded = False
        try:
            staged.initialize()
            if staged.quick_check() != ("ok",):
                raise MemoryStoreError("staged v5 database failed quick_check")
            staged_foreign_errors = staged.foreign_key_check()
            if staged_foreign_errors:
                raise MemoryStoreError(
                    "staged v5 database failed foreign_key_check: "
                    + json.dumps(
                        staged_foreign_errors[:5],
                        ensure_ascii=False,
                        default=str,
                    )
                )
            counts, verification = staged._verify_v5_partitions(
                source_counts=source_counts,
            )
            staged_audit = staged._connect()
            try:
                with staged._transaction(staged_audit):
                    staged_audit.execute(
                        """
                        UPDATE audit_migrations
                        SET counts_json = ?,
                            verification_json = ?,
                            status = 'completed',
                            error = NULL,
                            completed_at = ?
                        WHERE migration_key = 'schema-v5'
                        """,
                        (
                            json.dumps(
                                counts,
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            json.dumps(
                                verification,
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            _now_ts(),
                        ),
                    )
            finally:
                staged_audit.close()
            staged_source = sqlite3.connect(str(staging_path))
            destination = sqlite3.connect(str(source_path))
            try:
                staged_source.backup(destination)
                destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                destination.close()
                staged_source.close()
            migration_succeeded = True
        except Exception as exc:
            # Never mutate the original into a half-v4/half-v5 database.  Keep
            # the staging copy as the failure artifact and record the reason
            # there alongside the already completed SQLite backup.
            try:
                failed = sqlite3.connect(str(staging_path))
                try:
                    failed.execute(
                        """
                        CREATE TABLE IF NOT EXISTS audit_migrations (
                            migration_key TEXT PRIMARY KEY,
                            from_version INTEGER NOT NULL,
                            to_version INTEGER NOT NULL,
                            backup_path TEXT,
                            status TEXT NOT NULL,
                            counts_json TEXT NOT NULL DEFAULT '{}',
                            verification_json TEXT NOT NULL DEFAULT '{}',
                            error TEXT,
                            started_at INTEGER NOT NULL,
                            completed_at INTEGER
                        )
                        """
                    )
                    failed.execute(
                        """
                        INSERT INTO audit_migrations(
                            migration_key,
                            from_version,
                            to_version,
                            backup_path,
                            status,
                            verification_json,
                            error,
                            started_at,
                            completed_at
                        )
                        VALUES(
                            'schema-v5', 4, 5, ?, 'failed', ?, ?, ?, ?
                        )
                        ON CONFLICT(migration_key)
                        DO UPDATE SET
                            backup_path=excluded.backup_path,
                            status='failed',
                            verification_json=excluded.verification_json,
                            error=excluded.error,
                            completed_at=excluded.completed_at
                        """,
                        (
                            str(backup_path),
                            json.dumps(
                                {"staging_path": str(staging_path)},
                                ensure_ascii=False,
                            ),
                            f"{type(exc).__name__}: {exc}"[:1000],
                            _now_ts(),
                            _now_ts(),
                        ),
                    )
                    failed.commit()
                finally:
                    failed.close()
            except Exception:
                pass
            raise
        finally:
            if migration_succeeded and staging_path.exists():
                staging_path.unlink()
        return backup_path

    def initialize(self) -> None:
        conn = self._connect()
        try:
            legacy_content = any(
                _table_exists(conn, table)
                for table in (
                    "memory_facts",
                    "memory_evidence",
                    "memory_suppressions",
                    "raw_transcript_messages",
                    "persona_memory_partitions",
                    "schema_meta",
                    "canonical_identities",
                    "identity_aliases",
                    "conversations",
                    "session_settings",
                )
            )
            migration_mode = bool(
                getattr(self, "_explicit_v5_migration", False)
            )
            if legacy_content and not migration_mode:
                raise MemoryMigrationRequiredError(
                    "legacy JianerAI content tables require explicit "
                    "migrate_to_v5() before normal startup"
                )
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sys_schema (
                    schema_name TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sys_personas (
                    persona_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    preset_key TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sys_persona_partitions (
                    persona_id INTEGER PRIMARY KEY,
                    preset_key TEXT NOT NULL UNIQUE,
                    partition_id TEXT NOT NULL UNIQUE,
                    people_table TEXT NOT NULL UNIQUE,
                    groups_table TEXT NOT NULL UNIQUE,
                    evidence_table TEXT NOT NULL UNIQUE,
                    episodes_table TEXT NOT NULL UNIQUE,
                    suppressions_table TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY (persona_id)
                        REFERENCES sys_personas(persona_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS sys_identities (
                    canonical_id TEXT PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    merged_into TEXT,
                    FOREIGN KEY (merged_into)
                        REFERENCES sys_identities(canonical_id)
                );

                CREATE TABLE IF NOT EXISTS sys_identity_aliases (
                    alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    protocol TEXT NOT NULL,
                    self_id TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    canonical_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(protocol, self_id, external_id),
                    FOREIGN KEY (canonical_id)
                        REFERENCES sys_identities(canonical_id)
                );
                CREATE INDEX IF NOT EXISTS idx_identity_aliases_canonical
                    ON sys_identity_aliases(canonical_id);

                CREATE TABLE IF NOT EXISTS sys_identity_authorizations (
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
                        REFERENCES sys_identities(canonical_id)
                );

                CREATE TABLE IF NOT EXISTS audit_identity_merges (
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

                CREATE TABLE IF NOT EXISTS sys_conversations (
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

                CREATE TABLE IF NOT EXISTS cfg_conversation_settings (
                    conversation_pk INTEGER PRIMARY KEY,
                    active_preset TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY (conversation_pk)
                        REFERENCES sys_conversations(conversation_pk)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS sys_chat_partitions (
                    conversation_ref INTEGER PRIMARY KEY,
                    protocol TEXT NOT NULL,
                    self_id TEXT NOT NULL,
                    conversation_kind TEXT NOT NULL
                        CHECK(conversation_kind IN ('group', 'private')),
                    conversation_id TEXT NOT NULL,
                    table_name TEXT NOT NULL UNIQUE,
                    retention_days INTEGER NOT NULL DEFAULT 90,
                    last_pruned_at INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY (conversation_ref)
                        REFERENCES sys_conversations(conversation_pk)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS sys_chat_message_index (
                    message_uid INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_ref INTEGER NOT NULL,
                    table_name TEXT NOT NULL,
                    local_seq INTEGER,
                    message_key TEXT NOT NULL,
                    direction TEXT NOT NULL
                        CHECK(direction IN ('incoming', 'outgoing')),
                    sender_person_id TEXT,
                    active_persona_id INTEGER,
                    occurred_at INTEGER NOT NULL,
                    message_type TEXT NOT NULL,
                    UNIQUE(conversation_ref, message_key),
                    FOREIGN KEY (conversation_ref)
                        REFERENCES sys_conversations(conversation_pk)
                        ON DELETE CASCADE,
                    FOREIGN KEY (sender_person_id)
                        REFERENCES sys_identities(canonical_id),
                    FOREIGN KEY (active_persona_id)
                        REFERENCES sys_personas(persona_id)
                );
                CREATE INDEX IF NOT EXISTS idx_chat_index_conversation_time
                    ON sys_chat_message_index(
                        conversation_ref,
                        occurred_at DESC
                    );
                CREATE INDEX IF NOT EXISTS idx_chat_index_sender
                    ON sys_chat_message_index(
                        sender_person_id,
                        active_persona_id,
                        message_uid
                    );

                CREATE TABLE IF NOT EXISTS cfg_session_settings (
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
                        REFERENCES sys_conversations(conversation_pk)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS cfg_memory_settings (
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
                        REFERENCES sys_identities(canonical_id)
                );

                CREATE TABLE IF NOT EXISTS job_generation_barriers (
                    canonical_id TEXT NOT NULL,
                    preset_key TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    active_generation_id TEXT,
                    started_at INTEGER NOT NULL,
                    invalidated_at INTEGER,
                    completed_at INTEGER,
                    PRIMARY KEY(canonical_id, preset_key),
                    FOREIGN KEY (canonical_id)
                        REFERENCES sys_identities(canonical_id)
                );

                CREATE TABLE IF NOT EXISTS job_generation_cursors (
                    session_id INTEGER NOT NULL,
                    canonical_id TEXT NOT NULL,
                    last_transcript_id INTEGER NOT NULL DEFAULT 0,
                    last_generated_at INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(session_id, canonical_id),
                    FOREIGN KEY (session_id)
                        REFERENCES cfg_session_settings(session_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (canonical_id)
                        REFERENCES sys_identities(canonical_id)
                );

                CREATE TABLE IF NOT EXISTS job_identity_generation_cursors (
                    canonical_id TEXT NOT NULL,
                    preset_key TEXT NOT NULL,
                    last_transcript_id INTEGER NOT NULL DEFAULT 0,
                    last_generated_at INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(canonical_id, preset_key),
                    FOREIGN KEY (canonical_id)
                        REFERENCES sys_identities(canonical_id)
                );

                CREATE TABLE IF NOT EXISTS audit_legacy_migrations (
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

                CREATE TABLE IF NOT EXISTS audit_migration_quarantine (
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
                        REFERENCES audit_legacy_migrations(migration_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS job_memory_reviews (
                    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    persona_id INTEGER NOT NULL,
                    episode_id INTEGER NOT NULL,
                    exchange_key TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN (
                            'pending', 'reviewing', 'completed', 'failed'
                        )),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(persona_id, exchange_key),
                    FOREIGN KEY (persona_id)
                        REFERENCES sys_personas(persona_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS audit_memory_actions (
                    action_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    persona_id INTEGER NOT NULL,
                    exchange_key TEXT NOT NULL,
                    action_index INTEGER NOT NULL,
                    operation TEXT NOT NULL
                        CHECK(operation IN ('create', 'update', 'no-op')),
                    scope TEXT NOT NULL
                        CHECK(scope IN ('person', 'group')),
                    target_memory_id TEXT,
                    semantic_hash TEXT,
                    status TEXT NOT NULL,
                    error_code TEXT,
                    created_at INTEGER NOT NULL,
                    UNIQUE(persona_id, exchange_key, action_index),
                    FOREIGN KEY (persona_id)
                        REFERENCES sys_personas(persona_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS audit_migrations (
                    migration_key TEXT PRIMARY KEY,
                    from_version INTEGER NOT NULL,
                    to_version INTEGER NOT NULL,
                    backup_path TEXT,
                    status TEXT NOT NULL,
                    counts_json TEXT NOT NULL DEFAULT '{}',
                    verification_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    started_at INTEGER NOT NULL,
                    completed_at INTEGER
                );
                """
            )
            preference_columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(cfg_memory_settings)"
                )
            }
            if "next_retry_at" not in preference_columns:
                conn.execute(
                    """
                    ALTER TABLE cfg_memory_settings
                    ADD COLUMN next_retry_at INTEGER NOT NULL DEFAULT 0
                    """
                )
            if "failure_count" not in preference_columns:
                conn.execute(
                    """
                    ALTER TABLE cfg_memory_settings
                    ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0
                    """
                )
            session_columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(cfg_session_settings)"
                )
            }
            if "agent_enabled" not in session_columns:
                conn.execute(
                    """
                    ALTER TABLE cfg_session_settings
                    ADD COLUMN agent_enabled INTEGER
                    """
                )
            now = int(
                getattr(self, "_migration_started_at", _now_ts())
            )
            with self._transaction(conn):
                if migration_mode:
                    self._migrate_legacy_control_tables_in_tx(conn)
                presets = {
                    normalize_preset(row[0])
                    for row in conn.execute(
                        """
                        SELECT preset_key FROM cfg_session_settings
                        UNION SELECT active_preset FROM cfg_conversation_settings
                        """
                    ).fetchall()
                    if str(row[0] or "").strip()
                }
                if migration_mode and _table_exists(conn, "memory_facts"):
                    presets.update(
                        normalize_preset(row[0])
                        for row in conn.execute(
                            "SELECT DISTINCT preset_key FROM memory_facts"
                        ).fetchall()
                    )
                if migration_mode and _table_exists(
                    conn, "raw_transcript_messages"
                ):
                    transcript_columns = {
                        str(row["name"])
                        for row in conn.execute(
                            "PRAGMA table_info(raw_transcript_messages)"
                        )
                    }
                    if "preset_key" in transcript_columns:
                        presets.update(
                            normalize_preset(row[0])
                            for row in conn.execute(
                                """
                                SELECT DISTINCT preset_key
                                FROM raw_transcript_messages
                                """
                            ).fetchall()
                        )
                if migration_mode and _table_exists(
                    conn, "persona_memory_partitions"
                ):
                    presets.update(
                        normalize_preset(row[0])
                        for row in conn.execute(
                            """
                            SELECT preset_key
                            FROM persona_memory_partitions
                            """
                        ).fetchall()
                    )
                for preset_s in sorted(presets):
                    partition = self._ensure_persona_partition_in_tx(
                        conn,
                        preset_s,
                        now,
                    )
                    if migration_mode:
                        self._sync_legacy_people_partition_in_tx(
                            conn,
                            partition=partition,
                        )
                        self._migrate_v4_persona_partition_in_tx(
                            conn,
                            partition=partition,
                            now=now,
                        )
                if migration_mode:
                    self._migrate_legacy_transcripts_in_tx(conn, now=now)
                    self._drop_legacy_content_tables_in_tx(conn)
                conn.execute(
                    """
                    UPDATE job_memory_reviews
                    SET status='failed',
                        next_retry_at=0,
                        last_error=COALESCE(
                            last_error,
                            'interrupted before completion'
                        ),
                        updated_at=?
                    WHERE status='reviewing'
                    """,
                    (now,),
                )
                conn.execute(
                    """
                    INSERT INTO sys_schema(schema_name, version, updated_at)
                    VALUES('jianer_ai_memory', ?, ?)
                    ON CONFLICT(schema_name)
                    DO UPDATE SET
                        version=excluded.version,
                        updated_at=excluded.updated_at
                    """,
                    (SCHEMA_VERSION, now),
                )
                if migration_mode:
                    backup_path = getattr(
                        self,
                        "_migration_backup_path",
                        None,
                    )
                    conn.execute(
                        """
                        INSERT INTO audit_migrations(
                            migration_key,
                            from_version,
                            to_version,
                            backup_path,
                            status,
                            started_at,
                            completed_at
                        )
                        VALUES('schema-v5', 4, 5, ?, 'completed', ?, ?)
                        ON CONFLICT(migration_key)
                        DO UPDATE SET
                            backup_path=excluded.backup_path,
                            status='completed',
                            completed_at=excluded.completed_at
                        """,
                        (
                            str(backup_path) if backup_path else None,
                            now,
                            now,
                        ),
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
            INSERT INTO sys_identities(
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
                FROM sys_identities
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
            FROM sys_identity_aliases
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
            INSERT INTO sys_identity_aliases(
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
                FROM sys_identity_aliases
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
                    UPDATE sys_identity_authorizations
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
                    INSERT INTO sys_identity_authorizations(
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
                    UPDATE sys_identity_authorizations
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
            FROM sys_identity_authorizations
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
            INSERT INTO job_generation_barriers(
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
                version=job_generation_barriers.version + 1,
                active_generation_id=NULL,
                invalidated_at=excluded.invalidated_at,
                completed_at=NULL
            """,
            (canonical_id, preset, now, now),
        )
        row = conn.execute(
            """
            SELECT version
            FROM job_generation_barriers
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
        partitions = conn.execute(
            "SELECT * FROM sys_persona_partitions ORDER BY persona_id"
        ).fetchall()
        for partition in partitions:
            people = str(partition["people_table"])
            evidence_table = str(partition["evidence_table"])
            source_facts = conn.execute(
                f"""
                SELECT *
                FROM {people}
                WHERE person_id = ?
                ORDER BY memory_id
                """,
                (source,),
            ).fetchall()
            for fact in source_facts:
                existing = conn.execute(
                    f"""
                    SELECT *
                    FROM {people}
                    WHERE person_id = ? AND semantic_hash = ?
                    """,
                    (target, str(fact["semantic_hash"])),
                ).fetchone()
                source_id = int(fact["memory_id"])
                if existing is None:
                    conn.execute(
                        f"""
                        UPDATE {people}
                        SET person_id = ?, updated_at = ?
                        WHERE memory_id = ?
                        """,
                        (target, now, source_id),
                    )
                    continue
                target_id = int(existing["memory_id"])
                conn.execute(
                    f"""
                    INSERT INTO {evidence_table}(
                        memory_scope,
                        memory_id,
                        conversation_ref,
                        message_key,
                        excerpt,
                        observed_at,
                        evidence_hash,
                        metadata_json
                    )
                    SELECT
                        memory_scope,
                        ?,
                        conversation_ref,
                        message_key,
                        excerpt,
                        observed_at,
                        evidence_hash,
                        metadata_json
                    FROM {evidence_table}
                    WHERE memory_scope = 'person' AND memory_id = ?
                    ON CONFLICT(memory_scope, memory_id, evidence_hash)
                    DO NOTHING
                    """,
                    (target_id, source_id),
                )
                conn.execute(
                    f"""
                    DELETE FROM {evidence_table}
                    WHERE memory_scope = 'person' AND memory_id = ?
                    """,
                    (source_id,),
                )
                conn.execute(
                    f"""
                    UPDATE {people}
                    SET importance = MAX(importance, ?),
                        confidence = MAX(confidence, ?),
                        source_count = (
                            SELECT COUNT(*)
                            FROM {evidence_table}
                            WHERE memory_scope = 'person' AND memory_id = ?
                        ),
                        first_seen_at = MIN(first_seen_at, ?),
                        last_confirmed_at = MAX(last_confirmed_at, ?),
                        updated_at = ?
                    WHERE memory_id = ?
                    """,
                    (
                        float(fact["importance"]),
                        float(fact["confidence"]),
                        target_id,
                        int(fact["first_seen_at"]),
                        int(fact["last_confirmed_at"]),
                        now,
                        target_id,
                    ),
                )
                conn.execute(
                    f"DELETE FROM {people} WHERE memory_id = ?",
                    (source_id,),
                )

    def _merge_suppressions_in_tx(
        self,
        conn: sqlite3.Connection,
        source: str,
        target: str,
    ) -> None:
        for partition in conn.execute(
            "SELECT suppressions_table FROM sys_persona_partitions"
        ).fetchall():
            deleted = str(partition["suppressions_table"])
            rows = conn.execute(
                f"""
                SELECT *
                FROM {deleted}
                WHERE scope = 'person' AND subject_ref = ?
                """,
                (source,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    f"""
                    INSERT INTO {deleted}(
                        scope,
                        subject_ref,
                        deletion_kind,
                        semantic_hash,
                        snapshot,
                        source_memory_id,
                        reason,
                        deleted_at
                    )
                    VALUES('person', ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(
                        scope,
                        subject_ref,
                        deletion_kind,
                        semantic_hash
                    )
                    DO UPDATE SET
                        deleted_at=MAX(
                            {deleted}.deleted_at,
                            excluded.deleted_at
                        )
                    """,
                    (
                        target,
                        str(row["deletion_kind"]),
                        str(row["semantic_hash"]),
                        str(row["snapshot"]),
                        row["source_memory_id"],
                        str(row["reason"]),
                        int(row["deleted_at"]),
                    ),
                )
            conn.execute(
                f"""
                DELETE FROM {deleted}
                WHERE scope = 'person' AND subject_ref = ?
                """,
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
                FROM job_generation_barriers
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
                    FROM job_generation_barriers
                    WHERE canonical_id IN (?, ?) AND preset_key = ?
                    """,
                    (source, target, preset),
                )
            ]
            next_version = max(versions, default=0) + 1
            conn.execute(
                """
                INSERT INTO job_generation_barriers(
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
            "DELETE FROM job_generation_barriers WHERE canonical_id = ?",
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
            "SELECT * FROM job_generation_cursors WHERE canonical_id = ?",
            (source,),
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                INSERT INTO job_generation_cursors(
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
                        job_generation_cursors.last_transcript_id,
                        excluded.last_transcript_id
                    ),
                    last_generated_at=MAX(
                        job_generation_cursors.last_generated_at,
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
            "DELETE FROM job_generation_cursors WHERE canonical_id = ?",
            (source,),
        )
        identity_rows = conn.execute(
            """
            SELECT *
            FROM job_identity_generation_cursors
            WHERE canonical_id = ?
            """,
            (source,),
        ).fetchall()
        for row in identity_rows:
            conn.execute(
                """
                INSERT INTO job_identity_generation_cursors(
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
                        job_identity_generation_cursors.last_transcript_id,
                        excluded.last_transcript_id
                    ),
                    last_generated_at=MAX(
                        job_identity_generation_cursors.last_generated_at,
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
            DELETE FROM job_identity_generation_cursors
            WHERE canonical_id = ?
            """,
            (source,),
        )

        preference_rows = conn.execute(
            "SELECT * FROM cfg_memory_settings WHERE canonical_id = ?",
            (source,),
        ).fetchall()
        for row in preference_rows:
            existing = conn.execute(
                """
                SELECT *
                FROM cfg_memory_settings
                WHERE canonical_id = ? AND preset_key = ?
                """,
                (target, str(row["preset_key"])),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    UPDATE cfg_memory_settings
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
                UPDATE cfg_memory_settings
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
                DELETE FROM cfg_memory_settings
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
                        FROM sys_identity_aliases
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
                        UPDATE sys_identity_aliases
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
                        INSERT INTO audit_identity_merges(
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
                        INSERT INTO audit_identity_merges(
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
                for partition_row in conn.execute(
                    "SELECT * FROM sys_persona_partitions"
                ).fetchall():
                    episodes_table = str(partition_row["episodes_table"])
                    groups_table = str(partition_row["groups_table"])
                    conn.execute(
                        f"""
                        UPDATE {episodes_table}
                        SET person_id = ?
                        WHERE person_id = ?
                        """,
                        (target_canonical, source_canonical),
                    )
                    conn.execute(
                        f"""
                        UPDATE {groups_table}
                        SET last_writer_person_id = ?
                        WHERE last_writer_person_id = ?
                        """,
                        (target_canonical, source_canonical),
                    )
                self._merge_barriers_in_tx(
                    conn, source_canonical, target_canonical, now
                )
                self._merge_cursors_in_tx(
                    conn, source_canonical, target_canonical, now
                )
                for chat_row in conn.execute(
                    "SELECT table_name FROM sys_chat_partitions"
                ).fetchall():
                    chat_table = str(chat_row["table_name"])
                    conn.execute(
                        f"""
                        UPDATE {chat_table}
                        SET sender_person_id = ?
                        WHERE sender_person_id = ?
                        """,
                        (target_canonical, source_canonical),
                    )
                conn.execute(
                    """
                    UPDATE sys_chat_message_index
                    SET sender_person_id = ?
                    WHERE sender_person_id = ?
                    """,
                    (target_canonical, source_canonical),
                )
                conn.execute(
                    """
                    UPDATE sys_identity_aliases
                    SET canonical_id = ?, updated_at = ?
                    WHERE canonical_id = ?
                    """,
                    (target_canonical, now, source_canonical),
                )
                conn.execute(
                    """
                    UPDATE sys_identities
                    SET merged_into = ?, updated_at = ?
                    WHERE canonical_id = ?
                    """,
                    (target_canonical, now, source_canonical),
                )
                conn.execute(
                    """
                    INSERT INTO audit_identity_merges(
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
        row = conn.execute(
            """
            SELECT conversation_pk, last_seen_at
            FROM sys_conversations
            WHERE protocol = ?
              AND self_id = ?
              AND conversation_kind = ?
              AND conversation_id = ?
            """,
            (protocol_s, self_id_s, kind_s, conversation_id_s),
        ).fetchone()
        if row is None:
            cursor = conn.execute(
                """
                INSERT INTO sys_conversations(
                    protocol,
                    self_id,
                    conversation_kind,
                    conversation_id,
                    created_at,
                    last_seen_at
                )
                VALUES(?, ?, ?, ?, ?, ?)
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
            return int(cursor.lastrowid)
        if now > int(row["last_seen_at"]):
            conn.execute(
                """
                UPDATE sys_conversations
                SET last_seen_at = ?
                WHERE conversation_pk = ?
                """,
                (now, int(row["conversation_pk"])),
            )
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
                    INSERT INTO cfg_conversation_settings(
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
                FROM cfg_conversation_settings p
                JOIN sys_conversations c
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
        direction: str = "incoming",
        sender_name: str = "",
        reply_to: str | None = None,
        segments_json: str = "[]",
    ) -> TranscriptWriteResult:
        content_s = _required_text(content, "content")
        preset_s = normalize_preset(preset)
        direction_s = str(direction or "incoming").strip().casefold()
        if direction_s not in {"incoming", "outgoing"}:
            raise ValueError("direction must be 'incoming' or 'outgoing'")
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
        chat = self._ensure_chat_partition_in_tx(conn, conversation_pk, now)
        persona = self._ensure_persona_partition_in_tx(conn, preset_s, now)
        cursor = conn.execute(
            """
            INSERT INTO sys_chat_message_index(
                conversation_ref,
                table_name,
                message_key,
                direction,
                sender_person_id,
                active_persona_id,
                occurred_at,
                message_type
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_ref, message_key) DO NOTHING
            """,
            (
                conversation_pk,
                chat.table_name,
                message_key,
                direction_s,
                sender_canonical,
                persona.persona_id,
                occurred_at_i,
                message_type_s,
            ),
        )
        inserted = cursor.rowcount > 0
        row = conn.execute(
            """
            SELECT message_uid
            FROM sys_chat_message_index
            WHERE conversation_ref = ? AND message_key = ?
            """,
            (conversation_pk, message_key),
        ).fetchone()
        if row is None:
            raise MemoryStoreError("failed to register chat message")
        message_uid = int(row["message_uid"])
        conn.execute(
            f"""
            INSERT INTO {chat.table_name}(
                message_uid,
                message_key,
                direction,
                sender_person_id,
                sender_name,
                text,
                occurred_at,
                reply_to,
                message_type,
                active_persona_id,
                segments_json,
                content_hash,
                captured_at,
                external_message_id,
                sender_protocol,
                sender_self_id,
                sender_external_id
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_key) DO NOTHING
            """,
            (
                message_uid,
                message_key,
                direction_s,
                sender_canonical,
                str(sender_name or ""),
                content_s,
                occurred_at_i,
                str(reply_to) if reply_to is not None else None,
                message_type_s,
                persona.persona_id,
                str(segments_json or "[]"),
                payload_hash,
                now,
                external_message_id,
                sender_protocol_s,
                sender_self_id_s,
                sender_external_id_s,
            ),
        )
        local = conn.execute(
            f"SELECT seq FROM {chat.table_name} WHERE message_key = ?",
            (message_key,),
        ).fetchone()
        if local is None:
            raise MemoryStoreError("failed to persist chat message")
        conn.execute(
            """
            UPDATE sys_chat_message_index
            SET local_seq = ?
            WHERE message_uid = ?
            """,
            (int(local["seq"]), message_uid),
        )
        return TranscriptWriteResult(
            transcript_id=message_uid,
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
        direction: str = "incoming",
        sender_name: str = "",
        reply_to: str | None = None,
        segments_json: str = "[]",
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
                    direction=direction,
                    sender_name=sender_name,
                    reply_to=reply_to,
                    segments_json=segments_json,
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
                if str(direction).casefold() == "incoming":
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
                partition_row = conn.execute(
                    """
                    SELECT p.table_name
                    FROM sys_chat_partitions p
                    JOIN sys_conversations c
                      ON c.conversation_pk = p.conversation_ref
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
                if partition_row is None:
                    return False
                table_name = str(partition_row["table_name"])
                row = conn.execute(
                    f"""
                    SELECT *
                    FROM {table_name}
                    WHERE external_message_id = ?
                    ORDER BY seq DESC
                    LIMIT 1
                    """,
                    (external_message_id,),
                ).fetchone()
                if row is None:
                    return False
                content = str(row["text"])
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
                    f"""
                    UPDATE {table_name}
                    SET text = ?, content_hash = ?
                    WHERE message_uid = ?
                    """,
                    (redacted, payload_hash, int(row["message_uid"])),
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
            clauses.append("p.preset_key = ?")
            params.append(normalize_preset(preset))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = self._connect()
        try:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM sys_chat_message_index r
                JOIN sys_conversations c
                  ON c.conversation_pk = r.conversation_ref
                LEFT JOIN sys_personas p
                  ON p.persona_id = r.active_persona_id
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
            partition = conn.execute(
                """
                SELECT p.table_name
                FROM sys_chat_partitions p
                JOIN sys_conversations c
                  ON c.conversation_pk = p.conversation_ref
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
            if partition is None:
                return ()
            table_name = str(partition["table_name"])
            rows = conn.execute(
                f"""
                SELECT sender_external_id, MAX(occurred_at) AS last_seen
                FROM {table_name}
                WHERE sender_external_id != ''
                GROUP BY sender_external_id
                ORDER BY last_seen DESC
                LIMIT ?
                """,
                (limit_i,),
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
                deleted = 0
                partitions = conn.execute(
                    """
                    SELECT conversation_ref, table_name
                    FROM sys_chat_partitions
                    ORDER BY conversation_ref
                    """
                ).fetchall()
                for partition in partitions:
                    table_name = str(partition["table_name"])
                    rows = conn.execute(
                        f"""
                        SELECT message_uid
                        FROM {table_name}
                        WHERE occurred_at < ?
                        ORDER BY occurred_at, seq
                        LIMIT 1000
                        """,
                        (cutoff,),
                    ).fetchall()
                    message_uids = tuple(
                        int(row["message_uid"]) for row in rows
                    )
                    if not message_uids:
                        conn.execute(
                            """
                            UPDATE sys_chat_partitions
                            SET last_pruned_at = ?, updated_at = ?
                            WHERE conversation_ref = ?
                            """,
                            (
                                int(now if now is not None else _now_ts()),
                                int(now if now is not None else _now_ts()),
                                int(partition["conversation_ref"]),
                            ),
                        )
                        continue
                    placeholders = ",".join("?" for _ in message_uids)
                    cursor = conn.execute(
                        f"""
                        DELETE FROM {table_name}
                        WHERE message_uid IN ({placeholders})
                        """,
                        message_uids,
                    )
                    conn.execute(
                        f"""
                        DELETE FROM sys_chat_message_index
                        WHERE message_uid IN ({placeholders})
                        """,
                        message_uids,
                    )
                    deleted += int(cursor.rowcount)
                    pruned_at = int(
                        now if now is not None else _now_ts()
                    )
                    conn.execute(
                        """
                        UPDATE sys_chat_partitions
                        SET last_pruned_at = ?, updated_at = ?
                        WHERE conversation_ref = ?
                        """,
                        (
                            pruned_at,
                            pruned_at,
                            int(partition["conversation_ref"]),
                        ),
                    )
                return deleted
        finally:
            conn.close()

    def purge_transcripts(
        self,
        *,
        now: int | None = None,
        retention_days: int = GROUP_TRANSCRIPT_RETENTION_DAYS,
    ) -> int:
        """Prune one bounded batch from every chat partition."""

        return self.prune_group_transcripts(
            now=now, retention_days=retention_days
        )

    def query_recent_chat(
        self,
        *,
        protocol: Any,
        self_id: Any,
        conversation_kind: Any,
        conversation_id: Any,
        limit: int = 50,
        max_characters: int = 8000,
        query: str = "",
    ) -> tuple[ChatMessage, ...]:
        limit_i = max(1, min(int(limit), 100))
        max_chars = max(100, min(int(max_characters), 32_000))
        cutoff = _now_ts() - (CHAT_TRANSCRIPT_RETENTION_DAYS * 86400)
        conn = self._connect()
        try:
            partition = conn.execute(
                """
                SELECT p.*, c.protocol, c.self_id,
                       c.conversation_kind, c.conversation_id
                FROM sys_chat_partitions p
                JOIN sys_conversations c
                  ON c.conversation_pk = p.conversation_ref
                WHERE c.protocol = ?
                  AND c.self_id = ?
                  AND c.conversation_kind = ?
                  AND c.conversation_id = ?
                """,
                (
                    normalize_protocol(protocol),
                    _required_text(self_id, "self_id"),
                    _required_text(
                        conversation_kind,
                        "conversation_kind",
                    ).casefold(),
                    _required_text(conversation_id, "conversation_id"),
                ),
            ).fetchone()
            if partition is None:
                return ()
            table_name = str(partition["table_name"])
            query_s = str(query or "").strip()
            if query_s:
                rows = conn.execute(
                    f"""
                    SELECT m.*, p.preset_key
                    FROM {table_name} m
                    LEFT JOIN sys_personas p
                      ON p.persona_id = m.active_persona_id
                    WHERE m.occurred_at >= ?
                      AND m.text LIKE ? ESCAPE '\\'
                    ORDER BY m.occurred_at DESC, m.seq DESC
                    LIMIT ?
                    """,
                    (
                        cutoff,
                        "%"
                        + query_s.replace("\\", "\\\\")
                        .replace("%", "\\%")
                        .replace("_", "\\_")
                        + "%",
                        limit_i,
                    ),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT m.*, p.preset_key
                    FROM {table_name} m
                    LEFT JOIN sys_personas p
                      ON p.persona_id = m.active_persona_id
                    WHERE m.occurred_at >= ?
                    ORDER BY m.occurred_at DESC, m.seq DESC
                    LIMIT ?
                    """,
                    (cutoff, limit_i),
                ).fetchall()
            selected: list[ChatMessage] = []
            used = 0
            for row in rows:
                content = str(row["text"])
                if selected and used + len(content) > max_chars:
                    break
                if len(content) > max_chars and not selected:
                    content = content[-max_chars:]
                used += len(content)
                selected.append(
                    ChatMessage(
                        message_uid=int(row["message_uid"]),
                        conversation_pk=int(partition["conversation_ref"]),
                        message_key=str(row["message_key"]),
                        direction=str(row["direction"]),
                        sender_canonical_id=(
                            str(row["sender_person_id"])
                            if row["sender_person_id"] is not None
                            else None
                        ),
                        sender_name=str(row["sender_name"]),
                        content=content,
                        occurred_at=int(row["occurred_at"]),
                        message_type=str(row["message_type"]),
                        active_preset=str(row["preset_key"] or "default"),
                    )
                )
            return tuple(reversed(selected))
        finally:
            conn.close()

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
            INSERT INTO cfg_session_settings(
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
            FROM cfg_session_settings
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
                    UPDATE cfg_session_settings
                    SET {", ".join(assignments)}
                    WHERE session_id = ?
                    """,
                    params,
                )
                row = conn.execute(
                    "SELECT * FROM cfg_session_settings WHERE session_id = ?",
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
                "SELECT * FROM cfg_session_settings WHERE session_id = ?",
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
            INSERT INTO cfg_memory_settings(
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
            FROM cfg_memory_settings
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
                    UPDATE cfg_memory_settings
                    SET {", ".join(assignments)}
                    WHERE canonical_id = ? AND preset_key = ?
                    """,
                    params,
                )
                row = conn.execute(
                    """
                    SELECT *
                    FROM cfg_memory_settings
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
                FROM job_identity_generation_cursors
                WHERE canonical_id = ? AND preset_key = ?
                """,
                (canonical, preset_s),
            ).fetchone()
            last_transcript_id = (
                int(cursor["last_transcript_id"])
                if cursor is not None
                else 0
            )
            partition = self._ensure_persona_partition_in_tx(
                conn,
                preset_s,
                now,
            )
            raw_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sys_chat_message_index
                    WHERE sender_person_id = ? AND active_persona_id = ?
                    """,
                    (canonical, partition.persona_id),
                ).fetchone()[0]
            )
            new_raw_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sys_chat_message_index
                    WHERE sender_person_id = ?
                      AND active_persona_id = ?
                      AND message_uid > ?
                    """,
                    (canonical, partition.persona_id, last_transcript_id),
                ).fetchone()[0]
            )
            memory_count = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM {partition.people_table}
                    WHERE person_id = ?
                    """,
                    (canonical,),
                ).fetchone()[0]
            )
            suppression_count = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM {partition.suppressions_table}
                    WHERE scope = 'person' AND subject_ref = ?
                    """,
                    (canonical,),
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
                FROM cfg_memory_settings
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
                    FROM job_identity_generation_cursors
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
                    FROM sys_chat_message_index r
                    JOIN sys_conversations c
                      ON c.conversation_pk = r.conversation_ref
                    JOIN sys_personas p
                      ON p.persona_id = r.active_persona_id
                    WHERE r.sender_person_id = ?
                      AND p.preset_key = ?
                      AND r.message_uid > ?
                    ORDER BY r.message_uid DESC
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
                    INSERT INTO job_generation_barriers(
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
                        version=job_generation_barriers.version + 1,
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
                    FROM job_generation_barriers
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
            FROM job_generation_barriers
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
                persona_partition = self._ensure_persona_partition_in_tx(
                    conn,
                    preset_s,
                    now,
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
                        FROM cfg_session_settings
                        WHERE session_id = ?
                        """,
                        (session_id,),
                    ).fetchone()
                    cursor_row = conn.execute(
                        """
                        SELECT last_transcript_id
                        FROM job_generation_cursors
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
                            message_uid AS transcript_id,
                            conversation_ref AS conversation_pk,
                            sender_person_id AS sender_canonical_id,
                            table_name,
                            occurred_at,
                            message_type
                        FROM sys_chat_message_index
                        WHERE conversation_ref = ?
                          AND sender_person_id = ?
                          AND active_persona_id = ?
                          AND message_uid > ?
                        ORDER BY message_uid
                        LIMIT ?
                        """,
                        (
                            int(session_row["conversation_pk"]),
                            canonical,
                            persona_partition.persona_id,
                            after_id,
                            limit_i,
                        ),
                    ).fetchall()
                else:
                    session_id = None
                    cursor_row = conn.execute(
                        """
                        SELECT last_transcript_id
                        FROM job_identity_generation_cursors
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
                            message_uid AS transcript_id,
                            conversation_ref AS conversation_pk,
                            sender_person_id AS sender_canonical_id,
                            table_name,
                            occurred_at,
                            message_type
                        FROM sys_chat_message_index
                        WHERE sender_person_id = ?
                          AND active_persona_id = ?
                          AND message_uid > ?
                        ORDER BY message_uid
                        LIMIT ?
                        """,
                        (
                            canonical,
                            persona_partition.persona_id,
                            after_id,
                            limit_i,
                        ),
                    ).fetchall()
                loaded_rows: list[dict[str, Any]] = []
                for index_row in rows:
                    content_row = conn.execute(
                        f"""
                        SELECT text
                        FROM {str(index_row['table_name'])}
                        WHERE message_uid = ?
                        """,
                        (int(index_row["transcript_id"]),),
                    ).fetchone()
                    if content_row is None:
                        continue
                    loaded_rows.append(
                        {
                            "transcript_id": int(
                                index_row["transcript_id"]
                            ),
                            "conversation_pk": int(
                                index_row["conversation_pk"]
                            ),
                            "sender_canonical_id": index_row[
                                "sender_canonical_id"
                            ],
                            "content": str(content_row["text"]),
                            "occurred_at": int(index_row["occurred_at"]),
                            "message_type": str(index_row["message_type"]),
                        }
                    )
                rows = loaded_rows
                if len(rows) < min_rows_i:
                    return None
                generation_id = uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO job_generation_barriers(
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
                        version=job_generation_barriers.version + 1,
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
                    FROM job_generation_barriers
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
            INSERT INTO job_generation_cursors(
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
                    job_generation_cursors.last_transcript_id,
                    excluded.last_transcript_id
                ),
                last_generated_at=MAX(
                    job_generation_cursors.last_generated_at,
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
            UPDATE cfg_session_settings
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
        partition = self._ensure_persona_partition_in_tx(
            conn,
            preset,
            _now_ts(),
        )
        row = conn.execute(
            f"""
            SELECT 1
            FROM {partition.suppressions_table}
            WHERE scope = 'person'
              AND subject_ref = ?
              AND deletion_kind = ?
              AND semantic_hash = ?
            LIMIT 1
            """,
            (canonical_id, kind, fingerprint),
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
        canonical_fact: str | None = None,
        confidence: float = 1.0,
        origin: str = "generated",
    ) -> tuple[str, int | None]:
        content = str(memory.content or "").strip()
        if not content:
            return "empty", None
        canonical_fact_s = str(canonical_fact or content).strip()
        fact_fingerprint = memory_fingerprint(canonical_fact_s)
        partition = self._ensure_persona_partition_in_tx(conn, preset, now)
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
            f"""
            SELECT memory_id, importance
            FROM {partition.people_table}
            WHERE person_id = ? AND semantic_hash = ?
            """,
            (canonical_id, fact_fingerprint),
        ).fetchone()
        weight = max(0.0, min(1.0, float(memory.weight)))
        if existing is None:
            cursor = conn.execute(
                f"""
                INSERT INTO {partition.people_table}(
                    person_id,
                    memory_text,
                    importance,
                    confidence,
                    updated_at,
                    canonical_fact,
                    semantic_hash,
                    source_count,
                    persona_revision,
                    first_seen_at,
                    last_confirmed_at,
                    created_at,
                    origin
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?, ?, ?)
                """,
                (
                    canonical_id,
                    content,
                    weight,
                    max(0.0, min(1.0, float(confidence))),
                    now,
                    canonical_fact_s,
                    fact_fingerprint,
                    now,
                    now,
                    now,
                    str(origin or "generated"),
                ),
            )
            fact_id = int(cursor.lastrowid)
            outcome = "inserted"
        else:
            fact_id = int(existing["memory_id"])
            conn.execute(
                f"""
                UPDATE {partition.people_table}
                SET memory_text = ?,
                    canonical_fact = ?,
                    importance = ?,
                    confidence = MAX(confidence, ?),
                    updated_at = ?,
                    last_confirmed_at = ?
                WHERE memory_id = ?
                """,
                (
                    content,
                    canonical_fact_s,
                    max(float(existing["importance"]), weight),
                    max(0.0, min(1.0, float(confidence))),
                    now,
                    now,
                    fact_id,
                ),
            )
            outcome = "updated"

        for evidence, fingerprint in zip(
            evidence_values, evidence_fingerprints
        ):
            conversation_ref = evidence.conversation_pk
            message_key: str | None = None
            if evidence.transcript_id is not None:
                message_row = conn.execute(
                    """
                    SELECT conversation_ref, message_key
                    FROM sys_chat_message_index
                    WHERE message_uid = ?
                    """,
                    (int(evidence.transcript_id),),
                ).fetchone()
                if message_row is not None:
                    message_key = str(message_row["message_key"])
                    if conversation_ref is None:
                        conversation_ref = int(
                            message_row["conversation_ref"]
                        )
            conn.execute(
                f"""
                INSERT INTO {partition.evidence_table}(
                    memory_scope,
                    memory_id,
                    conversation_ref,
                    message_key,
                    excerpt,
                    observed_at,
                    evidence_hash,
                    metadata_json
                )
                VALUES('person', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_scope, memory_id, evidence_hash)
                DO UPDATE SET
                    observed_at=MAX(
                        {partition.evidence_table}.observed_at,
                        excluded.observed_at
                    ),
                    metadata_json=excluded.metadata_json
                """,
                (
                    fact_id,
                    conversation_ref,
                    message_key,
                    str(evidence.content).strip(),
                    int(evidence.observed_at or now),
                    fingerprint,
                    _json(evidence.metadata),
                ),
            )
        conn.execute(
            f"""
            UPDATE {partition.people_table}
            SET source_count = (
                SELECT COUNT(*)
                FROM {partition.evidence_table}
                WHERE memory_scope = 'person' AND memory_id = ?
            )
            WHERE memory_id = ?
            """,
            (fact_id, fact_id),
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
                        FROM job_generation_barriers
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
                    UPDATE job_generation_barriers
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
                        INSERT INTO job_identity_generation_cursors(
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
                                job_identity_generation_cursors.last_transcript_id,
                                excluded.last_transcript_id
                            ),
                            last_generated_at=MAX(
                                job_identity_generation_cursors.last_generated_at,
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
                    UPDATE cfg_memory_settings
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
                    UPDATE job_generation_barriers
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
                    UPDATE cfg_memory_settings
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

    def create_memory(
        self,
        *,
        canonical_user_id: str,
        preset: Any = "default",
        content: str,
        weight: float = 1.0,
        canonical_fact: str | None = None,
        importance: float | None = None,
        confidence: float = 1.0,
        origin: str = "direct",
        honor_deleted: bool = False,
    ) -> MemoryWriteResult | None:
        """Create or explicitly reconfirm one user-scoped memory.

        Direct user edits invalidate any in-flight extraction and override an
        exact fact tombstone without advancing the background generation
        cursor.
        """

        content_s = _required_text(content, "content")
        preset_s = normalize_preset(preset)
        weight_f = max(
            0.0,
            min(1.0, float(weight if importance is None else importance)),
        )
        canonical_fact_s = str(canonical_fact or content_s).strip()
        fingerprint = memory_fingerprint(canonical_fact_s)
        now = _now_ts()
        conn = self._connect()
        try:
            with self._transaction(conn):
                canonical = self._resolve_canonical_in_tx(
                    conn, canonical_user_id
                )
                self._invalidate_barrier_in_tx(
                    conn, canonical, preset_s, now
                )
                partition = self._ensure_persona_partition_in_tx(
                    conn,
                    preset_s,
                    now,
                )
                if not honor_deleted:
                    conn.execute(
                        f"""
                        DELETE FROM {partition.suppressions_table}
                        WHERE scope = 'person'
                          AND subject_ref = ?
                          AND deletion_kind = 'fact'
                          AND semantic_hash = ?
                        """,
                        (canonical, fingerprint),
                    )
                outcome, fact_id = self._upsert_memory_in_tx(
                    conn,
                    canonical_id=canonical,
                    preset=preset_s,
                    memory=GeneratedMemory(
                        content=content_s,
                        weight=weight_f,
                    ),
                    now=now,
                    honor_suppressions=honor_deleted,
                    canonical_fact=canonical_fact_s,
                    confidence=confidence,
                    origin=origin,
                )
                self._ensure_memory_preference_in_tx(
                    conn,
                    canonical_id=canonical,
                    preset=preset_s,
                    now=now,
                )
                if fact_id is None and honor_deleted:
                    return None
                if fact_id is None:
                    raise MemoryStoreError("direct memory creation failed")
                row = conn.execute(
                    f"""
                    SELECT memory_text, importance
                    FROM {partition.people_table}
                    WHERE memory_id = ?
                    """,
                    (fact_id,),
                ).fetchone()
            return MemoryWriteResult(
                fact_id=fact_id,
                content=str(row["memory_text"]),
                weight=float(row["importance"]),
                outcome=outcome,
            )
        finally:
            conn.close()

    def update_memory(
        self,
        *,
        canonical_user_id: str,
        preset: Any = "default",
        memory_id: str | int,
        content: str,
        weight: float = 1.0,
        canonical_fact: str | None = None,
        importance: float | None = None,
        confidence: float = 1.0,
    ) -> MemoryWriteResult | None:
        """Replace one memory while preserving its stable scoped ID."""

        try:
            fact_id = int(memory_id)
        except (TypeError, ValueError):
            return None
        if fact_id <= 0:
            return None
        content_s = _required_text(content, "content")
        preset_s = normalize_preset(preset)
        weight_f = max(
            0.0,
            min(1.0, float(weight if importance is None else importance)),
        )
        canonical_fact_s = str(canonical_fact or content_s).strip()
        new_fingerprint = memory_fingerprint(canonical_fact_s)
        now = _now_ts()
        conn = self._connect()
        try:
            with self._transaction(conn):
                canonical = self._resolve_canonical_in_tx(
                    conn, canonical_user_id
                )
                partition = self._ensure_persona_partition_in_tx(
                    conn,
                    preset_s,
                    now,
                )
                row = conn.execute(
                    f"""
                    SELECT *
                    FROM {partition.people_table}
                    WHERE memory_id = ? AND person_id = ?
                    """,
                    (fact_id, canonical),
                ).fetchone()
                if row is None:
                    return None
                duplicate = conn.execute(
                    f"""
                    SELECT memory_id
                    FROM {partition.people_table}
                    WHERE person_id = ?
                      AND semantic_hash = ?
                      AND memory_id != ?
                    LIMIT 1
                    """,
                    (canonical, new_fingerprint, fact_id),
                ).fetchone()
                if duplicate is not None:
                    raise MemoryConflictError(
                        "another memory already has the requested content"
                    )

                old_content = str(row["memory_text"])
                old_fingerprint = str(row["semantic_hash"])
                old_weight = float(row["importance"])
                self._invalidate_barrier_in_tx(
                    conn, canonical, preset_s, now
                )
                if old_fingerprint != new_fingerprint:
                    conn.execute(
                        f"""
                        INSERT INTO {partition.suppressions_table}(
                            scope,
                            subject_ref,
                            deletion_kind,
                            semantic_hash,
                            snapshot,
                            source_memory_id,
                            reason,
                            deleted_at
                        )
                        VALUES('person', ?, 'fact', ?, ?, ?, 'user_updated', ?)
                        ON CONFLICT(
                            scope,
                            subject_ref,
                            deletion_kind,
                            semantic_hash
                        )
                        DO UPDATE SET
                            snapshot=excluded.snapshot,
                            source_memory_id=excluded.source_memory_id,
                            reason=excluded.reason,
                            deleted_at=excluded.deleted_at
                        """,
                        (
                            canonical,
                            old_fingerprint,
                            old_content,
                            str(fact_id),
                            now,
                        ),
                    )
                conn.execute(
                    f"""
                    DELETE FROM {partition.suppressions_table}
                    WHERE scope = 'person'
                      AND subject_ref = ?
                      AND deletion_kind = 'fact'
                      AND semantic_hash = ?
                    """,
                    (canonical, new_fingerprint),
                )
                stored_weight = max(old_weight, weight_f)
                outcome = (
                    "unchanged"
                    if old_content == content_s and stored_weight == old_weight
                    else "updated"
                )
                conn.execute(
                    f"""
                    UPDATE {partition.people_table}
                    SET semantic_hash = ?,
                        canonical_fact = ?,
                        memory_text = ?,
                        importance = ?,
                        confidence = ?,
                        updated_at = ?,
                        last_confirmed_at = ?
                    WHERE memory_id = ?
                    """,
                    (
                        new_fingerprint,
                        canonical_fact_s,
                        content_s,
                        stored_weight,
                        max(0.0, min(1.0, float(confidence))),
                        now,
                        now,
                        fact_id,
                    ),
                )
                self._ensure_memory_preference_in_tx(
                    conn,
                    canonical_id=canonical,
                    preset=preset_s,
                    now=now,
                )
            return MemoryWriteResult(
                fact_id=fact_id,
                content=content_s,
                weight=stored_weight,
                outcome=outcome,
            )
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
            with self._transaction(conn):
                canonical = self._resolve_canonical_in_tx(
                    conn, canonical_user_id
                )
                partition = self._ensure_persona_partition_in_tx(
                    conn, preset_s, _now_ts()
                )
                rows = conn.execute(
                    f"""
                    SELECT *
                    FROM {partition.people_table}
                    WHERE person_id = ?
                    ORDER BY importance DESC, updated_at DESC, memory_id DESC
                    LIMIT ?
                    """,
                    (canonical, max(1, int(limit))),
                ).fetchall()
                records: list[MemoryRecord] = []
                for row in rows:
                    evidence_rows = conn.execute(
                        f"""
                        SELECT e.*, i.message_uid
                        FROM {partition.evidence_table} e
                        LEFT JOIN sys_chat_message_index i
                          ON i.conversation_ref = e.conversation_ref
                         AND i.message_key = e.message_key
                        WHERE e.memory_scope = 'person'
                          AND e.memory_id = ?
                        ORDER BY e.observed_at DESC, e.evidence_id DESC
                        """,
                        (int(row["memory_id"]),),
                    ).fetchall()
                    evidence = tuple(
                        MemoryEvidence(
                            content=str(item["excerpt"]),
                            conversation_pk=(
                                int(item["conversation_ref"])
                                if item["conversation_ref"] is not None
                                else None
                            ),
                            transcript_id=(
                                int(item["message_uid"])
                                if item["message_uid"] is not None
                                else None
                            ),
                            observed_at=int(item["observed_at"]),
                            metadata=json.loads(
                                str(item["metadata_json"]) or "{}"
                            ),
                        )
                        for item in evidence_rows
                    )
                    records.append(
                        MemoryRecord(
                            fact_id=int(row["memory_id"]),
                            canonical_user_id=canonical,
                            preset=preset_s,
                            content=str(row["memory_text"]),
                            fingerprint=str(row["semantic_hash"]),
                            weight=float(row["importance"]),
                            created_at=int(row["created_at"]),
                            updated_at=int(row["updated_at"]),
                            evidence=evidence,
                            scope="person",
                            subject_id=canonical,
                            canonical_fact=str(row["canonical_fact"]),
                            confidence=float(row["confidence"]),
                            source_count=int(row["source_count"]),
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
        return self._rank_memories(candidates, query=query, limit=limit)

    @staticmethod
    def _rank_memories(
        candidates: Sequence[MemoryRecord],
        *,
        query: str,
        limit: int,
    ) -> tuple[MemoryRecord, ...]:
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

    def list_group_memories(
        self,
        *,
        preset: Any = "default",
        protocol: Any,
        self_id: Any,
        group_id: Any,
        limit: int = 100,
    ) -> tuple[MemoryRecord, ...]:
        preset_s = normalize_preset(preset)
        protocol_s = normalize_protocol(protocol)
        self_id_s = _required_text(self_id, "self_id")
        group_id_s = _required_text(group_id, "group_id")
        group_key = _group_subject_key(protocol_s, self_id_s, group_id_s)
        conn = self._connect()
        try:
            with self._transaction(conn):
                partition = self._ensure_persona_partition_in_tx(
                    conn, preset_s, _now_ts()
                )
                rows = conn.execute(
                    f"""
                    SELECT *
                    FROM {partition.groups_table}
                    WHERE group_ref = ?
                    ORDER BY importance DESC, updated_at DESC, memory_id DESC
                    LIMIT ?
                    """,
                    (group_key, max(1, int(limit))),
                ).fetchall()
            records: list[MemoryRecord] = []
            for row in rows:
                evidence_rows = conn.execute(
                    f"""
                    SELECT e.*, i.message_uid
                    FROM {partition.evidence_table} e
                    LEFT JOIN sys_chat_message_index i
                      ON i.conversation_ref = e.conversation_ref
                     AND i.message_key = e.message_key
                    WHERE e.memory_scope = 'group'
                      AND e.memory_id = ?
                    ORDER BY e.observed_at DESC, e.evidence_id DESC
                    """,
                    (int(row["memory_id"]),),
                ).fetchall()
                evidence = tuple(
                    MemoryEvidence(
                        content=str(item["excerpt"]),
                        conversation_pk=(
                            int(item["conversation_ref"])
                            if item["conversation_ref"] is not None
                            else None
                        ),
                        transcript_id=(
                            int(item["message_uid"])
                            if item["message_uid"] is not None
                            else None
                        ),
                        observed_at=int(item["observed_at"]),
                        metadata=json.loads(
                            str(item["metadata_json"]) or "{}"
                        ),
                    )
                    for item in evidence_rows
                )
                records.append(
                    MemoryRecord(
                        fact_id=int(row["memory_id"]),
                        canonical_user_id="",
                        preset=preset_s,
                        content=str(row["memory_text"]),
                        fingerprint=str(row["semantic_hash"]),
                        weight=float(row["importance"]),
                        created_at=int(row["created_at"]),
                        updated_at=int(row["updated_at"]),
                        evidence=evidence,
                        scope="group",
                        subject_id=group_key,
                        canonical_fact=str(row["canonical_fact"]),
                        confidence=float(row["confidence"]),
                        source_count=int(row["source_count"]),
                    )
                )
            return tuple(records)
        finally:
            conn.close()

    def query_group_memories(
        self,
        *,
        preset: Any = "default",
        protocol: Any,
        self_id: Any,
        group_id: Any,
        query: str = "",
        limit: int = 6,
    ) -> tuple[MemoryRecord, ...]:
        candidates = self.list_group_memories(
            preset=preset,
            protocol=protocol,
            self_id=self_id,
            group_id=group_id,
            limit=max(60, int(limit) * 10),
        )
        return self._rank_memories(candidates, query=query, limit=limit)

    def create_group_memory(
        self,
        *,
        preset: Any = "default",
        protocol: Any,
        self_id: Any,
        group_id: Any,
        content: str,
        canonical_user_id: str | None = None,
        weight: float = 1.0,
        canonical_fact: str | None = None,
        importance: float | None = None,
        confidence: float = 1.0,
        honor_deleted: bool = False,
    ) -> MemoryWriteResult | None:
        preset_s = normalize_preset(preset)
        protocol_s = normalize_protocol(protocol)
        self_id_s = _required_text(self_id, "self_id")
        group_id_s = _required_text(group_id, "group_id")
        content_s = _required_text(content, "content")
        canonical_fact_s = str(canonical_fact or content_s).strip()
        fingerprint = memory_fingerprint(canonical_fact_s)
        weight_f = max(
            0.0,
            min(1.0, float(weight if importance is None else importance)),
        )
        group_key = _group_subject_key(protocol_s, self_id_s, group_id_s)
        now = _now_ts()
        conn = self._connect()
        try:
            with self._transaction(conn):
                partition = self._ensure_persona_partition_in_tx(
                    conn, preset_s, now
                )
                writer = None
                if canonical_user_id is not None:
                    writer = self._resolve_canonical_in_tx(
                        conn, canonical_user_id
                    )
                suppressed = conn.execute(
                    f"""
                    SELECT 1
                    FROM {partition.suppressions_table}
                    WHERE scope = 'group'
                      AND subject_ref = ?
                      AND deletion_kind = 'fact'
                      AND semantic_hash = ?
                    LIMIT 1
                    """,
                    (group_key, fingerprint),
                ).fetchone()
                if suppressed is not None and honor_deleted:
                    return None
                if not honor_deleted:
                    conn.execute(
                        f"""
                        DELETE FROM {partition.suppressions_table}
                        WHERE scope = 'group'
                          AND subject_ref = ?
                          AND deletion_kind = 'fact'
                          AND semantic_hash = ?
                        """,
                        (group_key, fingerprint),
                    )
                existing = conn.execute(
                    f"""
                    SELECT memory_id, importance
                    FROM {partition.groups_table}
                    WHERE group_ref = ? AND semantic_hash = ?
                    """,
                    (group_key, fingerprint),
                ).fetchone()
                if existing is None:
                    cursor = conn.execute(
                        f"""
                        INSERT INTO {partition.groups_table}(
                            group_ref,
                            memory_text,
                            importance,
                            confidence,
                            updated_at,
                            canonical_fact,
                            semantic_hash,
                            source_count,
                            persona_revision,
                            first_seen_at,
                            last_confirmed_at,
                            created_at,
                            last_writer_person_id
                        )
                        VALUES(?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?, ?, ?)
                        """,
                        (
                            group_key,
                            content_s,
                            weight_f,
                            max(0.0, min(1.0, float(confidence))),
                            now,
                            canonical_fact_s,
                            fingerprint,
                            now,
                            now,
                            now,
                            writer,
                        ),
                    )
                    memory_id = int(cursor.lastrowid)
                    stored_weight = weight_f
                    outcome = "inserted"
                else:
                    memory_id = int(existing["memory_id"])
                    stored_weight = max(
                        float(existing["importance"]),
                        weight_f,
                    )
                    conn.execute(
                        f"""
                        UPDATE {partition.groups_table}
                        SET memory_text = ?,
                            canonical_fact = ?,
                            importance = ?,
                            confidence = MAX(confidence, ?),
                            last_writer_person_id = COALESCE(
                                ?, last_writer_person_id
                            ),
                            updated_at = ?,
                            last_confirmed_at = ?
                        WHERE memory_id = ?
                        """,
                        (
                            content_s,
                            canonical_fact_s,
                            stored_weight,
                            max(0.0, min(1.0, float(confidence))),
                            writer,
                            now,
                            now,
                            memory_id,
                        ),
                    )
                    outcome = "updated"
            return MemoryWriteResult(
                fact_id=memory_id,
                content=content_s,
                weight=stored_weight,
                outcome=outcome,
                scope="group",
                subject_id=group_key,
            )
        finally:
            conn.close()

    def update_group_memory(
        self,
        *,
        preset: Any = "default",
        protocol: Any,
        self_id: Any,
        group_id: Any,
        memory_id: str | int,
        content: str,
        canonical_user_id: str | None = None,
        weight: float = 1.0,
        canonical_fact: str | None = None,
        importance: float | None = None,
        confidence: float = 1.0,
    ) -> MemoryWriteResult | None:
        try:
            stable_id = int(memory_id)
        except (TypeError, ValueError):
            return None
        if stable_id <= 0:
            return None
        preset_s = normalize_preset(preset)
        protocol_s = normalize_protocol(protocol)
        self_id_s = _required_text(self_id, "self_id")
        group_id_s = _required_text(group_id, "group_id")
        content_s = _required_text(content, "content")
        canonical_fact_s = str(canonical_fact or content_s).strip()
        fingerprint = memory_fingerprint(canonical_fact_s)
        weight_f = max(
            0.0,
            min(1.0, float(weight if importance is None else importance)),
        )
        group_key = _group_subject_key(protocol_s, self_id_s, group_id_s)
        now = _now_ts()
        conn = self._connect()
        try:
            with self._transaction(conn):
                partition = self._ensure_persona_partition_in_tx(
                    conn, preset_s, now
                )
                row = conn.execute(
                    f"""
                    SELECT *
                    FROM {partition.groups_table}
                    WHERE memory_id = ? AND group_ref = ?
                    """,
                    (stable_id, group_key),
                ).fetchone()
                if row is None:
                    return None
                duplicate = conn.execute(
                    f"""
                    SELECT memory_id
                    FROM {partition.groups_table}
                    WHERE group_ref = ?
                      AND semantic_hash = ?
                      AND memory_id != ?
                    LIMIT 1
                    """,
                    (group_key, fingerprint, stable_id),
                ).fetchone()
                if duplicate is not None:
                    raise MemoryConflictError(
                        "another group memory already has the requested content"
                    )
                old_fingerprint = str(row["semantic_hash"])
                old_content = str(row["memory_text"])
                old_weight = float(row["importance"])
                if old_fingerprint != fingerprint:
                    conn.execute(
                        f"""
                        INSERT INTO {partition.suppressions_table}(
                            scope,
                            subject_ref,
                            deletion_kind,
                            semantic_hash,
                            snapshot,
                            source_memory_id,
                            reason,
                            deleted_at
                        )
                        VALUES(
                            'group', ?, 'fact', ?, ?, ?, 'ai_updated', ?
                        )
                        ON CONFLICT(
                            scope,
                            subject_ref,
                            deletion_kind,
                            semantic_hash
                        )
                        DO UPDATE SET
                            snapshot=excluded.snapshot,
                            source_memory_id=excluded.source_memory_id,
                            reason=excluded.reason,
                            deleted_at=excluded.deleted_at
                        """,
                        (
                            group_key,
                            old_fingerprint,
                            old_content,
                            str(stable_id),
                            now,
                        ),
                    )
                conn.execute(
                    f"""
                    DELETE FROM {partition.suppressions_table}
                    WHERE scope = 'group'
                      AND subject_ref = ?
                      AND deletion_kind = 'fact'
                      AND semantic_hash = ?
                    """,
                    (group_key, fingerprint),
                )
                writer = None
                if canonical_user_id is not None:
                    writer = self._resolve_canonical_in_tx(
                        conn, canonical_user_id
                    )
                stored_weight = max(old_weight, weight_f)
                outcome = (
                    "unchanged"
                    if old_content == content_s and stored_weight == old_weight
                    else "updated"
                )
                conn.execute(
                    f"""
                    UPDATE {partition.groups_table}
                    SET semantic_hash = ?,
                        canonical_fact = ?,
                        memory_text = ?,
                        importance = ?,
                        confidence = ?,
                        last_writer_person_id = COALESCE(
                            ?, last_writer_person_id
                        ),
                        updated_at = ?,
                        last_confirmed_at = ?
                    WHERE memory_id = ?
                    """,
                    (
                        fingerprint,
                        canonical_fact_s,
                        content_s,
                        stored_weight,
                        max(0.0, min(1.0, float(confidence))),
                        writer,
                        now,
                        now,
                        stable_id,
                    ),
                )
            return MemoryWriteResult(
                fact_id=stable_id,
                content=content_s,
                weight=stored_weight,
                outcome=outcome,
                scope="group",
                subject_id=group_key,
            )
        finally:
            conn.close()

    def list_scoped_memories(
        self,
        *,
        scope: Any,
        canonical_user_id: str,
        preset: Any = "default",
        protocol: Any | None = None,
        self_id: Any | None = None,
        group_id: Any | None = None,
        limit: int = 100,
    ) -> tuple[MemoryRecord, ...]:
        if _memory_scope(scope) == "person":
            return self.list_memories(
                canonical_user_id=canonical_user_id,
                preset=preset,
                limit=limit,
            )
        return self.list_group_memories(
            preset=preset,
            protocol=protocol,
            self_id=self_id,
            group_id=group_id,
            limit=limit,
        )

    def create_scoped_memory(
        self,
        *,
        scope: Any,
        canonical_user_id: str,
        preset: Any = "default",
        content: str,
        protocol: Any | None = None,
        self_id: Any | None = None,
        group_id: Any | None = None,
        weight: float = 1.0,
        canonical_fact: str | None = None,
        importance: float | None = None,
        confidence: float = 1.0,
        honor_deleted: bool = False,
    ) -> MemoryWriteResult | None:
        if _memory_scope(scope) == "person":
            return self.create_memory(
                canonical_user_id=canonical_user_id,
                preset=preset,
                content=content,
                weight=weight,
                canonical_fact=canonical_fact,
                importance=importance,
                confidence=confidence,
                honor_deleted=honor_deleted,
            )
        return self.create_group_memory(
            preset=preset,
            protocol=protocol,
            self_id=self_id,
            group_id=group_id,
            content=content,
            canonical_user_id=canonical_user_id,
            weight=weight,
            canonical_fact=canonical_fact,
            importance=importance,
            confidence=confidence,
            honor_deleted=honor_deleted,
        )

    def update_scoped_memory(
        self,
        *,
        scope: Any,
        canonical_user_id: str,
        preset: Any = "default",
        memory_id: str | int,
        content: str,
        protocol: Any | None = None,
        self_id: Any | None = None,
        group_id: Any | None = None,
        weight: float = 1.0,
        canonical_fact: str | None = None,
        importance: float | None = None,
        confidence: float = 1.0,
    ) -> MemoryWriteResult | None:
        if _memory_scope(scope) == "person":
            return self.update_memory(
                canonical_user_id=canonical_user_id,
                preset=preset,
                memory_id=memory_id,
                content=content,
                weight=weight,
                canonical_fact=canonical_fact,
                importance=importance,
                confidence=confidence,
            )
        return self.update_group_memory(
            preset=preset,
            protocol=protocol,
            self_id=self_id,
            group_id=group_id,
            memory_id=memory_id,
            content=content,
            canonical_user_id=canonical_user_id,
            weight=weight,
            canonical_fact=canonical_fact,
            importance=importance,
            confidence=confidence,
        )

    def add_scoped_memory_evidence(
        self,
        *,
        scope: Any,
        canonical_user_id: str,
        preset: Any,
        memory_id: str | int,
        protocol: Any,
        self_id: Any,
        conversation_kind: Any,
        conversation_id: Any,
        message_id: Any,
        excerpt: Any,
        observed_at: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        scope_s = _memory_scope(scope)
        try:
            memory_id_i = int(memory_id)
        except (TypeError, ValueError):
            return False
        if memory_id_i <= 0:
            return False
        preset_s = normalize_preset(preset)
        excerpt_s = _required_text(excerpt, "excerpt")[:2000]
        now = _now_ts()
        observed_at_i = int(observed_at if observed_at is not None else now)
        external_message_id = _optional_text(message_id)
        message_key = (
            f"id:{external_message_id}"
            if external_message_id
            else None
        )
        conn = self._connect()
        try:
            with self._transaction(conn):
                canonical = self._resolve_canonical_in_tx(
                    conn, canonical_user_id
                )
                conversation_pk = self._ensure_conversation_in_tx(
                    conn,
                    protocol=protocol,
                    self_id=self_id,
                    conversation_kind=conversation_kind,
                    conversation_id=conversation_id,
                    now=observed_at_i,
                )
                partition = self._ensure_persona_partition_in_tx(
                    conn, preset_s, now
                )
                target_table = (
                    partition.people_table
                    if scope_s == "person"
                    else partition.groups_table
                )
                subject_column = (
                    "person_id" if scope_s == "person" else "group_ref"
                )
                subject_ref = (
                    canonical
                    if scope_s == "person"
                    else _group_subject_key(
                        normalize_protocol(protocol),
                        _required_text(self_id, "self_id"),
                        _required_text(conversation_id, "conversation_id"),
                    )
                )
                exists = conn.execute(
                    f"SELECT 1 FROM {target_table} "
                    f"WHERE memory_id = ? AND {subject_column} = ?",
                    (memory_id_i, subject_ref),
                ).fetchone()
                if exists is None:
                    return False
                evidence_hash = hashlib.sha256(
                    "\x00".join(
                        (
                            scope_s,
                            str(memory_id_i),
                            str(conversation_pk),
                            str(message_key or ""),
                            normalize_memory_text(excerpt_s),
                        )
                    ).encode("utf-8")
                ).hexdigest()
                cursor = conn.execute(
                    f"""
                    INSERT INTO {partition.evidence_table}(
                        memory_scope,
                        memory_id,
                        conversation_ref,
                        message_key,
                        excerpt,
                        observed_at,
                        evidence_hash,
                        metadata_json
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(memory_scope, memory_id, evidence_hash)
                    DO NOTHING
                    """,
                    (
                        scope_s,
                        memory_id_i,
                        conversation_pk,
                        message_key,
                        excerpt_s,
                        observed_at_i,
                        evidence_hash,
                        json.dumps(
                            dict(metadata or {}),
                            ensure_ascii=False,
                            separators=(",", ":"),
                            default=str,
                        ),
                    ),
                )
                conn.execute(
                    f"""
                    UPDATE {target_table}
                    SET source_count = (
                        SELECT COUNT(*)
                        FROM {partition.evidence_table}
                        WHERE memory_scope = ? AND memory_id = ?
                    ),
                        last_confirmed_at = MAX(last_confirmed_at, ?),
                        updated_at = MAX(updated_at, ?)
                    WHERE memory_id = ?
                    """,
                    (
                        scope_s,
                        memory_id_i,
                        observed_at_i,
                        now,
                        memory_id_i,
                    ),
                )
                return cursor.rowcount > 0
        finally:
            conn.close()

    def record_conversation_episode(
        self,
        *,
        preset: Any,
        protocol: Any,
        self_id: Any,
        conversation_kind: Any,
        conversation_id: Any,
        speaker_canonical_id: str,
        exchange_id: Any,
        user_content: Any,
        assistant_content: Any,
        occurred_at: int | None = None,
        queue_review: bool = False,
    ) -> ConversationEpisode:
        preset_s = normalize_preset(preset)
        user_content_s = _required_text(user_content, "user_content")
        assistant_content_s = _required_text(
            assistant_content, "assistant_content"
        )
        now = _now_ts()
        occurred_at_i = int(occurred_at if occurred_at is not None else now)
        exchange_key = _optional_text(exchange_id)
        if not exchange_key:
            seed = "\x00".join(
                (
                    str(speaker_canonical_id),
                    str(occurred_at_i),
                    user_content_s,
                )
            )
            exchange_key = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        conn = self._connect()
        try:
            with self._transaction(conn):
                canonical = self._resolve_canonical_in_tx(
                    conn, speaker_canonical_id
                )
                conversation_pk = self._ensure_conversation_in_tx(
                    conn,
                    protocol=protocol,
                    self_id=self_id,
                    conversation_kind=conversation_kind,
                    conversation_id=conversation_id,
                    now=occurred_at_i,
                )
                partition = self._ensure_persona_partition_in_tx(
                    conn, preset_s, now
                )
                conn.execute(
                    f"""
                    INSERT INTO {partition.episodes_table}(
                        conversation_ref,
                        exchange_key,
                        person_id,
                        user_text,
                        assistant_text,
                        occurred_at,
                        send_state,
                        review_state,
                        created_at,
                        updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, 'sent', ?, ?, ?)
                    ON CONFLICT(conversation_ref, exchange_key)
                    DO UPDATE SET
                        person_id=excluded.person_id,
                        user_text=excluded.user_text,
                        assistant_text=excluded.assistant_text,
                        occurred_at=excluded.occurred_at,
                        send_state='sent',
                        updated_at=excluded.updated_at
                    """,
                    (
                        conversation_pk,
                        exchange_key,
                        canonical,
                        user_content_s,
                        assistant_content_s,
                        occurred_at_i,
                        "pending" if queue_review else "completed",
                        now,
                        now,
                    ),
                )
                row = conn.execute(
                    f"""
                    SELECT e.*, c.protocol, c.self_id,
                           c.conversation_kind, c.conversation_id
                    FROM {partition.episodes_table} e
                    JOIN sys_conversations c
                      ON c.conversation_pk = e.conversation_ref
                    WHERE e.conversation_ref = ? AND e.exchange_key = ?
                    """,
                    (conversation_pk, exchange_key),
                ).fetchone()
                if queue_review:
                    conn.execute(
                        """
                        INSERT INTO job_memory_reviews(
                            persona_id,
                            episode_id,
                            exchange_key,
                            status,
                            attempt_count,
                            next_retry_at,
                            created_at,
                            updated_at
                        )
                        VALUES(?, ?, ?, 'pending', 0, 0, ?, ?)
                        ON CONFLICT(persona_id, exchange_key) DO NOTHING
                        """,
                        (
                            partition.persona_id,
                            int(row["episode_id"]),
                            exchange_key,
                            now,
                            now,
                        ),
                    )
            return self._episode_from_row(row, preset_s)
        finally:
            conn.close()

    @staticmethod
    def _episode_from_row(
        row: sqlite3.Row,
        preset: str,
    ) -> ConversationEpisode:
        return ConversationEpisode(
            episode_id=int(row["episode_id"]),
            preset=preset,
            conversation_pk=int(row["conversation_ref"]),
            protocol=str(row["protocol"]),
            self_id=str(row["self_id"]),
            conversation_kind=str(row["conversation_kind"]),
            conversation_id=str(row["conversation_id"]),
            speaker_canonical_id=str(row["person_id"]),
            user_content=str(row["user_text"]),
            assistant_content=str(row["assistant_text"]),
            occurred_at=int(row["occurred_at"]),
            updated_at=int(row["updated_at"]),
            send_state=str(row["send_state"]),
            review_state=str(row["review_state"]),
            reviewed_at=(
                int(row["reviewed_at"])
                if row["reviewed_at"] is not None
                else None
            ),
            review_error=(
                str(row["review_error"])
                if row["review_error"] is not None
                else None
            ),
        )

    def query_conversation_episodes(
        self,
        *,
        preset: Any,
        protocol: Any,
        self_id: Any,
        conversation_kind: Any,
        conversation_id: Any,
        speaker_canonical_id: str | None = None,
        query: str = "",
        limit: int = 4,
    ) -> tuple[ConversationEpisode, ...]:
        preset_s = normalize_preset(preset)
        protocol_s = normalize_protocol(protocol)
        self_id_s = _required_text(self_id, "self_id")
        kind_s = _required_text(
            conversation_kind, "conversation_kind"
        ).casefold()
        if kind_s not in CONVERSATION_KINDS:
            raise ValueError("conversation_kind must be 'group' or 'private'")
        conversation_id_s = _required_text(
            conversation_id, "conversation_id"
        )
        conn = self._connect()
        try:
            with self._transaction(conn):
                partition = self._ensure_persona_partition_in_tx(
                    conn, preset_s, _now_ts()
                )
                if kind_s == "private" and speaker_canonical_id is not None:
                    canonical = self._resolve_canonical_in_tx(
                        conn, speaker_canonical_id
                    )
                    rows = conn.execute(
                        f"""
                        SELECT e.*, c.protocol, c.self_id,
                               c.conversation_kind, c.conversation_id
                        FROM {partition.episodes_table} e
                        JOIN sys_conversations c
                          ON c.conversation_pk = e.conversation_ref
                        WHERE c.conversation_kind = 'private'
                          AND e.person_id = ?
                        ORDER BY e.occurred_at DESC, e.episode_id DESC
                        LIMIT 80
                        """,
                        (canonical,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"""
                        SELECT e.*, c.protocol, c.self_id,
                               c.conversation_kind, c.conversation_id
                        FROM {partition.episodes_table} e
                        JOIN sys_conversations c
                          ON c.conversation_pk = e.conversation_ref
                        WHERE c.protocol = ?
                          AND c.self_id = ?
                          AND c.conversation_kind = ?
                          AND c.conversation_id = ?
                        ORDER BY e.occurred_at DESC, e.episode_id DESC
                        LIMIT 80
                        """,
                        (
                            protocol_s,
                            self_id_s,
                            kind_s,
                            conversation_id_s,
                        ),
                    ).fetchall()
            episodes = tuple(
                self._episode_from_row(row, preset_s) for row in rows
            )
        finally:
            conn.close()

        query_tokens = set(
            re.findall(
                r"[a-z0-9_]{2,}|[\u4e00-\u9fff]",
                normalize_memory_text(query),
            )
        )
        now = _now_ts()

        def score(item: ConversationEpisode) -> tuple[float, int]:
            content_tokens = set(
                re.findall(
                    r"[a-z0-9_]{2,}|[\u4e00-\u9fff]",
                    normalize_memory_text(
                        f"{item.user_content} {item.assistant_content}"
                    ),
                )
            )
            overlap = (
                len(query_tokens & content_tokens) / max(1, len(query_tokens))
                if query_tokens
                else 0.0
            )
            age_days = max(0.0, (now - item.occurred_at) / 86400.0)
            recency = 1.0 / (1.0 + age_days / 3.0)
            return ((0.65 * recency) + (0.35 * overlap), item.occurred_at)

        selected = sorted(episodes, key=score, reverse=True)[
            : max(1, int(limit))
        ]
        return tuple(sorted(selected, key=lambda item: item.occurred_at))

    def list_pending_memory_reviews(
        self,
        *,
        now: int | None = None,
        limit: int = 20,
    ) -> tuple[dict[str, Any], ...]:
        now_i = int(now if now is not None else _now_ts())
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT j.*, p.preset_key
                FROM job_memory_reviews j
                JOIN sys_personas p ON p.persona_id = j.persona_id
                WHERE j.status IN ('pending', 'failed')
                  AND j.next_retry_at <= ?
                ORDER BY j.created_at, j.review_id
                LIMIT ?
                """,
                (now_i, max(1, min(int(limit), 100))),
            ).fetchall()
            return tuple(dict(row) for row in rows)
        finally:
            conn.close()

    def claim_memory_review(
        self,
        *,
        preset: Any,
        exchange_key: str,
    ) -> ConversationEpisode | None:
        preset_s = normalize_preset(preset)
        exchange_key_s = _required_text(exchange_key, "exchange_key")
        now = _now_ts()
        conn = self._connect()
        try:
            with self._transaction(conn):
                partition = self._ensure_persona_partition_in_tx(
                    conn,
                    preset_s,
                    now,
                )
                cursor = conn.execute(
                    """
                    UPDATE job_memory_reviews
                    SET status='reviewing',
                        attempt_count=attempt_count + 1,
                        updated_at=?
                    WHERE persona_id = ?
                      AND exchange_key = ?
                      AND status IN ('pending', 'failed')
                      AND next_retry_at <= ?
                    """,
                    (
                        now,
                        partition.persona_id,
                        exchange_key_s,
                        now,
                    ),
                )
                if cursor.rowcount <= 0:
                    return None
                conn.execute(
                    f"""
                    UPDATE {partition.episodes_table}
                    SET review_state='reviewing',
                        review_error=NULL,
                        updated_at=?
                    WHERE exchange_key = ?
                    """,
                    (now, exchange_key_s),
                )
                row = conn.execute(
                    f"""
                    SELECT e.*, c.protocol, c.self_id,
                           c.conversation_kind, c.conversation_id
                    FROM {partition.episodes_table} e
                    JOIN sys_conversations c
                      ON c.conversation_pk = e.conversation_ref
                    WHERE e.exchange_key = ?
                    """,
                    (exchange_key_s,),
                ).fetchone()
                if row is None:
                    raise MemoryStoreError(
                        "memory review episode does not exist"
                    )
                return self._episode_from_row(row, preset_s)
        finally:
            conn.close()

    def complete_memory_review(
        self,
        *,
        preset: Any,
        exchange_key: str,
        actions: Sequence[Mapping[str, Any]] = (),
    ) -> bool:
        preset_s = normalize_preset(preset)
        exchange_key_s = _required_text(exchange_key, "exchange_key")
        now = _now_ts()
        conn = self._connect()
        try:
            with self._transaction(conn):
                partition = self._ensure_persona_partition_in_tx(
                    conn,
                    preset_s,
                    now,
                )
                cursor = conn.execute(
                    """
                    UPDATE job_memory_reviews
                    SET status='completed',
                        next_retry_at=0,
                        last_error=NULL,
                        updated_at=?
                    WHERE persona_id = ? AND exchange_key = ?
                      AND status = 'reviewing'
                    """,
                    (now, partition.persona_id, exchange_key_s),
                )
                if cursor.rowcount <= 0:
                    return False
                conn.execute(
                    f"""
                    UPDATE {partition.episodes_table}
                    SET review_state='completed',
                        reviewed_at=?,
                        review_error=NULL,
                        updated_at=?
                    WHERE exchange_key = ?
                    """,
                    (now, now, exchange_key_s),
                )
                action_values = tuple(actions)
                if not action_values:
                    action_values = (
                        {
                            "operation": "no-op",
                            "scope": "person",
                            "status": "completed",
                        },
                    )
                for index, action in enumerate(action_values):
                    operation = str(
                        action.get("operation") or "no-op"
                    ).casefold()
                    scope = _memory_scope(action.get("scope") or "person")
                    conn.execute(
                        """
                        INSERT INTO audit_memory_actions(
                            persona_id,
                            exchange_key,
                            action_index,
                            operation,
                            scope,
                            target_memory_id,
                            semantic_hash,
                            status,
                            error_code,
                            created_at
                        )
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(persona_id, exchange_key, action_index)
                        DO NOTHING
                        """,
                        (
                            partition.persona_id,
                            exchange_key_s,
                            index,
                            operation,
                            scope,
                            (
                                str(action.get("memory_id"))
                                if action.get("memory_id") is not None
                                else None
                            ),
                            action.get("semantic_hash"),
                            str(action.get("status") or "completed"),
                            action.get("error_code"),
                            now,
                        ),
                    )
                return True
        finally:
            conn.close()

    def fail_memory_review(
        self,
        *,
        preset: Any,
        exchange_key: str,
        error: str,
    ) -> bool:
        preset_s = normalize_preset(preset)
        exchange_key_s = _required_text(exchange_key, "exchange_key")
        error_s = str(error or "memory review failed")[:500]
        now = _now_ts()
        conn = self._connect()
        try:
            with self._transaction(conn):
                partition = self._ensure_persona_partition_in_tx(
                    conn,
                    preset_s,
                    now,
                )
                row = conn.execute(
                    """
                    SELECT attempt_count
                    FROM job_memory_reviews
                    WHERE persona_id = ? AND exchange_key = ?
                    """,
                    (partition.persona_id, exchange_key_s),
                ).fetchone()
                if row is None:
                    return False
                delay = min(
                    MAX_GENERATION_RETRY_SECONDS,
                    DEFAULT_GENERATION_RETRY_SECONDS
                    * (2 ** min(max(int(row["attempt_count"]) - 1, 0), 8)),
                )
                conn.execute(
                    """
                    UPDATE job_memory_reviews
                    SET status='failed',
                        next_retry_at=?,
                        last_error=?,
                        updated_at=?
                    WHERE persona_id = ? AND exchange_key = ?
                    """,
                    (
                        now + delay,
                        error_s,
                        now,
                        partition.persona_id,
                        exchange_key_s,
                    ),
                )
                conn.execute(
                    f"""
                    UPDATE {partition.episodes_table}
                    SET review_state='failed',
                        review_error=?,
                        updated_at=?
                    WHERE exchange_key = ?
                    """,
                    (error_s, now, exchange_key_s),
                )
                return True
        finally:
            conn.close()

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
        self._invalidate_barrier_in_tx(conn, canonical_id, preset, now)
        partition = self._ensure_persona_partition_in_tx(
            conn,
            preset,
            now,
        )
        conn.execute(
            f"""
            INSERT INTO {partition.suppressions_table}(
                scope,
                subject_ref,
                deletion_kind,
                semantic_hash,
                snapshot,
                source_memory_id,
                reason,
                deleted_at
            )
            VALUES('person', ?, 'fact', ?, ?, ?, ?, ?)
            ON CONFLICT(
                scope,
                subject_ref,
                deletion_kind,
                semantic_hash
            )
            DO UPDATE SET
                snapshot=excluded.snapshot,
                source_memory_id=excluded.source_memory_id,
                reason=excluded.reason,
                deleted_at=excluded.deleted_at
            """,
            (
                canonical_id,
                fact_fingerprint,
                fact_content,
                source_fact_id,
                reason,
                now,
            ),
        )
        for evidence_fingerprint, evidence_content in evidence:
            conn.execute(
                f"""
                INSERT INTO {partition.suppressions_table}(
                    scope,
                    subject_ref,
                    deletion_kind,
                    semantic_hash,
                    snapshot,
                    source_memory_id,
                    reason,
                    deleted_at
                )
                VALUES('person', ?, 'evidence', ?, ?, ?, ?, ?)
                ON CONFLICT(
                    scope,
                    subject_ref,
                    deletion_kind,
                    semantic_hash
                )
                DO UPDATE SET
                    snapshot=excluded.snapshot,
                    source_memory_id=excluded.source_memory_id,
                    reason=excluded.reason,
                    deleted_at=excluded.deleted_at
                """,
                (
                    canonical_id,
                    evidence_fingerprint,
                    evidence_content,
                    source_fact_id,
                    reason,
                    now,
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
                partition = self._ensure_persona_partition_in_tx(
                    conn,
                    preset_s,
                    now,
                )
                if fact_id is not None:
                    row = conn.execute(
                        f"""
                        SELECT *
                        FROM {partition.people_table}
                        WHERE memory_id = ? AND person_id = ?
                        """,
                        (int(fact_id), canonical),
                    ).fetchone()
                else:
                    fingerprint = memory_fingerprint(content)
                    row = conn.execute(
                        f"""
                        SELECT *
                        FROM {partition.people_table}
                        WHERE person_id = ? AND semantic_hash = ?
                        """,
                        (canonical, fingerprint),
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
                    f"""
                    SELECT evidence_hash, excerpt
                    FROM {partition.evidence_table}
                    WHERE memory_scope = 'person' AND memory_id = ?
                    """,
                    (int(row["memory_id"]),),
                ).fetchall()
                self._suppress_fact_in_tx(
                    conn,
                    canonical_id=canonical,
                    preset=preset_s,
                    fact_fingerprint=str(row["semantic_hash"]),
                    fact_content=str(row["memory_text"]),
                    evidence=tuple(
                        (
                            str(item["evidence_hash"]),
                            str(item["excerpt"]),
                        )
                        for item in evidence_rows
                    ),
                    source_fact_id=str(row["memory_id"]),
                    reason=reason_s,
                    now=now,
                )
                conn.execute(
                    f"""
                    DELETE FROM {partition.evidence_table}
                    WHERE memory_scope = 'person' AND memory_id = ?
                    """,
                    (int(row["memory_id"]),),
                )
                conn.execute(
                    f"DELETE FROM {partition.people_table} WHERE memory_id = ?",
                    (int(row["memory_id"]),),
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
                partition = self._ensure_persona_partition_in_tx(
                    conn,
                    preset_s,
                    _now_ts(),
                )
                cursor = conn.execute(
                    f"""
                    DELETE FROM {partition.suppressions_table}
                    WHERE scope = 'person'
                      AND subject_ref = ?
                      AND deletion_kind IN ({placeholders})
                      AND semantic_hash = ?
                    """,
                    (canonical, *kinds, fingerprint),
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
                    partition = self._ensure_persona_partition_in_tx(
                        conn,
                        preset_s,
                        _now_ts(),
                    )
                    cursor = conn.execute(
                        f"""
                        DELETE FROM {partition.suppressions_table}
                        WHERE scope = 'person'
                          AND subject_ref = ?
                          AND source_memory_id = ?
                        """,
                        (canonical, str(stable_id)),
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
            partition = self._ensure_persona_partition_in_tx(
                conn,
                normalize_preset(preset),
                _now_ts(),
            )
            rows = conn.execute(
                f"""
                SELECT *
                FROM {partition.suppressions_table}
                WHERE scope = 'person' AND subject_ref = ?
                ORDER BY deleted_at DESC, deletion_kind, semantic_hash
                """,
                (canonical,),
            ).fetchall()
            values: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                item["memory_id"] = item.get("source_memory_id")
                item["canonical_id"] = canonical
                item["preset_key"] = normalize_preset(preset)
                item["suppression_kind"] = item.get("deletion_kind")
                item["fingerprint"] = item.get("semantic_hash")
                item["content_snapshot"] = item.get("snapshot")
                item["source_fact_id"] = item.get("source_memory_id")
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
