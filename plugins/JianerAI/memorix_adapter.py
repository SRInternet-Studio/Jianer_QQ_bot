"""JianerAI <-> long-memory compatibility facade.

The JianerAI service predates Jianer Memory and exposes a synchronous, integer
``memory_id`` API.  The long-memory core uses paragraph hashes and an
asynchronous kernel.
This module keeps that boundary in one place:

* The long-memory core owns paragraph text, embeddings, sparse indexes, graph links and
  soft-deletion state.
* A small SQLite ledger only maps Jianer logical IDs to Jianer Memory paragraph
  hashes and records the Jianer scope.  It is deliberately not a second
  retrieval store.
* Non-memory methods are delegated to the legacy store, so transcript,
  identity, generation-barrier and review code can be migrated independently.

The kernel is imported lazily so the Jianer host can construct its service
without doing network or native-vector initialization during module import.
Memory operations remain strict Jianer Memory operations unless the caller
explicitly opts into ``allow_legacy_fallback``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Coroutine, TypeVar

from plugins.JianerAI.memory import (
    ConversationEpisode,
    MemoryConflictError,
    MemoryEvidence,
    MemoryRecord,
    MemoryWriteResult,
)


_LOGGER = logging.getLogger("jianer_ai.memorix_adapter")
_T = TypeVar("_T")


def _now() -> int:
    return int(time.time())


def _text(value: Any) -> str:
    return str(value or "").strip()


def _preset(value: Any) -> str:
    return _text(value) or "default"


def _scope(value: Any) -> str:
    token = _text(value).casefold() or "person"
    if token not in {"person", "group"}:
        raise ValueError("scope must be 'person' or 'group'")
    return token


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_text(value).encode("utf-8")).hexdigest()


def _group_ref(protocol: Any, self_id: Any, group_id: Any) -> str:
    protocol_s = _text(protocol) or "onebot"
    self_s = _text(self_id)
    group_s = _text(group_id)
    if not self_s or not group_s:
        raise ValueError("protocol, self_id and group_id are required")
    return f"group:{protocol_s}:{self_s}:{group_s}"


def _metadata_json(value: Any) -> str:
    try:
        return json.dumps(value if isinstance(value, Mapping) else {}, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return "{}"


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(loaded) if isinstance(loaded, Mapping) else {}
    return {}


class _KernelRunner:
    """Own one event loop for the long-lived Jianer memory kernel."""

    def __init__(self) -> None:
        self._ready = threading.Event()
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(
            target=self._run_loop,
            name="jianer-memorix-kernel",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=10.0)
        if self._loop is None:
            raise RuntimeError("Jianer memory kernel event loop did not start")

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    def call(self, coroutine: Coroutine[Any, Any, _T]) -> _T:
        loop = self._loop
        if self._closed or loop is None:
            coroutine.close()
            raise RuntimeError("Jianer memory kernel runner is closed")
        future: Future[_T] = asyncio.run_coroutine_threadsafe(coroutine, loop)
        return future.result()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        self._thread.join(timeout=5.0)


class JianerMemoryAdapter:
    """Synchronous JianerAI memory API backed by the long-memory core.

    ``legacy_store`` is optional. Methods unrelated to long-term memory are
    transparently delegated to it through ``__getattr__``; long-term memory
    never changes backend unless ``allow_legacy_fallback`` is explicitly true.
    """

    def __init__(
        self,
        *,
        project_root: str | Path | None = None,
        data_dir: str | Path | None = None,
        config: Mapping[str, Any] | None = None,
        legacy_store: Any | None = None,
        kernel: Any | None = None,
        enabled: bool = True,
        allow_legacy_fallback: bool = False,
    ) -> None:
        self.project_root = Path(project_root or Path.cwd()).resolve()
        self.data_dir = Path(data_dir or self.project_root / "data" / "jianer_ai_memorix").resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._ledger_path = self.data_dir / "jianer_adapter.db"
        self._config = dict(config or {})
        self._legacy = legacy_store
        self._kernel = kernel
        self._enabled = bool(enabled)
        # The default backend is a strict Jianer Memory runtime. The legacy store
        # remains attached for transcript/identity compatibility, but memory
        # writes and retrieval never silently change semantic backends.
        self._allow_legacy_fallback = bool(allow_legacy_fallback)
        self._runner: _KernelRunner | None = None
        self._kernel_error = ""
        self._lock = threading.RLock()
        self._initialize_ledger()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self._ledger_path), timeout=30.0, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize_ledger(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    memory_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL CHECK(scope IN ('person', 'group')),
                    preset TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    canonical_user_id TEXT NOT NULL DEFAULT '',
                    protocol TEXT NOT NULL DEFAULT '',
                    self_id TEXT NOT NULL DEFAULT '',
                    group_id TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    canonical_fact TEXT NOT NULL,
                    importance REAL NOT NULL DEFAULT 0.3,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    paragraph_hash TEXT NOT NULL,
                    external_id TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    deleted_at INTEGER,
                    deletion_reason TEXT NOT NULL DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_jianer_active_fact
                    ON memories(scope, preset, subject_id, canonical_fact, deleted_at);
                CREATE INDEX IF NOT EXISTS idx_jianer_scope
                    ON memories(scope, preset, subject_id, deleted_at, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_jianer_hash
                    ON memories(paragraph_hash);
                CREATE TABLE IF NOT EXISTS memory_evidence (
                    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    conversation_pk INTEGER,
                    transcript_id INTEGER,
                    observed_at INTEGER,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    fingerprint TEXT NOT NULL,
                    UNIQUE(memory_id, fingerprint),
                    FOREIGN KEY(memory_id) REFERENCES memories(memory_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS episodes (
                    episode_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    preset TEXT NOT NULL,
                    conversation_pk INTEGER NOT NULL DEFAULT 0,
                    protocol TEXT NOT NULL,
                    self_id TEXT NOT NULL,
                    conversation_kind TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    speaker_canonical_id TEXT NOT NULL,
                    user_content TEXT NOT NULL,
                    assistant_content TEXT NOT NULL,
                    occurred_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    exchange_id TEXT NOT NULL,
                    paragraph_hash TEXT NOT NULL DEFAULT '',
                    send_state TEXT NOT NULL DEFAULT 'sent',
                    review_state TEXT NOT NULL DEFAULT 'completed',
                    reviewed_at INTEGER,
                    review_error TEXT,
                    UNIQUE(preset, protocol, self_id, conversation_kind, conversation_id, exchange_id)
                );
                CREATE INDEX IF NOT EXISTS idx_jianer_episode_scope
                    ON episodes(preset, protocol, self_id, conversation_kind, conversation_id, occurred_at DESC);
                """
            )

    def __getattr__(self, name: str) -> Any:
        legacy = object.__getattribute__(self, "_legacy")
        if legacy is not None:
            try:
                return getattr(legacy, name)
            except AttributeError:
                pass
        raise AttributeError(name)

    @property
    def backend_status(self) -> dict[str, Any]:
        return {
            "backend": "jianer_long_memory" if self._kernel is not None and not self._kernel_error else "jianer_long_memory_unavailable",
            "enabled": self._enabled,
            "data_dir": str(self.data_dir),
            "kernel_error": self._kernel_error,
            "ledger": str(self._ledger_path),
        }

    def memorix_stats(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total, SUM(deleted_at IS NULL) AS active FROM memories"
            ).fetchone()
            episodes = connection.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        return {
            **self.backend_status,
            "total": int(row["total"] or 0),
            "active": int(row["active"] or 0),
            "episodes": int(episodes or 0),
            "vector_backend": "Jianer long-memory kernel",
        }

    def embedding_status(self) -> dict[str, Any]:
        kernel = self._kernel
        if kernel is None:
            return {
                "state": "lazy",
                "configured": True,
                "kernel_initialized": False,
                "backend": "Jianer long-memory kernel",
            }
        status = getattr(kernel, "_runtime_capability_status", None)
        if callable(status):
            try:
                return dict(status())
            except Exception as exc:
                return {"state": "error", "error": str(exc)}
        return {"state": "ready", "backend": "Jianer long-memory kernel"}

    def _runner_or_create(self) -> _KernelRunner:
        if self._runner is None:
            self._runner = _KernelRunner()
        return self._runner

    async def _kernel_instance(self) -> Any:
        if self._kernel is None:
            from plugins.JianerAI.memorix.core.runtime.sdk_memory_kernel import SDKMemoryKernel
            from plugins.JianerAI.memorix.core._host_compat import configure_host_runtime

            kernel_config = dict(self._config)
            storage = dict(kernel_config.get("storage") or {})
            storage.setdefault("data_dir", str(self.data_dir))
            kernel_config["storage"] = storage
            configure_host_runtime(
                project_root=self.project_root,
                memory_store=self._legacy,
                plugin_config=kernel_config,
            )
            self._kernel = SDKMemoryKernel(plugin_root=self.project_root, config=kernel_config)
        if not bool(getattr(self._kernel, "is_runtime_ready", lambda: False)()):
            await self._kernel.initialize()
        return self._kernel

    def _call_kernel(self, operation: Callable[[Any], Coroutine[Any, Any, _T]]) -> _T:
        if not self._enabled:
            raise RuntimeError("Jianer long-memory backend is disabled")
        runner = self._runner_or_create()

        async def invoke() -> _T:
            kernel = await self._kernel_instance()
            return await operation(kernel)

        try:
            return runner.call(invoke())
        except Exception as exc:
            self._kernel_error = f"{type(exc).__name__}: {exc}"
            raise

    def get_person_alias_details(self, canonical_user_id: str) -> Mapping[str, Any]:
        """Resolve the display aliases used by profile and relation views."""

        person_id = _text(canonical_user_id)
        if not person_id:
            return {}

        async def operation(kernel: Any) -> Mapping[str, Any]:
            service = getattr(kernel, "person_profile_service", None)
            getter = getattr(service, "get_person_alias_details", None)
            if not callable(getter):
                return {}
            value = getter(person_id)
            return dict(value) if isinstance(value, Mapping) else {}

        return self._call_kernel(operation)

    def _scope_payload(
        self,
        *,
        scope: str,
        canonical_user_id: str,
        protocol: Any = "",
        self_id: Any = "",
        group_id: Any = "",
    ) -> tuple[str, dict[str, Any]]:
        scope_s = _scope(scope)
        if scope_s == "person":
            canonical = _text(canonical_user_id)
            if not canonical:
                raise ValueError("canonical_user_id is required for person memory")
            return canonical, {
                "jianer_scope": "person",
                "canonical_user_id": canonical,
            }
        ref = _group_ref(protocol, self_id, group_id)
        return ref, {
            "jianer_scope": "group",
            "group_ref": ref,
            "protocol": _text(protocol),
            "self_id": _text(self_id),
            "group_id": _text(group_id),
        }

    def _call_ingest(
        self,
        *,
        external_id: str,
        content: str,
        source_type: str,
        metadata: Mapping[str, Any],
        person_ids: Sequence[str] = (),
        entities: Sequence[str] = (),
        relations: Sequence[Mapping[str, Any]] = (),
        chat_id: str = "",
        group_id: str = "",
        timestamp: int | None = None,
    ) -> str:
        payload = self._call_ingest_payload(
            external_id=external_id,
            content=content,
            source_type=source_type,
            metadata=metadata,
            person_ids=person_ids,
            entities=entities,
            relations=relations,
            chat_id=chat_id,
            group_id=group_id,
            timestamp=timestamp,
        )
        stored = payload.get("stored_ids") if isinstance(payload, Mapping) else None
        if not stored:
            skipped = payload.get("skipped_ids") if isinstance(payload, Mapping) else None
            stored = skipped or []
        paragraph_hash = next((str(item) for item in stored if str(item)), "")
        if not paragraph_hash:
            raise RuntimeError(f"Jianer long-memory ingest returned no paragraph hash: {payload!r}")
        return paragraph_hash

    def _call_ingest_payload(
        self,
        *,
        external_id: str,
        content: str,
        source_type: str,
        metadata: Mapping[str, Any],
        person_ids: Sequence[str] = (),
        entities: Sequence[str] = (),
        relations: Sequence[Mapping[str, Any]] = (),
        chat_id: str = "",
        group_id: str = "",
        timestamp: int | None = None,
    ) -> Mapping[str, Any]:
        async def operation(kernel: Any) -> dict[str, Any]:
            return await kernel.ingest_text(
                external_id=external_id,
                source_type=source_type,
                text=content,
                chat_id=chat_id,
                person_ids=tuple(person_ids),
                participants=(),
                entities=tuple(entities),
                relations=tuple(dict(item) for item in relations if isinstance(item, Mapping)),
                timestamp=float(timestamp or _now()),
                metadata=dict(metadata),
                respect_filter=False,
                user_id=(str(person_ids[0]) if person_ids else ""),
                group_id=group_id if group_id else (chat_id if source_type == "group_fact" else ""),
            )

        payload = self._call_kernel(operation)
        return dict(payload) if isinstance(payload, Mapping) else {}

    def write_memory_review_relations(
        self,
        *,
        preset: Any = "default",
        exchange_key: str,
        source_text: str,
        relations: Sequence[Mapping[str, Any]],
        canonical_user_id: str = "",
        protocol: Any = "",
        self_id: Any = "",
        conversation_kind: str = "private",
        conversation_id: Any = "",
        observed_at: int | None = None,
    ) -> Mapping[str, Any]:
        """Persist relations extracted by the asynchronous memory reviewer.

        The reviewer writes through the same ingest path as imported knowledge so
        metadata, graph edges and optional relation embeddings stay consistent.
        The exchange key and normalized triples make retries idempotent.
        """

        normalized: list[dict[str, Any]] = []
        entities: list[str] = []
        seen_entities: set[str] = set()
        for raw in relations:
            if not isinstance(raw, Mapping):
                continue
            subject = _text(raw.get("subject"))
            predicate = _text(raw.get("predicate"))
            obj = _text(raw.get("object"))
            if not (subject and predicate and obj):
                continue
            try:
                confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.8))))
            except (TypeError, ValueError):
                confidence = 0.8
            relation = {
                "subject": subject,
                "predicate": predicate,
                "object": obj,
                "confidence": confidence,
                "metadata": {
                    "source_type": "memory_review_relation",
                    "relation_origin": "automatic_memory_review",
                },
            }
            normalized.append(relation)
            for entity in (subject, obj):
                if entity.casefold() not in seen_entities:
                    seen_entities.add(entity.casefold())
                    entities.append(entity)
        if not normalized:
            return {"stored_ids": [], "relation_hashes": [], "skipped_ids": []}

        scope = "group" if str(conversation_kind or "").strip().casefold() == "group" else "person"
        subject_id = _text(canonical_user_id) if scope == "person" else _text(conversation_id)
        relation_signature = json.dumps(
            [
                {key: item[key] for key in ("subject", "predicate", "object", "confidence")}
                for item in normalized
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(
            f"{_preset(preset)}\0{exchange_key}\0{relation_signature}".encode("utf-8")
        ).hexdigest()
        metadata = {
            "source_type": "memory_review_relation",
            "relation_origin": "automatic_memory_review",
            "review_exchange_key": _text(exchange_key),
            "preset": _preset(preset),
            "person_id": _text(canonical_user_id) if scope == "person" else "",
            "person_ids": [_text(canonical_user_id)] if scope == "person" and _text(canonical_user_id) else [],
            "relation_scope": scope,
            "protocol": _text(protocol),
            "self_id": _text(self_id),
            "group_id": _text(conversation_id) if scope == "group" else "",
            "evidence_message_ids": [_text(exchange_key)],
        }
        payload = self._call_ingest_payload(
            external_id=f"jianer:auto-relation:{digest}",
            content=_text(source_text),
            source_type="memory_review_relation",
            metadata=metadata,
            person_ids=(_text(canonical_user_id),) if scope == "person" and _text(canonical_user_id) else (),
            entities=entities,
            relations=normalized,
            chat_id=_text(conversation_id),
            group_id=_text(conversation_id) if scope == "group" else "",
            timestamp=observed_at,
        )
        stored = payload.get("stored_ids") if isinstance(payload, Mapping) else []
        relation_hashes = [str(item) for item in (stored or [])[1:] if str(item)]
        return {
            **dict(payload),
            "relation_hashes": relation_hashes,
            "scope": scope,
            "subject_id": subject_id,
        }

    def _fallback_call(self, name: str, **kwargs: Any) -> Any:
        if not self._allow_legacy_fallback:
            raise RuntimeError(
                "Jianer long-memory operation failed; legacy fallback is disabled"
            )
        legacy = self._legacy
        if legacy is None:
            raise RuntimeError("Jianer long-memory backend unavailable and no legacy store configured")
        return getattr(legacy, name)(**kwargs)

    def _row_evidence(self, connection: sqlite3.Connection, memory_id: int) -> tuple[MemoryEvidence, ...]:
        rows = connection.execute(
            "SELECT * FROM memory_evidence WHERE memory_id=? ORDER BY observed_at DESC, evidence_id DESC",
            (memory_id,),
        ).fetchall()
        return tuple(
            MemoryEvidence(
                content=str(row["content"]),
                conversation_pk=(int(row["conversation_pk"]) if row["conversation_pk"] is not None else None),
                transcript_id=(int(row["transcript_id"]) if row["transcript_id"] is not None else None),
                observed_at=(int(row["observed_at"]) if row["observed_at"] is not None else None),
                metadata=_metadata(row["metadata_json"]),
                fingerprint=str(row["fingerprint"]),
            )
            for row in rows
        )

    def _record_from_row(self, connection: sqlite3.Connection, row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            fact_id=int(row["memory_id"]),
            canonical_user_id=str(row["canonical_user_id"]),
            preset=str(row["preset"]),
            content=str(row["content"]),
            fingerprint=_fingerprint(row["canonical_fact"]),
            weight=float(row["importance"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            evidence=self._row_evidence(connection, int(row["memory_id"])),
            scope=str(row["scope"]),
            subject_id=str(row["subject_id"]),
            canonical_fact=str(row["canonical_fact"]),
            confidence=float(row["confidence"]),
            source_count=int(
                connection.execute(
                    "SELECT COUNT(*) FROM memory_evidence WHERE memory_id=?",
                    (int(row["memory_id"]),),
                ).fetchone()[0]
            ),
        )

    def _ledger_rows(
        self,
        *,
        scope: str,
        preset: Any,
        subject_id: str,
        limit: int,
        include_deleted: bool = False,
    ) -> tuple[MemoryRecord, ...]:
        with self._lock, self._connect() as connection:
            deleted_clause = "" if include_deleted else " AND deleted_at IS NULL"
            rows = connection.execute(
                "SELECT * FROM memories WHERE scope=? AND preset=? AND subject_id=?"
                + deleted_clause
                + " ORDER BY importance DESC, updated_at DESC, memory_id DESC LIMIT ?",
                (_scope(scope), _preset(preset), subject_id, max(1, int(limit))),
            ).fetchall()
            return tuple(self._record_from_row(connection, row) for row in rows)

    def list_scoped_memories(self, *, scope: str, canonical_user_id: str, preset: Any = "default", limit: int = 100, **kwargs: Any) -> tuple[MemoryRecord, ...]:
        subject, _ = self._scope_payload(scope=scope, canonical_user_id=canonical_user_id, **kwargs)
        return self._ledger_rows(scope=scope, preset=preset, subject_id=subject, limit=limit)

    def list_memories(self, *, canonical_user_id: str, preset: Any = "default", limit: int = 100) -> tuple[MemoryRecord, ...]:
        records = self.list_scoped_memories(scope="person", canonical_user_id=canonical_user_id, preset=preset, limit=limit)
        if not records and self._allow_legacy_fallback and self._legacy is not None:
            return self._fallback_call("list_memories", canonical_user_id=canonical_user_id, preset=preset, limit=limit)
        return records

    def list_group_memories(self, *, preset: Any = "default", protocol: Any, self_id: Any, group_id: Any, limit: int = 100) -> tuple[MemoryRecord, ...]:
        subject, _ = self._scope_payload(scope="group", canonical_user_id="", protocol=protocol, self_id=self_id, group_id=group_id)
        records = self._ledger_rows(scope="group", preset=preset, subject_id=subject, limit=limit)
        if not records and self._allow_legacy_fallback and self._legacy is not None:
            return self._fallback_call("list_group_memories", preset=preset, protocol=protocol, self_id=self_id, group_id=group_id, limit=limit)
        return records

    def _search_records(self, *, scope: str, preset: Any, subject_id: str, query: str, limit: int) -> tuple[MemoryRecord, ...]:
        candidates = self._ledger_rows(scope=scope, preset=preset, subject_id=subject_id, limit=max(100, limit * 20))
        if not _text(query):
            return candidates[: max(1, int(limit))]
        tokens = {part.casefold() for part in _text(query).split() if part}
        ranked = sorted(
            candidates,
            key=lambda item: (
                len(tokens & {part.casefold() for part in item.content.split()}),
                item.weight,
                item.updated_at,
            ),
            reverse=True,
        )
        return tuple(ranked[: max(1, int(limit))])

    def _semantic_query(self, *, scope: str, preset: Any, subject_id: str, query: str, limit: int, protocol: str = "", self_id: str = "", group_id: str = "") -> tuple[MemoryRecord, ...]:
        if not _text(query):
            return self._search_records(scope=scope, preset=preset, subject_id=subject_id, query=query, limit=limit)

        async def operation(kernel: Any) -> dict[str, Any]:
            from plugins.JianerAI.memorix.core.runtime.models import KernelSearchRequest

            chat_id = subject_id if scope == "group" else ""
            return await kernel.search_memory(
                KernelSearchRequest(
                    query=_text(query),
                    limit=max(1, int(limit) * 3),
                    mode="search",
                    chat_id=chat_id,
                    person_id=subject_id if scope == "person" else "",
                    user_id=subject_id if scope == "person" else "",
                    group_id=chat_id,
                    respect_filter=False,
                )
            )

        try:
            payload = self._call_kernel(operation)
            hits = payload.get("hits", []) if isinstance(payload, Mapping) else []
            hashes = {str(item.get("hash", "")) for item in hits if isinstance(item, Mapping)}
            with self._lock, self._connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM memories WHERE scope=? AND preset=? AND subject_id=? AND deleted_at IS NULL",
                    (_scope(scope), _preset(preset), subject_id),
                ).fetchall()
                by_hash = {str(row["paragraph_hash"]): row for row in rows}
                ordered = [by_hash[token] for token in (str(item.get("hash", "")) for item in hits if isinstance(item, Mapping)) if token in by_hash]
                ordered.extend(row for row in rows if str(row["paragraph_hash"]) in hashes and row not in ordered)
                records = tuple(self._record_from_row(connection, row) for row in ordered[: max(1, int(limit))])
            # An empty vector result is a real empty result.  In strict mode
            # it must not turn into keyword retrieval from the compatibility
            # ledger, otherwise the configured vector backend is bypassed.
            if records or not self._allow_legacy_fallback:
                return records
            return self._search_records(scope=scope, preset=preset, subject_id=subject_id, query=query, limit=limit)
        except Exception as exc:
            self._kernel_error = f"{type(exc).__name__}: {exc}"
            if not self._allow_legacy_fallback:
                raise
            _LOGGER.warning("explicit legacy fallback enabled; semantic query used ledger search: %s", exc)
            if self._legacy is not None:
                try:
                    if scope == "person":
                        return tuple(self._fallback_call("query_memories", canonical_user_id=subject_id, preset=preset, query=query, limit=limit))
                except Exception:
                    pass
            return self._search_records(scope=scope, preset=preset, subject_id=subject_id, query=query, limit=limit)

    def query_memories(self, *, canonical_user_id: str, preset: Any = "default", query: str = "", limit: int = 6) -> tuple[MemoryRecord, ...]:
        return self._semantic_query(scope="person", preset=preset, subject_id=_text(canonical_user_id), query=query, limit=limit)

    def query_group_memories(self, *, preset: Any = "default", protocol: Any, self_id: Any, group_id: Any, query: str = "", limit: int = 6) -> tuple[MemoryRecord, ...]:
        subject, _ = self._scope_payload(scope="group", canonical_user_id="", protocol=protocol, self_id=self_id, group_id=group_id)
        return self._semantic_query(scope="group", preset=preset, subject_id=subject, query=query, limit=limit, protocol=_text(protocol), self_id=_text(self_id), group_id=_text(group_id))

    def _write_memory(self, *, scope: str, preset: Any, subject_id: str, metadata: Mapping[str, Any], canonical_user_id: str, content: str, canonical_fact: str | None, weight: float, confidence: float, protocol: str = "", self_id: str = "", group_id: str = "", origin: str = "direct", honor_deleted: bool = False) -> MemoryWriteResult | None:
        content_s = _text(content)
        if not content_s:
            raise ValueError("content is required")
        preset_s = _preset(preset)
        fact_s = _text(canonical_fact) or content_s
        fp = _fingerprint(fact_s)
        now = _now()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM memories WHERE scope=? AND preset=? AND subject_id=? AND canonical_fact=? AND deleted_at IS NULL LIMIT 1",
                (_scope(scope), preset_s, subject_id, fact_s),
            ).fetchone()
            if existing is not None:
                return MemoryWriteResult(fact_id=int(existing["memory_id"]), content=str(existing["content"]), weight=float(existing["importance"]), outcome="unchanged", scope=_scope(scope), subject_id=subject_id)
            deleted = connection.execute(
                "SELECT * FROM memories WHERE scope=? AND preset=? AND subject_id=? AND canonical_fact=? AND deleted_at IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
                (_scope(scope), preset_s, subject_id, fact_s),
            ).fetchone()
            if deleted is not None and honor_deleted:
                return None

        external_id = f"jianer:{_scope(scope)}:{preset_s}:{subject_id}:{fp}:{now}:{threading.get_ident()}"
        payload = dict(metadata)
        payload.update({"jianer_scope": _scope(scope), "jianer_subject_id": subject_id, "preset": preset_s, "canonical_fact": fact_s, "origin": origin})
        try:
            paragraph_hash = self._call_ingest(
                external_id=external_id,
                content=content_s,
                source_type="person_fact" if _scope(scope) == "person" else "group_fact",
                metadata=payload,
                person_ids=(canonical_user_id,) if _scope(scope) == "person" else (),
                chat_id=subject_id if _scope(scope) == "group" else "",
                timestamp=now,
            )
        except Exception:
            if self._allow_legacy_fallback and self._legacy is not None:
                if _scope(scope) == "person":
                    return self._fallback_call("create_memory", canonical_user_id=canonical_user_id, preset=preset_s, content=content_s, weight=weight, canonical_fact=fact_s, confidence=confidence, origin=origin, honor_deleted=honor_deleted)
                return self._fallback_call("create_group_memory", preset=preset_s, protocol=protocol, self_id=self_id, group_id=group_id, content=content_s, canonical_user_id=(canonical_user_id or None), weight=weight, canonical_fact=fact_s, confidence=confidence, honor_deleted=honor_deleted)
            raise

        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO memories(scope,preset,subject_id,canonical_user_id,protocol,self_id,group_id,content,canonical_fact,importance,confidence,paragraph_hash,external_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_scope(scope), preset_s, subject_id, _text(canonical_user_id) if _scope(scope) == "person" else "", protocol, self_id, group_id, content_s, fact_s, max(0.0, min(1.0, float(weight))), max(0.0, min(1.0, float(confidence))), paragraph_hash, external_id, now, now),
            )
            memory_id = int(cursor.lastrowid)
        return MemoryWriteResult(fact_id=memory_id, content=content_s, weight=max(0.0, min(1.0, float(weight))), outcome="created", scope=_scope(scope), subject_id=subject_id)

    def create_scoped_memory(self, *, scope: str, canonical_user_id: str, preset: Any = "default", content: str, canonical_fact: str | None = None, importance: float = 1.0, confidence: float = 1.0, protocol: Any = "", self_id: Any = "", group_id: Any = "", origin: str = "direct", honor_deleted: bool = False, **kwargs: Any) -> MemoryWriteResult | None:
        subject, payload = self._scope_payload(scope=scope, canonical_user_id=canonical_user_id, protocol=protocol, self_id=self_id, group_id=group_id)
        return self._write_memory(scope=scope, preset=preset, subject_id=subject, metadata=payload, canonical_user_id=canonical_user_id, content=content, canonical_fact=canonical_fact, weight=importance, confidence=confidence, protocol=_text(protocol), self_id=_text(self_id), group_id=_text(group_id), origin=origin, honor_deleted=honor_deleted)

    def create_memory(self, *, canonical_user_id: str, preset: Any = "default", content: str, weight: float = 1.0, canonical_fact: str | None = None, importance: float | None = None, confidence: float = 1.0, origin: str = "direct", honor_deleted: bool = False) -> MemoryWriteResult | None:
        return self.create_scoped_memory(scope="person", canonical_user_id=canonical_user_id, preset=preset, content=content, canonical_fact=canonical_fact, importance=float(weight if importance is None else importance), confidence=confidence, origin=origin, honor_deleted=honor_deleted)

    def create_group_memory(self, *, preset: Any = "default", protocol: Any, self_id: Any, group_id: Any, content: str, canonical_user_id: str | None = None, weight: float = 1.0, canonical_fact: str | None = None, importance: float | None = None, confidence: float = 1.0, honor_deleted: bool = False) -> MemoryWriteResult | None:
        return self.create_scoped_memory(scope="group", canonical_user_id=canonical_user_id or "", preset=preset, content=content, canonical_fact=canonical_fact, importance=float(weight if importance is None else importance), confidence=confidence, protocol=protocol, self_id=self_id, group_id=group_id, honor_deleted=honor_deleted)

    def _lookup_row(self, *, scope: str, canonical_user_id: str, preset: Any, memory_id: str | int, protocol: Any = "", self_id: Any = "", group_id: Any = "") -> sqlite3.Row | None:
        subject, _ = self._scope_payload(scope=scope, canonical_user_id=canonical_user_id, protocol=protocol, self_id=self_id, group_id=group_id)
        try:
            stable_id = int(str(memory_id))
        except (TypeError, ValueError):
            return None
        with self._lock, self._connect() as connection:
            return connection.execute("SELECT * FROM memories WHERE memory_id=? AND scope=? AND preset=? AND subject_id=? LIMIT 1", (stable_id, _scope(scope), _preset(preset), subject)).fetchone()

    def update_scoped_memory(self, *, scope: str, canonical_user_id: str, preset: Any = "default", memory_id: str | int, content: str, canonical_fact: str | None = None, importance: float = 1.0, confidence: float = 1.0, protocol: Any = "", self_id: Any = "", group_id: Any = "", **kwargs: Any) -> MemoryWriteResult | None:
        row = self._lookup_row(scope=scope, canonical_user_id=canonical_user_id, preset=preset, memory_id=memory_id, protocol=protocol, self_id=self_id, group_id=group_id)
        if row is None or row["deleted_at"] is not None:
            return None
        fact_s = _text(canonical_fact) or _text(content)
        with self._lock, self._connect() as connection:
            duplicate = connection.execute("SELECT 1 FROM memories WHERE scope=? AND preset=? AND subject_id=? AND canonical_fact=? AND memory_id!=? AND deleted_at IS NULL", (_scope(scope), _preset(preset), str(row["subject_id"]), fact_s, int(row["memory_id"]))).fetchone()
            if duplicate is not None:
                raise MemoryConflictError("another memory already has the requested content")
        old_hash = str(row["paragraph_hash"])
        try:
            self._call_kernel(lambda kernel: self._delete_paragraph(kernel, old_hash, "user_updated"))
            new_hash = self._call_ingest(external_id=f"jianer:update:{int(row['memory_id'])}:{_now()}", content=_text(content), source_type="person_fact" if _scope(scope) == "person" else "group_fact", metadata={"jianer_scope": _scope(scope), "jianer_subject_id": str(row["subject_id"]), "canonical_fact": fact_s}, person_ids=(canonical_user_id,) if _scope(scope) == "person" else (), chat_id=str(row["subject_id"]) if _scope(scope) == "group" else "", timestamp=_now())
        except Exception:
            if self._allow_legacy_fallback and self._legacy is not None and _scope(scope) == "person":
                return self._fallback_call("update_memory", canonical_user_id=canonical_user_id, preset=preset, memory_id=memory_id, content=content, weight=importance, canonical_fact=canonical_fact, confidence=confidence)
            if self._allow_legacy_fallback and self._legacy is not None:
                return self._fallback_call("update_group_memory", preset=preset, protocol=protocol, self_id=self_id, group_id=group_id, memory_id=memory_id, content=content, weight=importance, canonical_fact=canonical_fact, confidence=confidence)
            raise
        now = _now()
        with self._lock, self._connect() as connection:
            connection.execute("UPDATE memories SET content=?, canonical_fact=?, importance=?, confidence=?, paragraph_hash=?, updated_at=? WHERE memory_id=?", (_text(content), fact_s, max(0.0, min(1.0, float(importance))), max(0.0, min(1.0, float(confidence))), new_hash, now, int(row["memory_id"])))
        return MemoryWriteResult(fact_id=int(row["memory_id"]), content=_text(content), weight=max(0.0, min(1.0, float(importance))), outcome="updated", scope=_scope(scope), subject_id=str(row["subject_id"]))

    def update_memory(self, *, canonical_user_id: str, preset: Any = "default", memory_id: str | int, content: str, weight: float = 1.0, canonical_fact: str | None = None, importance: float | None = None, confidence: float = 1.0) -> MemoryWriteResult | None:
        return self.update_scoped_memory(scope="person", canonical_user_id=canonical_user_id, preset=preset, memory_id=memory_id, content=content, weight=weight, importance=float(weight if importance is None else importance), canonical_fact=canonical_fact, confidence=confidence)

    def update_group_memory(self, *, preset: Any = "default", protocol: Any, self_id: Any, group_id: Any, memory_id: str | int, content: str, weight: float = 1.0, canonical_fact: str | None = None, importance: float | None = None, confidence: float = 1.0) -> MemoryWriteResult | None:
        return self.update_scoped_memory(scope="group", canonical_user_id="", preset=preset, memory_id=memory_id, content=content, importance=float(weight if importance is None else importance), canonical_fact=canonical_fact, confidence=confidence, protocol=protocol, self_id=self_id, group_id=group_id)

    async def _delete_paragraph(self, kernel: Any, paragraph_hash: str, reason: str) -> None:
        metadata = getattr(kernel, "metadata_store", None)
        if metadata is None:
            raise RuntimeError("Jianer long-memory metadata store is unavailable")
        metadata.mark_as_deleted([paragraph_hash], "paragraph", reason=reason)
        persist = getattr(kernel, "_save_vector_store", None)
        if callable(persist):
            for store in (getattr(kernel, "paragraph_vector_store", None), getattr(kernel, "vector_store", None)):
                if store is not None:
                    try:
                        store.delete([paragraph_hash])
                        persist(store)
                    except Exception:
                        _LOGGER.debug("vector delete failed for %s", paragraph_hash, exc_info=True)

    def delete_memory(self, *, canonical_user_id: str, preset: Any = "default", fact_id: int | None = None, memory_id: str | int | None = None, content: str | None = None, reason: str = "user_deleted") -> bool:
        target = fact_id if fact_id is not None else memory_id
        row = None
        if target is not None:
            row = self._lookup_row(scope="person", canonical_user_id=canonical_user_id, preset=preset, memory_id=target)
        if row is None and _text(content):
            with self._lock, self._connect() as connection:
                row = connection.execute("SELECT * FROM memories WHERE scope='person' AND preset=? AND subject_id=? AND canonical_fact=? AND deleted_at IS NULL LIMIT 1", (_preset(preset), _text(canonical_user_id), _text(content))).fetchone()
        if row is None or row["deleted_at"] is not None:
            if self._allow_legacy_fallback and self._legacy is not None:
                return bool(self._fallback_call("delete_memory", canonical_user_id=canonical_user_id, preset=preset, fact_id=target, content=content, reason=reason))
            return False
        try:
            self._call_kernel(lambda kernel: self._delete_paragraph(kernel, str(row["paragraph_hash"]), reason))
        except Exception:
            if self._allow_legacy_fallback and self._legacy is not None:
                return bool(self._fallback_call("delete_memory", canonical_user_id=canonical_user_id, preset=preset, fact_id=int(row["memory_id"]), reason=reason))
            raise
        with self._lock, self._connect() as connection:
            connection.execute("UPDATE memories SET deleted_at=?, deletion_reason=?, updated_at=? WHERE memory_id=?", (_now(), _text(reason) or "user_deleted", _now(), int(row["memory_id"])))
        return True

    def clear_memories(self, *, canonical_user_id: str, preset: Any = "default", reason: str = "user_cleared") -> int:
        rows = self.list_memories(canonical_user_id=canonical_user_id, preset=preset, limit=1_000_000)
        return sum(1 for row in rows if self.delete_memory(canonical_user_id=canonical_user_id, preset=preset, fact_id=row.fact_id, reason=reason))

    def restore_memory(self, *, canonical_user_id: str, preset: Any = "default", memory_id: str | int | None = None, fact_id: str | int | None = None, content: str | None = None, include_evidence: bool = False) -> bool:
        target = fact_id if fact_id is not None else memory_id
        row = self._lookup_row(scope="person", canonical_user_id=canonical_user_id, preset=preset, memory_id=target) if target is not None else None
        if row is None and _text(content):
            with self._lock, self._connect() as connection:
                row = connection.execute("SELECT * FROM memories WHERE scope='person' AND preset=? AND subject_id=? AND canonical_fact=? ORDER BY updated_at DESC LIMIT 1", (_preset(preset), _text(canonical_user_id), _text(content))).fetchone()
        if row is None or row["deleted_at"] is None:
            if self._allow_legacy_fallback and self._legacy is not None:
                return bool(self._fallback_call("restore_memory", canonical_user_id=canonical_user_id, preset=preset, memory_id=target, content=content))
            return False
        async def operation(kernel: Any) -> bool:
            metadata = getattr(kernel, "metadata_store", None)
            if metadata is None:
                return False
            return bool(metadata.restore_paragraph_by_hash(str(row["paragraph_hash"])))
        try:
            restored = self._call_kernel(operation)
        except Exception:
            restored = False
        if not restored:
            return False
        with self._lock, self._connect() as connection:
            connection.execute("UPDATE memories SET deleted_at=NULL, deletion_reason='', updated_at=? WHERE memory_id=?", (_now(), int(row["memory_id"])))
        return True

    def add_scoped_memory_evidence(self, *, scope: str, canonical_user_id: str, preset: Any = "default", memory_id: str | int, excerpt: str, conversation_pk: int | None = None, transcript_id: int | None = None, observed_at: int | None = None, metadata: Mapping[str, Any] | None = None, **kwargs: Any) -> bool:
        # Agent tools include transport/session fields for the legacy store.
        # They belong in evidence metadata and must not leak into the row
        # lookup, whose scope identity only needs the group coordinates.
        lookup_kwargs = {
            name: kwargs[name]
            for name in ("protocol", "self_id", "group_id")
            if name in kwargs
        }
        row = self._lookup_row(scope=scope, canonical_user_id=canonical_user_id, preset=preset, memory_id=memory_id, **lookup_kwargs)
        if row is None:
            return False
        content = _text(excerpt)
        if not content:
            return False
        evidence_metadata = dict(metadata or {})
        for name in ("conversation_kind", "conversation_id", "message_id"):
            if name in kwargs:
                evidence_metadata[name] = kwargs[name]
        with self._lock, self._connect() as connection:
            connection.execute("INSERT OR IGNORE INTO memory_evidence(memory_id,content,conversation_pk,transcript_id,observed_at,metadata_json,fingerprint) VALUES(?,?,?,?,?,?,?)", (int(row["memory_id"]), content, conversation_pk, transcript_id, observed_at or _now(), _metadata_json(evidence_metadata), _fingerprint(content)))
            connection.execute("UPDATE memories SET updated_at=? WHERE memory_id=?", (_now(), int(row["memory_id"])))
        return True

    def record_conversation_episode(self, *, preset: Any, protocol: Any, self_id: Any, conversation_kind: Any, conversation_id: Any, speaker_canonical_id: str, exchange_id: Any, user_content: Any, assistant_content: Any, occurred_at: int | None = None, queue_review: bool = False) -> ConversationEpisode:
        preset_s = _preset(preset)
        protocol_s, self_s, kind_s, conversation_s = _text(protocol), _text(self_id), _text(conversation_kind), _text(conversation_id)
        exchange_s = _text(exchange_id) or _fingerprint(f"{speaker_canonical_id}\0{occurred_at}\0{user_content}")
        occurred = int(occurred_at or _now())
        existing = None
        with self._lock, self._connect() as connection:
            existing = connection.execute("SELECT * FROM episodes WHERE preset=? AND protocol=? AND self_id=? AND conversation_kind=? AND conversation_id=? AND exchange_id=?", (preset_s, protocol_s, self_s, kind_s, conversation_s, exchange_s)).fetchone()
        paragraph_hash = ""
        try:
            paragraph_hash = self._call_ingest(external_id=f"jianer:episode:{preset_s}:{protocol_s}:{self_s}:{kind_s}:{conversation_s}:{exchange_s}", content=f"用户：{_text(user_content)}\n我：{_text(assistant_content)}", source_type="chat_episode", metadata={"jianer_scope": "episode", "chat_id": conversation_s, "conversation_kind": kind_s, "speaker_canonical_id": _text(speaker_canonical_id)}, chat_id=conversation_s, timestamp=occurred)
        except Exception:
            if not self._allow_legacy_fallback:
                raise
            _LOGGER.debug("legacy episode fallback enabled; vector write unavailable", exc_info=True)
        now = _now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute("INSERT INTO episodes(preset,conversation_pk,protocol,self_id,conversation_kind,conversation_id,speaker_canonical_id,user_content,assistant_content,occurred_at,updated_at,exchange_id,paragraph_hash,send_state,review_state,reviewed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(preset,protocol,self_id,conversation_kind,conversation_id,exchange_id) DO UPDATE SET user_content=excluded.user_content,assistant_content=excluded.assistant_content,occurred_at=excluded.occurred_at,updated_at=excluded.updated_at,paragraph_hash=COALESCE(NULLIF(excluded.paragraph_hash,''),episodes.paragraph_hash)", (preset_s, 0, protocol_s, self_s, kind_s, conversation_s, _text(speaker_canonical_id), _text(user_content), _text(assistant_content), occurred, now, exchange_s, paragraph_hash, "sent", "pending" if queue_review else "completed", None if queue_review else now))
            episode_id = int(cursor.lastrowid or (existing["episode_id"] if existing is not None else 0))
            row = connection.execute("SELECT * FROM episodes WHERE episode_id=?", (episode_id,)).fetchone()
        return self._episode_from_row(row, preset_s)

    @staticmethod
    def _episode_from_row(row: sqlite3.Row, preset: str) -> ConversationEpisode:
        return ConversationEpisode(episode_id=int(row["episode_id"]), preset=preset, conversation_pk=int(row["conversation_pk"]), protocol=str(row["protocol"]), self_id=str(row["self_id"]), conversation_kind=str(row["conversation_kind"]), conversation_id=str(row["conversation_id"]), speaker_canonical_id=str(row["speaker_canonical_id"]), user_content=str(row["user_content"]), assistant_content=str(row["assistant_content"]), occurred_at=int(row["occurred_at"]), updated_at=int(row["updated_at"]), send_state=str(row["send_state"]), review_state=str(row["review_state"]), reviewed_at=(int(row["reviewed_at"]) if row["reviewed_at"] is not None else None), review_error=(str(row["review_error"]) if row["review_error"] is not None else None))

    def query_conversation_episodes(self, *, preset: Any, protocol: Any, self_id: Any, conversation_kind: Any, conversation_id: Any, speaker_canonical_id: str | None = None, query: str = "", limit: int = 4) -> tuple[ConversationEpisode, ...]:
        clauses = ["preset=?", "protocol=?", "self_id=?", "conversation_kind=?", "conversation_id=?"]
        params: list[Any] = [_preset(preset), _text(protocol), _text(self_id), _text(conversation_kind), _text(conversation_id)]
        if speaker_canonical_id:
            clauses.append("speaker_canonical_id=?")
            params.append(_text(speaker_canonical_id))
        if _text(query):
            clauses.append("(user_content LIKE ? OR assistant_content LIKE ?)")
            params.extend([f"%{_text(query)}%", f"%{_text(query)}%"])
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT * FROM episodes WHERE " + " AND ".join(clauses) + " ORDER BY occurred_at DESC, episode_id DESC LIMIT ?", (*params, max(1, int(limit)))).fetchall()
            return tuple(self._episode_from_row(row, _preset(preset)) for row in rows)

    def close(self) -> None:
        if self._runner is not None and self._kernel is not None:
            async def shutdown(kernel: Any) -> None:
                method = getattr(kernel, "shutdown", None)
                if callable(method):
                    await method()
            try:
                self._call_kernel(shutdown)
            except Exception:
                _LOGGER.debug("Jianer long-memory shutdown failed", exc_info=True)
        if self._runner is not None:
            self._runner.close()
            self._runner = None

    shutdown = close


__all__ = ["JianerMemoryAdapter"]
