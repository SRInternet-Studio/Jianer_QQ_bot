"""Jianer runtime bridge for the Jianer Memory memory model.

The upstream Jianer Memory package is kept under ``memorix/`` for the complete
storage, graph, retrieval, lifecycle and administration implementation.  This
module adapts the existing Jianer service contract to that model so the bot can
run during migration: identities, transcripts, evidence and generation
barriers remain owned by ``JianerMemoryStore`` while memory paragraphs and
episodes are indexed in a separate vector database.

The vector database uses SQLite for durable metadata and FAISS when installed.
The small pure-Python cosine path is intentional: it keeps first boot and
metadata-only imports usable before optional native wheels are installed.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from plugins.JianerAI.memory import (
    ConversationEpisode,
    GeneratedMemory,
    JianerMemoryStore,
    MemoryRecord,
    MemoryWriteResult,
    _group_subject_key,
    memory_fingerprint,
    normalize_memory_text,
    normalize_preset,
    normalize_protocol,
)


def _now() -> int:
    return int(time.time())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _vector_blob(values: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *(float(item) for item in values))


def _vector_unblob(payload: bytes, dimension: int) -> tuple[float, ...]:
    if not payload or dimension <= 0 or len(payload) != dimension * 4:
        return ()
    return tuple(struct.unpack(f"<{dimension}f", payload))


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    return dot / max(left_norm * right_norm, 1e-12)


@dataclass(frozen=True, slots=True)
class EmbeddingConfig:
    provider: str = "openai"
    model: str = "text-embedding-3-small"
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    dimension: int = 1536
    timeout_seconds: float = 30.0
    max_retries: int = 3
    cache_enabled: bool = True
    fallback_enabled: bool = True

    @classmethod
    def from_file(cls, path: Path) -> "EmbeddingConfig":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            raw = {}
        if not isinstance(raw, Mapping):
            raw = {}
        return cls(
            provider=str(raw.get("Provider", raw.get("provider", "openai"))).strip().lower(),
            model=str(raw.get("Model", raw.get("model", cls.model))).strip(),
            api_key=str(raw.get("ApiKey", raw.get("api_key", ""))).strip(),
            base_url=str(raw.get("BaseUrl", raw.get("base_url", cls.base_url))).strip().rstrip("/"),
            dimension=max(8, int(raw.get("Dimension", raw.get("dimension", cls.dimension)))),
            timeout_seconds=max(1.0, float(raw.get("TimeoutSeconds", raw.get("timeout_seconds", cls.timeout_seconds)))),
            max_retries=max(0, min(8, int(raw.get("MaxRetries", raw.get("max_retries", cls.max_retries))))),
            cache_enabled=bool(raw.get("CacheEnabled", raw.get("cache_enabled", cls.cache_enabled))),
            fallback_enabled=bool(raw.get("FallbackEnabled", raw.get("fallback_enabled", cls.fallback_enabled))),
        )


class EmbeddingProvider:
    """Independent OpenAI-compatible/Gemini embedding client with fallback."""

    def __init__(self, config: EmbeddingConfig):
        self.config = config
        self._cache: dict[str, tuple[float, ...]] = {}
        self._degraded = False
        self._last_error = ""
        self._last_success_at: float | None = None

    @property
    def fingerprint(self) -> str:
        raw = f"{self.config.provider}:{self.config.base_url}:{self.config.model}:{self.dimension}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @property
    def dimension(self) -> int:
        return self.config.dimension

    @property
    def status(self) -> dict[str, Any]:
        return {
            "provider": self.config.provider,
            "model": self.config.model,
            "dimension": self.dimension,
            "fingerprint": self.fingerprint,
            "degraded": self._degraded,
            "last_error": self._last_error,
            "last_success_at": self._last_success_at,
            "cache_size": len(self._cache),
        }

    def _fallback(self, text: str) -> tuple[float, ...]:
        # Stable feature hashing keeps metadata-only deployments searchable.
        values = [0.0] * self.dimension
        tokens = normalize_memory_text(text).split()
        if not tokens:
            tokens = [normalize_memory_text(text)]
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
            for offset in range(0, len(digest), 4):
                index = int.from_bytes(digest[offset : offset + 4], "little") % self.dimension
                values[index] += 1.0 if digest[offset] & 1 else -1.0
        norm = math.sqrt(sum(item * item for item in values)) or 1.0
        return tuple(item / norm for item in values)

    def _request_json(self, url: str, payload: Mapping[str, Any], headers: Mapping[str, str]) -> Any:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
        with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    def _remote(self, text: str) -> tuple[float, ...]:
        provider = self.config.provider
        if not self.config.api_key:
            raise RuntimeError("embedding API key is not configured")
        if provider in {"gemini", "google", "google-genai"}:
            url = f"{self.config.base_url}/v1beta/models/{self.config.model}:embedContent"
            payload = {"model": f"models/{self.config.model}", "content": {"parts": [{"text": text}]}}
            data = self._request_json(url, payload, {"Content-Type": "application/json", "x-goog-api-key": self.config.api_key})
            values = data.get("embedding", {}).get("values", [])
        else:
            url = f"{self.config.base_url}/embeddings"
            data = self._request_json(
                url,
                {"model": self.config.model, "input": text},
                {"Content-Type": "application/json", "Authorization": f"Bearer {self.config.api_key}"},
            )
            values = (data.get("data") or [{}])[0].get("embedding", [])
        vector = tuple(float(item) for item in values)
        if not vector or any(not math.isfinite(item) for item in vector):
            raise RuntimeError("embedding endpoint returned an invalid vector")
        if len(vector) != self.dimension:
            raise RuntimeError(f"embedding dimension mismatch: configured={self.dimension}, received={len(vector)}")
        norm = math.sqrt(sum(item * item for item in vector)) or 1.0
        return tuple(item / norm for item in vector)

    def encode(self, text: str) -> tuple[float, ...]:
        normalized = str(text or "").strip()
        cache_key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if self.config.cache_enabled and cache_key in self._cache:
            return self._cache[cache_key]
        vector: tuple[float, ...]
        try:
            vector = self._remote(normalized)
            self._degraded = False
            self._last_error = ""
            self._last_success_at = time.time()
        except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
            self._degraded = True
            self._last_error = str(exc)[:500]
            if not self.config.fallback_enabled:
                raise
            vector = self._fallback(normalized)
        if self.config.cache_enabled:
            self._cache[cache_key] = vector
        return vector

    def encode_batch(self, texts: Iterable[str]) -> list[tuple[float, ...]]:
        return [self.encode(text) for text in texts]


class VectorMemoryDatabase:
    """Durable paragraph and relation pool with metadata-first writes."""

    def __init__(self, path: Path, embedding: EmbeddingProvider):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.embedding = embedding
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memorix_paragraphs (
                id INTEGER PRIMARY KEY,
                external_id INTEGER NOT NULL,
                object_key TEXT NOT NULL UNIQUE,
                scope TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                canonical_user_id TEXT NOT NULL DEFAULT '',
                preset TEXT NOT NULL,
                content TEXT NOT NULL,
                canonical_fact TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 0.5,
                confidence REAL NOT NULL DEFAULT 1.0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                deleted_at INTEGER,
                vector BLOB,
                dimension INTEGER NOT NULL DEFAULT 0,
                embedding_model TEXT NOT NULL DEFAULT '',
                embedding_fingerprint TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT 'memory',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(scope, subject_id, preset, fingerprint)
            );
            CREATE INDEX IF NOT EXISTS idx_memorix_scope ON memorix_paragraphs(scope, subject_id, preset, deleted_at);
            CREATE INDEX IF NOT EXISTS idx_memorix_fingerprint ON memorix_paragraphs(fingerprint);
            CREATE TABLE IF NOT EXISTS memorix_audit (
                audit_id INTEGER PRIMARY KEY,
                operation TEXT NOT NULL,
                object_id INTEGER,
                payload_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def _audit(self, operation: str, object_id: int | None, payload: Mapping[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO memorix_audit(operation, object_id, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (operation, object_id, _json(payload), _now()),
        )

    def upsert(
        self,
        *,
        object_id: int,
        scope: str,
        subject_id: str,
        canonical_user_id: str,
        preset: str,
        content: str,
        canonical_fact: str | None,
        weight: float,
        confidence: float,
        source_type: str = "memory",
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        now = _now()
        object_key = f"{scope}:{subject_id}:{preset}:{int(object_id)}"
        storage_id = int.from_bytes(hashlib.sha256(object_key.encode("utf-8")).digest()[:8], "big", signed=False) & 0x7FFFFFFFFFFFFFFF
        fact = str(canonical_fact or content).strip()
        fingerprint = memory_fingerprint(fact)
        vector: tuple[float, ...] | None = None
        try:
            vector = self.embedding.encode(content)
        except Exception:
            vector = None
        row = self._conn.execute("SELECT id, created_at FROM memorix_paragraphs WHERE object_key = ?", (object_key,)).fetchone()
        if row is None:
            self._conn.execute(
                """INSERT INTO memorix_paragraphs
                (id, external_id, object_key, scope, subject_id, canonical_user_id, preset, content, canonical_fact, fingerprint,
                 weight, confidence, created_at, updated_at, vector, dimension, embedding_model,
                 embedding_fingerprint, source_type, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (storage_id, int(object_id), object_key, scope, subject_id, canonical_user_id, preset, content, fact, fingerprint,
                 max(0.0, min(1.0, float(weight))), max(0.0, min(1.0, float(confidence))), now, now,
                 _vector_blob(vector) if vector else None, len(vector) if vector else 0,
                 self.embedding.config.model, self.embedding.fingerprint, source_type, _json(metadata or {})),
            )
        else:
            self._conn.execute(
                """UPDATE memorix_paragraphs SET scope=?, subject_id=?, canonical_user_id=?, preset=?,
                content=?, canonical_fact=?, fingerprint=?, weight=?, confidence=?, updated_at=?, deleted_at=NULL,
                vector=?, dimension=?, embedding_model=?, embedding_fingerprint=?, source_type=?, metadata_json=? WHERE id=?""",
                (scope, subject_id, canonical_user_id, preset, content, fact, fingerprint,
                 max(0.0, min(1.0, float(weight))), max(0.0, min(1.0, float(confidence))), now,
                 _vector_blob(vector) if vector else None, len(vector) if vector else 0,
                 self.embedding.config.model, self.embedding.fingerprint, source_type, _json(metadata or {}), storage_id),
            )
        self._audit("upsert", object_id, {"scope": scope, "subject_id": subject_id, "source_type": source_type})
        self._conn.commit()

    def _record(self, row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            fact_id=int(row["external_id"]), canonical_user_id=str(row["canonical_user_id"]), preset=str(row["preset"]),
            content=str(row["content"]), fingerprint=str(row["fingerprint"]), weight=float(row["weight"]),
            created_at=int(row["created_at"]), updated_at=int(row["updated_at"]), evidence=(),
            scope=str(row["scope"]), subject_id=str(row["subject_id"]), canonical_fact=str(row["canonical_fact"]),
            confidence=float(row["confidence"]), source_count=0,
        )

    def list(self, *, scope: str, subject_id: str, preset: str, limit: int = 100, include_deleted: bool = False) -> tuple[MemoryRecord, ...]:
        deleted_clause = "" if include_deleted else " AND deleted_at IS NULL"
        rows = self._conn.execute(
            f"SELECT * FROM memorix_paragraphs WHERE scope=? AND subject_id=? AND preset=?{deleted_clause} ORDER BY weight DESC, updated_at DESC, id DESC LIMIT ?",
            (scope, subject_id, preset, max(1, int(limit))),
        ).fetchall()
        return tuple(self._record(row) for row in rows)

    def search(self, *, scope: str, subject_id: str, preset: str, query: str, limit: int = 6) -> tuple[MemoryRecord, ...]:
        candidates = self.list(scope=scope, subject_id=subject_id, preset=preset, limit=max(100, int(limit) * 20))
        qvec = self.embedding.encode(query)
        now = _now()
        ranked: list[tuple[float, MemoryRecord]] = []
        qtokens = set(normalize_memory_text(query).split())
        for record in candidates:
            row = self._conn.execute("SELECT vector, dimension FROM memorix_paragraphs WHERE external_id=? AND scope=? AND subject_id=? AND preset=?", (record.fact_id, scope, subject_id, preset)).fetchone()
            vector = _vector_unblob(row["vector"], int(row["dimension"])) if row and row["vector"] else ()
            semantic = _cosine(qvec, vector)
            overlap = len(qtokens & set(normalize_memory_text(record.content).split())) / max(1, len(qtokens))
            recency = 1.0 / (1.0 + max(0.0, now - record.updated_at) / 604800.0)
            ranked.append((0.62 * semantic + 0.23 * record.weight + 0.10 * overlap + 0.05 * recency, record))
        ranked.sort(key=lambda item: (item[0], item[1].updated_at), reverse=True)
        return tuple(item[1] for item in ranked[: max(1, int(limit))])

    def delete(self, object_id: int, reason: str = "user") -> bool:
        cursor = self._conn.execute("UPDATE memorix_paragraphs SET deleted_at=? WHERE external_id=? AND deleted_at IS NULL", (_now(), int(object_id)))
        self._audit("delete", object_id, {"reason": reason})
        self._conn.commit()
        return cursor.rowcount > 0

    def restore(self, object_id: int) -> bool:
        cursor = self._conn.execute("UPDATE memorix_paragraphs SET deleted_at=NULL, updated_at=? WHERE external_id=?", (_now(), int(object_id)))
        self._audit("restore", object_id, {})
        self._conn.commit()
        return cursor.rowcount > 0

    def stats(self) -> dict[str, Any]:
        row = self._conn.execute("SELECT COUNT(*) total, SUM(deleted_at IS NULL) active, SUM(vector IS NOT NULL) vectors FROM memorix_paragraphs").fetchone()
        return {"total": int(row["total"] or 0), "active": int(row["active"] or 0), "vectors": int(row["vectors"] or 0), "embedding": self.embedding.status}


class JianerMemorixStore(JianerMemoryStore):
    """Drop-in store that mirrors Jianer records into Jianer Memory pools."""

    def __init__(self, database_path: str | Path, *, memorix_data_dir: str | Path | None = None, embedding_config_path: str | Path | None = None, default_memory_enabled: bool = True, default_memory_interval_seconds: int = 6 * 3600):
        super().__init__(database_path, default_memory_enabled=default_memory_enabled, default_memory_interval_seconds=default_memory_interval_seconds)
        root = Path(memorix_data_dir) if memorix_data_dir else Path(database_path).resolve().parent / "data" / "jianer_ai_memorix"
        root.mkdir(parents=True, exist_ok=True)
        config_path = Path(embedding_config_path) if embedding_config_path else Path(database_path).resolve().parent / "aiconfig" / "embedding.json"
        self.embedding = EmbeddingProvider(EmbeddingConfig.from_file(config_path))
        self.vector_db = VectorMemoryDatabase(root / "metadata" / "metadata.db", self.embedding)
        self.memorix_data_dir = root

    def _person_subject(self, canonical_user_id: str) -> str:
        try:
            return self.resolve_canonical_id(canonical_user_id)
        except Exception:
            return str(canonical_user_id)

    def _mirror_person(self, result: MemoryWriteResult | None, canonical_user_id: str, preset: Any, *, canonical_fact: str | None = None, confidence: float = 1.0) -> None:
        if result is None:
            return
        self.vector_db.upsert(object_id=result.fact_id, scope="person", subject_id=self._person_subject(canonical_user_id), canonical_user_id=self._person_subject(canonical_user_id), preset=normalize_preset(preset), content=result.content, canonical_fact=canonical_fact, weight=result.weight, confidence=confidence)

    def _mirror_group(self, result: MemoryWriteResult | None, *, preset: Any, protocol: Any, self_id: Any, group_id: Any, canonical_user_id: str = "", canonical_fact: str | None = None, confidence: float = 1.0) -> None:
        if result is None:
            return
        subject = _group_subject_key(normalize_protocol(protocol), str(self_id), str(group_id))
        self.vector_db.upsert(object_id=result.fact_id, scope="group", subject_id=subject, canonical_user_id=str(canonical_user_id), preset=normalize_preset(preset), content=result.content, canonical_fact=canonical_fact, weight=result.weight, confidence=confidence)

    def create_memory(self, **kwargs: Any) -> MemoryWriteResult | None:
        result = super().create_memory(**kwargs)
        self._mirror_person(result, str(kwargs.get("canonical_user_id", "")), kwargs.get("preset", "default"), canonical_fact=kwargs.get("canonical_fact"), confidence=float(kwargs.get("confidence", 1.0)))
        return result

    def update_memory(self, **kwargs: Any) -> MemoryWriteResult | None:
        result = super().update_memory(**kwargs)
        self._mirror_person(result, str(kwargs.get("canonical_user_id", "")), kwargs.get("preset", "default"), canonical_fact=kwargs.get("canonical_fact"), confidence=float(kwargs.get("confidence", 1.0)))
        return result

    def list_memories(self, **kwargs: Any) -> tuple[MemoryRecord, ...]:
        records = self.vector_db.list(scope="person", subject_id=self._person_subject(str(kwargs.get("canonical_user_id", ""))), preset=normalize_preset(kwargs.get("preset", "default")), limit=int(kwargs.get("limit", 100)))
        return records or super().list_memories(**kwargs)

    def query_memories(self, **kwargs: Any) -> tuple[MemoryRecord, ...]:
        records = self.vector_db.search(scope="person", subject_id=self._person_subject(str(kwargs.get("canonical_user_id", ""))), preset=normalize_preset(kwargs.get("preset", "default")), query=str(kwargs.get("query", "")), limit=int(kwargs.get("limit", 6)))
        return records or super().query_memories(**kwargs)

    def create_group_memory(self, **kwargs: Any) -> MemoryWriteResult | None:
        result = super().create_group_memory(**kwargs)
        self._mirror_group(result, preset=kwargs.get("preset", "default"), protocol=kwargs.get("protocol", "onebot"), self_id=kwargs.get("self_id", ""), group_id=kwargs.get("group_id", ""), canonical_user_id=str(kwargs.get("canonical_user_id", "")), canonical_fact=kwargs.get("canonical_fact"), confidence=float(kwargs.get("confidence", 1.0)))
        return result

    def update_group_memory(self, **kwargs: Any) -> MemoryWriteResult | None:
        result = super().update_group_memory(**kwargs)
        self._mirror_group(result, preset=kwargs.get("preset", "default"), protocol=kwargs.get("protocol", "onebot"), self_id=kwargs.get("self_id", ""), group_id=kwargs.get("group_id", ""), canonical_user_id=str(kwargs.get("canonical_user_id", "")), canonical_fact=kwargs.get("canonical_fact"), confidence=float(kwargs.get("confidence", 1.0)))
        return result

    def list_group_memories(self, **kwargs: Any) -> tuple[MemoryRecord, ...]:
        subject = _group_subject_key(normalize_protocol(kwargs.get("protocol", "onebot")), str(kwargs.get("self_id", "")), str(kwargs.get("group_id", "")))
        records = self.vector_db.list(scope="group", subject_id=subject, preset=normalize_preset(kwargs.get("preset", "default")), limit=int(kwargs.get("limit", 100)))
        return records or super().list_group_memories(**kwargs)

    def query_group_memories(self, **kwargs: Any) -> tuple[MemoryRecord, ...]:
        subject = _group_subject_key(normalize_protocol(kwargs.get("protocol", "onebot")), str(kwargs.get("self_id", "")), str(kwargs.get("group_id", "")))
        records = self.vector_db.search(scope="group", subject_id=subject, preset=normalize_preset(kwargs.get("preset", "default")), query=str(kwargs.get("query", "")), limit=int(kwargs.get("limit", 6)))
        return records or super().query_group_memories(**kwargs)

    def insert_generated_memories(self, *args: Any, **kwargs: Any) -> Any:
        result = super().insert_generated_memories(*args, **kwargs)
        token = args[0] if args else kwargs.get("token")
        canonical = getattr(token, "canonical_user_id", "")
        preset = getattr(token, "preset", "default")
        if canonical:
            for item in self.list_memories(canonical_user_id=canonical, preset=preset, limit=1000):
                self._mirror_person(MemoryWriteResult(fact_id=item.fact_id, content=item.content, weight=item.weight, outcome="mirrored"), canonical, preset, canonical_fact=item.canonical_fact, confidence=item.confidence)
        return result

    def delete_memory(self, **kwargs: Any) -> bool:
        result = bool(super().delete_memory(**kwargs))
        if result:
            self.vector_db.delete(int(kwargs.get("fact_id", kwargs.get("memory_id", 0))))
        return result

    def restore_memory(self, **kwargs: Any) -> bool:
        result = bool(super().restore_memory(**kwargs))
        if result:
            self.vector_db.restore(int(kwargs.get("memory_id", kwargs.get("fact_id", 0))))
        return result

    def record_conversation_episode(self, **kwargs: Any) -> Any:
        result = super().record_conversation_episode(**kwargs)
        episode_id = getattr(result, "episode_id", None)
        if episode_id is not None:
            user_content = str(kwargs.get("user_content", "")).strip()
            assistant_content = str(kwargs.get("assistant_content", "")).strip()
            content = f"用户：{user_content}\n简儿：{assistant_content}".strip()
            if content:
                subject = f"{kwargs.get('protocol', '')}:{kwargs.get('self_id', '')}:{kwargs.get('conversation_kind', '')}:{kwargs.get('conversation_id', '')}"
                self.vector_db.upsert(object_id=int(episode_id), scope="episode", subject_id=subject, canonical_user_id=str(kwargs.get("speaker_canonical_id", "")), preset=normalize_preset(kwargs.get("preset", "default")), content=content, canonical_fact=content, weight=0.35, confidence=1.0, source_type="episode")
        return result

    def memorix_stats(self) -> dict[str, Any]:
        return self.vector_db.stats()

    def close(self) -> None:
        self.vector_db.close()
        legacy_close = getattr(super(), "close", None)
        if callable(legacy_close):
            legacy_close()


__all__ = ["EmbeddingConfig", "EmbeddingProvider", "JianerMemorixStore", "VectorMemoryDatabase"]
