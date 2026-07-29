"""Rehearsable migration from the legacy ``jianer_memory.db`` layout.

The migrator never writes to the supplied legacy database.  Production use is
expected to follow four explicit steps: ``copy_source`` -> ``dry_run`` or
``stage`` -> ``verify`` -> ``switch``.  ``apply`` is also available for a
transactional import into an already selected normalized target database.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from plugins.JianerAI.memory import GeneratedMemory, JianerMemoryStore


RAW_GROUP_RE = re.compile(r"^raw_g(?P<group_id>\d+)$")
RAW_PRIVATE_RE = re.compile(r"^raw_p_u(?P<user_id>\d+)$")
MEM_GROUP_RE = re.compile(
    r"^mem_g(?P<group_id>\d+)_p(?P<preset>[A-Za-z0-9_]+)$"
)
MEM_PRIVATE_RE = re.compile(
    r"^mem_p_u(?P<user_id>\d+)_p(?P<preset>[A-Za-z0-9_]+)$"
)


class MigrationError(RuntimeError):
    """Base migration failure."""


class MigrationVerificationError(MigrationError):
    """Raised when a staged or imported database fails verification."""


@dataclass(frozen=True)
class MigrationPlan:
    source_path: str
    source_sha256: str
    source_quick_check: tuple[str, ...]
    tables: tuple[str, ...]
    table_counts: Mapping[str, int]
    raw_tables: tuple[str, ...]
    memory_tables: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class MigrationReport:
    migration_id: str
    status: str
    dry_run: bool
    source_path: str
    source_sha256: str
    source_counts: Mapping[str, int]
    imported_counts: Mapping[str, int]
    duplicate_counts: Mapping[str, int]
    quarantined_count: int
    target_quick_check: tuple[str, ...]
    foreign_key_issues: tuple[tuple[Any, ...], ...]
    verified: bool
    backup_path: str | None = None


def _now_ts() -> int:
    return int(time.time())


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _read_only_uri(path: Path) -> str:
    posix = path.resolve().as_posix()
    encoded = urllib.parse.quote(posix, safe="/:")
    return f"file:{encoded}?mode=ro"


def _read_only_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        _read_only_uri(path),
        uri=True,
        timeout=30.0,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _table_names(conn: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        str(row["name"])
        for row in conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
    )


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type='table' AND name=?
            LIMIT 1
            """,
            (name,),
        ).fetchone()
        is not None
    )


class LegacyMemoryMigrator:
    """Copy, rehearse, validate, apply, switch, and roll back legacy data."""

    def __init__(
        self,
        target_db_path: str | Path,
        *,
        legacy_protocol: str = "onebot",
        legacy_self_id: str = "legacy",
    ):
        self.target_db_path = Path(target_db_path)
        self.legacy_protocol = str(legacy_protocol or "onebot")
        self.legacy_self_id = str(legacy_self_id or "legacy")

    def plan(self, source_path: str | Path) -> MigrationPlan:
        source = Path(source_path)
        if not source.is_file():
            raise FileNotFoundError(source)
        conn = _read_only_connection(source)
        try:
            quick_check = tuple(
                str(row[0]) for row in conn.execute("PRAGMA quick_check")
            )
            tables = _table_names(conn)
            counts: dict[str, int] = {}
            for table in tables:
                row = conn.execute(
                    f"SELECT COUNT(*) AS count FROM {_quote_identifier(table)}"
                ).fetchone()
                counts[table] = int(row["count"])
        finally:
            conn.close()

        raw_tables = tuple(
            table
            for table in tables
            if RAW_GROUP_RE.fullmatch(table)
            or RAW_PRIVATE_RE.fullmatch(table)
        )
        memory_tables = tuple(
            table
            for table in tables
            if MEM_GROUP_RE.fullmatch(table)
            or MEM_PRIVATE_RE.fullmatch(table)
        )
        warnings: list[str] = []
        if quick_check != ("ok",):
            warnings.append("legacy source quick_check did not return ok")
        if "mem_global" in counts and counts["mem_global"]:
            warnings.append(
                "global memories have no canonical user and will be quarantined"
            )
        unknown_dynamic = tuple(
            table
            for table in tables
            if table.startswith(("raw_", "mem_"))
            and table not in raw_tables
            and table not in memory_tables
            and table != "mem_global"
        )
        if unknown_dynamic:
            warnings.append(
                "unrecognized dynamic tables will not be imported: "
                + ", ".join(unknown_dynamic)
            )
        return MigrationPlan(
            source_path=str(source.resolve()),
            source_sha256=_file_sha256(source),
            source_quick_check=quick_check,
            tables=tables,
            table_counts=counts,
            raw_tables=raw_tables,
            memory_tables=memory_tables,
            warnings=tuple(warnings),
        )

    def copy_source(
        self,
        source_path: str | Path,
        destination_path: str | Path,
        *,
        overwrite: bool = False,
    ) -> Path:
        """Create a transactionally consistent SQLite backup of the source."""

        source = Path(source_path).resolve()
        destination = Path(destination_path).resolve()
        if source == destination:
            raise ValueError("source and destination must differ")
        if not source.is_file():
            raise FileNotFoundError(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if not overwrite:
                raise FileExistsError(destination)
            destination.unlink()

        source_conn = _read_only_connection(source)
        target_conn = sqlite3.connect(str(destination), isolation_level=None)
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
            source_conn.close()
        if self.plan(destination).source_quick_check != ("ok",):
            raise MigrationVerificationError("copied source failed quick_check")
        return destination

    @staticmethod
    def _backup_database(
        source_path: Path,
        destination_path: Path,
        *,
        overwrite: bool,
    ) -> Path:
        source = source_path.resolve()
        destination = destination_path.resolve()
        if source == destination:
            raise ValueError("source and destination must differ")
        if not source.is_file():
            raise FileNotFoundError(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if not overwrite:
                raise FileExistsError(destination)
            destination.unlink()
        source_conn = sqlite3.connect(str(source), timeout=30.0)
        target_conn = sqlite3.connect(str(destination), isolation_level=None)
        try:
            source_conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
            source_conn.close()
        return destination

    def dry_run(self, source_path: str | Path) -> MigrationReport:
        """Run a complete import and verification without touching the target."""

        target_existed = self.target_db_path.exists()
        target_hash = (
            _file_sha256(self.target_db_path) if target_existed else None
        )
        with tempfile.TemporaryDirectory(prefix="jianer-ai-migration-") as tmp:
            root = Path(tmp)
            source_copy = self.copy_source(
                source_path, root / "legacy-copy.db"
            )
            scratch = root / "normalized-dry-run.db"
            JianerMemoryStore(scratch)
            report = self._run_import(
                source_copy,
                scratch,
                dry_run=True,
                migration_id=None,
                backup_path=None,
            )

        if target_existed != self.target_db_path.exists():
            raise MigrationError("dry-run changed target database existence")
        if target_existed and _file_sha256(self.target_db_path) != target_hash:
            raise MigrationError("dry-run changed target database content")
        return report

    def stage(
        self,
        source_path: str | Path,
        staged_path: str | Path,
        *,
        overwrite: bool = False,
    ) -> MigrationReport:
        """Build a verified normalized database without selecting it as live."""

        staged = Path(staged_path).resolve()
        if staged.exists():
            if not overwrite:
                raise FileExistsError(staged)
            staged.unlink()
        staged.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="jianer-ai-stage-") as tmp:
            source_copy = self.copy_source(
                source_path, Path(tmp) / "legacy-copy.db"
            )
            JianerMemoryStore(staged)
            report = self._run_import(
                source_copy,
                staged,
                dry_run=False,
                migration_id=None,
                backup_path=None,
            )
        if not report.verified:
            raise MigrationVerificationError("staged migration did not verify")
        return report

    def apply(
        self,
        source_copy_path: str | Path,
        *,
        backup_path: str | Path,
        migration_id: str | None = None,
        overwrite_backup: bool = False,
    ) -> MigrationReport:
        """Transactionally import a copied source into the selected target."""

        source_copy = Path(source_copy_path).resolve()
        if not source_copy.is_file():
            raise FileNotFoundError(source_copy)
        JianerMemoryStore(self.target_db_path)
        backup = self._backup_database(
            self.target_db_path,
            Path(backup_path),
            overwrite=overwrite_backup,
        )
        try:
            return self._run_import(
                source_copy,
                self.target_db_path,
                dry_run=False,
                migration_id=migration_id,
                backup_path=str(backup),
            )
        except BaseException as exc:
            self._record_failed_migration(
                source_copy,
                migration_id=migration_id,
                backup_path=str(backup),
                error=str(exc),
            )
            raise

    def cutover(
        self,
        source_copy_path: str | Path,
        *,
        backup_path: str | Path,
        migration_id: str | None = None,
        overwrite_backup: bool = False,
    ) -> MigrationReport:
        """Alias for transactional import after the rehearsal gates pass."""

        return self.apply(
            source_copy_path,
            backup_path=backup_path,
            migration_id=migration_id,
            overwrite_backup=overwrite_backup,
        )

    def switch(
        self,
        staged_path: str | Path,
        *,
        backup_path: str | Path,
        overwrite_backup: bool = False,
    ) -> Path:
        """Select a verified staged normalized DB, retaining a live backup."""

        staged = Path(staged_path).resolve()
        if not staged.is_file():
            raise FileNotFoundError(staged)
        staged_store = JianerMemoryStore(staged, initialize=False)
        if staged_store.quick_check() != ("ok",):
            raise MigrationVerificationError("staged DB failed quick_check")
        if staged_store.foreign_key_check():
            raise MigrationVerificationError(
                "staged DB failed foreign_key_check"
            )
        conn = sqlite3.connect(str(staged))
        try:
            row = conn.execute(
                """
                SELECT version
                FROM schema_meta
                WHERE schema_name='jianer_ai_memory'
                """
            ).fetchone()
            if row is None:
                raise MigrationVerificationError(
                    "staged DB is not a JianerAI normalized database"
                )
        finally:
            conn.close()

        JianerMemoryStore(self.target_db_path)
        backup = self._backup_database(
            self.target_db_path,
            Path(backup_path),
            overwrite=overwrite_backup,
        )
        staged_conn = _read_only_connection(staged)
        target_conn = sqlite3.connect(str(self.target_db_path), timeout=30.0)
        try:
            target_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            staged_conn.backup(target_conn)
        finally:
            target_conn.close()
            staged_conn.close()
        selected = JianerMemoryStore(self.target_db_path, initialize=False)
        if selected.quick_check() != ("ok",) or selected.foreign_key_check():
            self.rollback_database(backup)
            raise MigrationVerificationError(
                "selected database failed verification; backup restored"
            )
        return backup

    def rollback_database(self, backup_path: str | Path) -> None:
        """Restore the selected target from a SQLite backup file."""

        backup = Path(backup_path).resolve()
        if not backup.is_file():
            raise FileNotFoundError(backup)
        source_conn = _read_only_connection(backup)
        target_conn = sqlite3.connect(str(self.target_db_path), timeout=30.0)
        try:
            target_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
            source_conn.close()
        restored = JianerMemoryStore(self.target_db_path, initialize=False)
        if restored.quick_check() != ("ok",) or restored.foreign_key_check():
            raise MigrationVerificationError(
                "restored database failed verification"
            )

    def rollback(self, migration_id: str) -> bool:
        """Restore the backup recorded by a completed migration."""

        migration_id_s = str(migration_id or "").strip()
        if not migration_id_s:
            raise ValueError("migration_id must not be empty")
        store = JianerMemoryStore(self.target_db_path)
        conn = store._connect()
        try:
            row = conn.execute(
                """
                SELECT *
                FROM migration_ledger
                WHERE migration_id = ?
                """,
                (migration_id_s,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return False
        backup_path = str(row["backup_path"] or "")
        if not backup_path:
            raise MigrationError("migration has no rollback backup")
        preserved = dict(row)
        self.rollback_database(backup_path)

        # The backup predates the migration ledger row; retain an auditable
        # rolled-back marker after restoring it.
        store = JianerMemoryStore(self.target_db_path)
        conn = store._connect()
        try:
            with store._transaction(conn):
                conn.execute(
                    """
                    INSERT INTO migration_ledger(
                        migration_id,
                        source_path,
                        source_sha256,
                        status,
                        dry_run,
                        started_at,
                        completed_at,
                        backup_path,
                        counts_json,
                        verification_json,
                        error
                    )
                    VALUES(?, ?, ?, 'rolled_back', ?, ?, ?, ?, ?, ?, NULL)
                    ON CONFLICT(migration_id)
                    DO UPDATE SET status='rolled_back',
                                  completed_at=excluded.completed_at,
                                  backup_path=excluded.backup_path
                    """,
                    (
                        migration_id_s,
                        str(preserved["source_path"]),
                        str(preserved["source_sha256"]),
                        int(preserved["dry_run"]),
                        int(preserved["started_at"]),
                        _now_ts(),
                        backup_path,
                        str(preserved["counts_json"]),
                        str(preserved["verification_json"]),
                    ),
                )
        finally:
            conn.close()
        return True

    def verify(self, database_path: str | Path) -> Mapping[str, Any]:
        store = JianerMemoryStore(database_path, initialize=False)
        quick_check = store.quick_check()
        foreign_keys = store.foreign_key_check()
        conn = store._connect()
        try:
            counts = {
                table: int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
                    ).fetchone()[0]
                )
                for table in (
                    "canonical_identities",
                    "identity_aliases",
                    "conversations",
                    "raw_transcript_messages",
                    "session_settings",
                    "memory_facts",
                    "memory_evidence",
                    "memory_suppressions",
                    "migration_quarantine",
                )
            }
        finally:
            conn.close()
        return {
            "quick_check": quick_check,
            "foreign_key_issues": foreign_keys,
            "counts": counts,
            "verified": quick_check == ("ok",) and not foreign_keys,
        }

    def _quarantine(
        self,
        conn: sqlite3.Connection,
        *,
        migration_id: str,
        item_type: str,
        source_table: str,
        source_key: str,
        payload: Mapping[str, Any],
        reason: str,
        now: int,
    ) -> bool:
        cursor = conn.execute(
            """
            INSERT INTO migration_quarantine(
                migration_id,
                item_type,
                source_table,
                source_key,
                payload_json,
                reason,
                created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(migration_id, source_table, source_key, reason)
            DO NOTHING
            """,
            (
                migration_id,
                item_type,
                source_table,
                source_key,
                json.dumps(
                    dict(payload), ensure_ascii=False, sort_keys=True, default=str
                ),
                reason,
                now,
            ),
        )
        return cursor.rowcount > 0

    def _import_raw_table(
        self,
        *,
        source_conn: sqlite3.Connection,
        target_conn: sqlite3.Connection,
        target_store: JianerMemoryStore,
        table: str,
        migration_id: str,
        now: int,
        imported: dict[str, int],
        duplicates: dict[str, int],
    ) -> int:
        group_match = RAW_GROUP_RE.fullmatch(table)
        private_match = RAW_PRIVATE_RE.fullmatch(table)
        if group_match:
            kind = "group"
            conversation_id = group_match.group("group_id")
            fallback_sender = ""
        elif private_match:
            kind = "private"
            conversation_id = private_match.group("user_id")
            fallback_sender = conversation_id
        else:
            return 0

        quarantined = 0
        rows = source_conn.execute(
            f"SELECT * FROM {_quote_identifier(table)} ORDER BY seq"
        ).fetchall()
        for row in rows:
            payload = dict(row)
            sender = str(payload.get("sender") or fallback_sender).strip()
            content = str(payload.get("content") or "").strip()
            source_key = str(payload.get("seq") or "")
            if not sender or sender == "0" or not content:
                quarantined += int(
                    self._quarantine(
                        target_conn,
                        migration_id=migration_id,
                        item_type="raw_message",
                        source_table=table,
                        source_key=source_key,
                        payload=payload,
                        reason="missing canonical sender or content",
                        now=now,
                    )
                )
                continue
            result = target_store._record_transcript_in_tx(
                target_conn,
                protocol=self.legacy_protocol,
                self_id=self.legacy_self_id,
                conversation_kind=kind,
                conversation_id=conversation_id,
                message_id=payload.get("message_id"),
                sender_protocol=self.legacy_protocol,
                sender_self_id=self.legacy_self_id,
                sender_external_id=sender,
                sender_canonical_id=None,
                preset="default",
                content=content,
                occurred_at=(
                    int(payload["timestamp"])
                    if str(payload.get("timestamp") or "").isdigit()
                    else now
                ),
                message_type=str(payload.get("message_type") or kind),
                now=now,
            )
            if result.inserted:
                imported["raw_transcripts"] += 1
            else:
                duplicates["raw_transcripts"] += 1
        return quarantined

    def _import_settings(
        self,
        *,
        source_conn: sqlite3.Connection,
        target_conn: sqlite3.Connection,
        target_store: JianerMemoryStore,
        now: int,
        imported: dict[str, int],
    ) -> None:
        if not _table_exists(source_conn, "memory_settings"):
            return
        rows = source_conn.execute(
            "SELECT * FROM memory_settings ORDER BY rowid"
        ).fetchall()
        for row in rows:
            payload = dict(row)
            is_private = bool(int(payload.get("is_private") or 0))
            if is_private:
                conversation_id = str(payload.get("user_id") or "").strip()
                kind = "private"
            else:
                conversation_id = str(payload.get("group_id") or "").strip()
                kind = "group"
            if not conversation_id or conversation_id == "0":
                continue
            session_id = target_store._ensure_session_in_tx(
                target_conn,
                protocol=self.legacy_protocol,
                self_id=self.legacy_self_id,
                conversation_kind=kind,
                conversation_id=conversation_id,
                preset="default",
                now=now,
            )
            target_conn.execute(
                """
                UPDATE session_settings
                SET memory_enabled = ?,
                    memory_interval_seconds = ?,
                    updated_at = ?
                WHERE session_id = ?
                """,
                (
                    int(bool(payload.get("enabled", 1))),
                    max(60, int(payload.get("interval_seconds") or 21600)),
                    now,
                    session_id,
                ),
            )
            imported["session_settings"] += 1

    def _import_memory_table(
        self,
        *,
        source_conn: sqlite3.Connection,
        target_conn: sqlite3.Connection,
        target_store: JianerMemoryStore,
        table: str,
        migration_id: str,
        now: int,
        imported: dict[str, int],
        duplicates: dict[str, int],
    ) -> int:
        private_match = MEM_PRIVATE_RE.fullmatch(table)
        group_match = MEM_GROUP_RE.fullmatch(table)
        if private_match:
            default_user = private_match.group("user_id")
            preset = private_match.group("preset")
            group_memory = False
        elif group_match:
            default_user = ""
            preset = group_match.group("preset")
            group_memory = True
        else:
            return 0

        quarantined = 0
        rows = source_conn.execute(
            f"SELECT * FROM {_quote_identifier(table)} ORDER BY memory_id"
        ).fetchall()
        for row in rows:
            payload = dict(row)
            source_key = str(payload.get("memory_id") or "")
            user_id = str(payload.get("user_id") or default_user).strip()
            content = str(payload.get("memory_content") or "").strip()
            if group_memory and (not user_id or user_id == "0"):
                quarantined += int(
                    self._quarantine(
                        target_conn,
                        migration_id=migration_id,
                        item_type="memory_fact",
                        source_table=table,
                        source_key=source_key,
                        payload=payload,
                        reason=(
                            "legacy group memory has no canonical user; "
                            "manual attribution required"
                        ),
                        now=now,
                    )
                )
                continue
            if not user_id or user_id == "0" or not content:
                quarantined += int(
                    self._quarantine(
                        target_conn,
                        migration_id=migration_id,
                        item_type="memory_fact",
                        source_table=table,
                        source_key=source_key,
                        payload=payload,
                        reason="missing canonical user or memory content",
                        now=now,
                    )
                )
                continue
            canonical = target_store._ensure_identity_in_tx(
                target_conn,
                self.legacy_protocol,
                self.legacy_self_id,
                user_id,
                now,
            )
            try:
                weight = float(payload.get("weight", 0.3))
            except (TypeError, ValueError):
                weight = 0.3
            outcome, _ = target_store._upsert_memory_in_tx(
                target_conn,
                canonical_id=canonical,
                preset=preset,
                memory=GeneratedMemory(content=content, weight=weight),
                now=int(payload.get("generated_at") or now),
                honor_suppressions=True,
            )
            if outcome == "inserted":
                imported["memory_facts"] += 1
            elif outcome == "updated":
                duplicates["memory_facts"] += 1
        return quarantined

    def _run_import(
        self,
        source_path: Path,
        target_path: Path,
        *,
        dry_run: bool,
        migration_id: str | None,
        backup_path: str | None,
    ) -> MigrationReport:
        plan = self.plan(source_path)
        if plan.source_quick_check != ("ok",):
            raise MigrationVerificationError("legacy source failed quick_check")
        migration_id_s = (
            str(migration_id).strip()
            if migration_id is not None
            else f"legacy-{plan.source_sha256[:20]}"
        )
        if not migration_id_s:
            raise ValueError("migration_id must not be empty")
        target_store = JianerMemoryStore(target_path)
        target_conn = target_store._connect()
        source_conn = _read_only_connection(source_path)
        imported = {
            "raw_transcripts": 0,
            "session_settings": 0,
            "memory_facts": 0,
        }
        duplicates = {"raw_transcripts": 0, "memory_facts": 0}
        quarantined = 0
        now = _now_ts()
        try:
            existing = target_conn.execute(
                """
                SELECT status, source_sha256, counts_json, verification_json
                FROM migration_ledger
                WHERE migration_id = ?
                """,
                (migration_id_s,),
            ).fetchone()
            if existing is not None and str(existing["status"]) == "completed":
                if str(existing["source_sha256"]) != plan.source_sha256:
                    raise MigrationError(
                        "migration_id already belongs to another source"
                    )
                counts = json.loads(str(existing["counts_json"]) or "{}")
                verification = json.loads(
                    str(existing["verification_json"]) or "{}"
                )
                return MigrationReport(
                    migration_id=migration_id_s,
                    status="already_applied",
                    dry_run=dry_run,
                    source_path=plan.source_path,
                    source_sha256=plan.source_sha256,
                    source_counts=plan.table_counts,
                    imported_counts=counts.get("imported", {}),
                    duplicate_counts=counts.get("duplicates", {}),
                    quarantined_count=int(counts.get("quarantined", 0)),
                    target_quick_check=tuple(
                        verification.get("quick_check", ())
                    ),
                    foreign_key_issues=tuple(
                        tuple(item)
                        for item in verification.get(
                            "foreign_key_issues", ()
                        )
                    ),
                    verified=bool(verification.get("verified")),
                    backup_path=backup_path,
                )

            with target_store._transaction(target_conn):
                target_conn.execute(
                    """
                    INSERT INTO migration_ledger(
                        migration_id,
                        source_path,
                        source_sha256,
                        status,
                        dry_run,
                        started_at,
                        completed_at,
                        backup_path,
                        counts_json,
                        verification_json,
                        error
                    )
                    VALUES(?, ?, ?, 'running', ?, ?, NULL, ?, '{}', '{}', NULL)
                    ON CONFLICT(migration_id)
                    DO UPDATE SET
                        source_path=excluded.source_path,
                        source_sha256=excluded.source_sha256,
                        status='running',
                        dry_run=excluded.dry_run,
                        started_at=excluded.started_at,
                        completed_at=NULL,
                        backup_path=excluded.backup_path,
                        counts_json='{}',
                        verification_json='{}',
                        error=NULL
                    """,
                    (
                        migration_id_s,
                        plan.source_path,
                        plan.source_sha256,
                        int(dry_run),
                        now,
                        backup_path,
                    ),
                )

                for table in plan.raw_tables:
                    quarantined += self._import_raw_table(
                        source_conn=source_conn,
                        target_conn=target_conn,
                        target_store=target_store,
                        table=table,
                        migration_id=migration_id_s,
                        now=now,
                        imported=imported,
                        duplicates=duplicates,
                    )
                self._import_settings(
                    source_conn=source_conn,
                    target_conn=target_conn,
                    target_store=target_store,
                    now=now,
                    imported=imported,
                )
                for table in plan.memory_tables:
                    quarantined += self._import_memory_table(
                        source_conn=source_conn,
                        target_conn=target_conn,
                        target_store=target_store,
                        table=table,
                        migration_id=migration_id_s,
                        now=now,
                        imported=imported,
                        duplicates=duplicates,
                    )

                if _table_exists(source_conn, "mem_global"):
                    for row in source_conn.execute(
                        "SELECT * FROM mem_global ORDER BY memory_id"
                    ):
                        quarantined += int(
                            self._quarantine(
                                target_conn,
                                migration_id=migration_id_s,
                                item_type="global_memory",
                                source_table="mem_global",
                                source_key=str(row["memory_id"]),
                                payload=dict(row),
                                reason=(
                                    "global memory has no canonical user; "
                                    "manual attribution required"
                                ),
                                now=now,
                            )
                        )

                if _table_exists(source_conn, "memory_state"):
                    for index, row in enumerate(
                        source_conn.execute(
                            "SELECT * FROM memory_state ORDER BY rowid"
                        ),
                        start=1,
                    ):
                        quarantined += int(
                            self._quarantine(
                                target_conn,
                                migration_id=migration_id_s,
                                item_type="generation_cursor",
                                source_table="memory_state",
                                source_key=str(index),
                                payload=dict(row),
                                reason=(
                                    "legacy dynamic-table sequence cannot be "
                                    "safely mapped; transcript will replay"
                                ),
                                now=now,
                            )
                        )

                counts_payload = json.dumps(
                    {
                        "imported": imported,
                        "duplicates": duplicates,
                        "quarantined": quarantined,
                    },
                    sort_keys=True,
                )
                target_conn.execute(
                    """
                    UPDATE migration_ledger
                    SET counts_json = ?
                    WHERE migration_id = ?
                    """,
                    (counts_payload, migration_id_s),
                )

            verification = self.verify(target_path)
            verified = bool(verification["verified"])
            if not verified:
                raise MigrationVerificationError(
                    "normalized target failed verification"
                )
            verification_json = json.dumps(
                {
                    "quick_check": verification["quick_check"],
                    "foreign_key_issues": verification["foreign_key_issues"],
                    "counts": verification["counts"],
                    "verified": verified,
                },
                sort_keys=True,
            )
            with target_store._transaction(target_conn):
                target_conn.execute(
                    """
                    UPDATE migration_ledger
                    SET status='completed',
                        completed_at=?,
                        verification_json=?,
                        error=NULL
                    WHERE migration_id=?
                    """,
                    (_now_ts(), verification_json, migration_id_s),
                )
            return MigrationReport(
                migration_id=migration_id_s,
                status="completed",
                dry_run=dry_run,
                source_path=plan.source_path,
                source_sha256=plan.source_sha256,
                source_counts=plan.table_counts,
                imported_counts=dict(imported),
                duplicate_counts=dict(duplicates),
                quarantined_count=quarantined,
                target_quick_check=tuple(verification["quick_check"]),
                foreign_key_issues=tuple(
                    tuple(item)
                    for item in verification["foreign_key_issues"]
                ),
                verified=verified,
                backup_path=backup_path,
            )
        finally:
            source_conn.close()
            target_conn.close()

    def _record_failed_migration(
        self,
        source_path: Path,
        *,
        migration_id: str | None,
        backup_path: str,
        error: str,
    ) -> None:
        try:
            plan = self.plan(source_path)
            migration_id_s = (
                str(migration_id).strip()
                if migration_id is not None
                else f"legacy-{plan.source_sha256[:20]}"
            )
            store = JianerMemoryStore(self.target_db_path)
            conn = store._connect()
            try:
                with store._transaction(conn):
                    conn.execute(
                        """
                        INSERT INTO migration_ledger(
                            migration_id,
                            source_path,
                            source_sha256,
                            status,
                            dry_run,
                            started_at,
                            completed_at,
                            backup_path,
                            counts_json,
                            verification_json,
                            error
                        )
                        VALUES(?, ?, ?, 'failed', 0, ?, ?, ?, '{}', '{}', ?)
                        ON CONFLICT(migration_id)
                        DO UPDATE SET status='failed',
                                      completed_at=excluded.completed_at,
                                      backup_path=excluded.backup_path,
                                      error=excluded.error
                        """,
                        (
                            migration_id_s,
                            plan.source_path,
                            plan.source_sha256,
                            _now_ts(),
                            _now_ts(),
                            backup_path,
                            error[:4000],
                        ),
                    )
            finally:
                conn.close()
        except Exception:
            # Preserve the original migration exception; failure-ledger
            # recording is best effort after the transaction rolled back.
            return
