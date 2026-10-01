"""Migrate Jianer legacy memories into the Jianer Memory backend.

The source SQLite file is opened read-only and the target is backed up before
import.  Jianer v5 stores person and group facts in physical tables whose
names end in ``_people`` and ``_groups``; the migration deliberately uses
those suffixes instead of guessing a group from the table prefix.  A group's
``group_ref`` is already a stable identity (the source store hashes the
transport coordinates), so it is preserved as the migrated group id.

The adapter's canonical-fact uniqueness makes a rerun idempotent.  Evidence is
migrated after its owning fact and is also protected by the adapter's evidence
fingerprint uniqueness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

from plugins.JianerAI.memory import JianerMemoryStore
from plugins.JianerAI.memorix_adapter import JianerMemoryAdapter


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def table_names(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type='table'
              AND (name LIKE 'mem_%' OR name LIKE 'persona_%')
            ORDER BY name
            """
        )
    ]


def _row_value(row: sqlite3.Row, *names: str, default: Any = None) -> Any:
    """Read the first present column from mixed Jianer schema revisions."""

    keys = set(row.keys())
    for name in names:
        if name in keys and row[name] is not None:
            return row[name]
    return default


def _fact_tables(tables: list[str]) -> list[str]:
    """Return only physical fact tables, excluding evidence/deletion tables."""

    return [table for table in tables if table.endswith(("_people", "_groups"))]


def _evidence_rows(
    connection: sqlite3.Connection,
    fact_table: str,
    *,
    scope: str,
    memory_id: int,
) -> list[dict[str, Any]]:
    """Load evidence belonging to a source fact when its table exists."""

    suffix = "_people" if scope == "person" else "_groups"
    evidence_table = f"{fact_table[:-len(suffix)]}_evidence"
    if evidence_table not in table_names(connection):
        return []
    rows = connection.execute(f'SELECT * FROM "{evidence_table}"').fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        if str(_row_value(row, "memory_scope", default=scope)) != scope:
            continue
        try:
            source_id = int(_row_value(row, "memory_id", default=-1))
        except (TypeError, ValueError):
            continue
        if source_id == memory_id:
            result.append(dict(row))
    return result


def _group_coordinates(
    row: Mapping[str, Any],
    *,
    default_protocol: str,
    default_self_id: str,
) -> tuple[str, str, str]:
    """Return transport coordinates while preserving hashed legacy group refs."""

    protocol = str(row.get("protocol") or default_protocol or "legacy")
    self_id = str(row.get("self_id") or default_self_id or "legacy")
    group_id = row.get("group_id") or row.get("group_key") or row.get("group_ref")
    group_id = str(group_id or "").strip()
    if not group_id:
        raise ValueError("legacy group row has no group_ref/group_id")
    return protocol, self_id, group_id


def migrate(
    source: Path,
    target_db: Path,
    data_dir: Path,
    *,
    dry_run: bool = False,
    group_protocol: str = "legacy",
    group_self_id: str = "legacy",
) -> dict[str, Any]:
    source = source.resolve()
    target_db = target_db.resolve()
    data_dir = data_dir.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if source == target_db:
        raise ValueError("source and target database must be different")
    source_hash = sha256(source)
    report: dict[str, Any] = {
        "source": str(source),
        "source_sha256": source_hash,
        "target": str(target_db),
        "data_dir": str(data_dir),
        "dry_run": dry_run,
        "imported": 0,
        "skipped": 0,
        "unchanged": 0,
        "scopes": {"person": 0, "group": 0},
    }
    connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = table_names(connection)
        report["tables"] = tables
        if dry_run:
            report["counts"] = {
                table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in _fact_tables(tables)
            }
            return report
        target_db.parent.mkdir(parents=True, exist_ok=True)
        backup = target_db.with_suffix(target_db.suffix + f".pre-memorix-{int(time.time())}.bak")
        if target_db.exists():
            shutil.copy2(target_db, backup)
            report["backup"] = str(backup)
        legacy = JianerMemoryStore(target_db)
        adapter = JianerMemoryAdapter(
            project_root=source.parent,
            data_dir=data_dir,
            legacy_store=legacy,
            enabled=True,
        )
        for table in _fact_tables(tables):
            rows = connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            is_group = table.endswith("_groups")
            for row in rows:
                payload = dict(row)
                content = str(_row_value(row, "memory_content", "memory_text", "content", default="") or "").strip()
                if not content:
                    report["skipped"] += 1
                    continue
                preset = str(_row_value(row, "preset", default="default") or "default")
                source_memory_id = int(_row_value(row, "memory_id", "id", default=0) or 0)
                canonical_fact = str(_row_value(row, "canonical_fact", default=content) or content)
                importance = float(_row_value(row, "importance", "weight", default=0.3) or 0.3)
                confidence = float(_row_value(row, "confidence", default=1.0) or 1.0)
                if is_group:
                    protocol, self_id, group_id = _group_coordinates(
                        payload,
                        default_protocol=group_protocol,
                        default_self_id=group_self_id,
                    )
                    result = adapter.create_group_memory(
                        preset=preset,
                        protocol=protocol,
                        self_id=self_id,
                        group_id=group_id,
                        content=content,
                        canonical_user_id=_row_value(row, "last_writer_person_id", "canonical_user_id"),
                        canonical_fact=canonical_fact,
                        weight=importance,
                        confidence=confidence,
                    )
                    scope = "group"
                    scope_kwargs = {
                        "protocol": protocol,
                        "self_id": self_id,
                        "group_id": group_id,
                    }
                else:
                    user_id = str(_row_value(row, "user_id", "person_id", "canonical_user_id", default="legacy") or "legacy")
                    result = adapter.create_memory(
                        canonical_user_id=user_id,
                        preset=preset,
                        content=content,
                        canonical_fact=canonical_fact,
                        weight=importance,
                        confidence=confidence,
                    )
                    scope = "person"
                    scope_kwargs = {}
                if result is None:
                    report["skipped"] += 1
                else:
                    if str(getattr(result, "outcome", "created")) == "unchanged":
                        report["unchanged"] += 1
                    else:
                        report["imported"] += 1
                        report["scopes"][scope] += 1
                    # Evidence is imported for both newly-created and
                    # already-present facts.  The adapter deduplicates by
                    # evidence fingerprint, so this remains idempotent.
                    if source_memory_id > 0:
                        for evidence in _evidence_rows(
                            connection,
                            table,
                            scope=scope,
                            memory_id=source_memory_id,
                        ):
                            excerpt = str(evidence.get("excerpt") or evidence.get("content") or "").strip()
                            if not excerpt:
                                continue
                            evidence_metadata = evidence.get("metadata_json", "{}")
                            try:
                                metadata = json.loads(str(evidence_metadata or "{}"))
                            except (TypeError, ValueError):
                                metadata = {}
                            if evidence.get("message_key") is not None:
                                metadata["message_key"] = evidence["message_key"]
                            adapter.add_scoped_memory_evidence(
                                scope=scope,
                                canonical_user_id=(
                                    user_id if scope == "person" else str(_row_value(row, "last_writer_person_id", "canonical_user_id", default="group-writer") or "group-writer")
                                ),
                                preset=preset,
                                memory_id=result.fact_id,
                                excerpt=excerpt,
                                conversation_pk=(
                                    int(evidence["conversation_ref"])
                                    if evidence.get("conversation_ref") is not None
                                    else None
                                ),
                                observed_at=(
                                    int(evidence["observed_at"])
                                    if evidence.get("observed_at") is not None
                                    else None
                                ),
                                metadata=metadata,
                                **scope_kwargs,
                            )
        adapter.close()
    finally:
        connection.close()
    report["completed_at"] = int(time.time())
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate Jianer legacy memory into Jianer Memory")
    parser.add_argument("source", type=Path, help="legacy Jianer SQLite database")
    parser.add_argument("--target-db", type=Path, default=Path("jianer_ai.db"))
    parser.add_argument("--data-dir", type=Path, default=Path("data/jianer_ai_memorix"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--group-protocol", default="legacy", help="protocol label for hashed legacy group refs")
    parser.add_argument("--group-self-id", default="legacy", help="bot/self label for hashed legacy group refs")
    args = parser.parse_args()
    print(
        json.dumps(
            migrate(
                args.source,
                args.target_db,
                args.data_dir,
                dry_run=args.dry_run,
                group_protocol=args.group_protocol,
                group_self_id=args.group_self_id,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
