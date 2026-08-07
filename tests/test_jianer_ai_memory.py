from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

from plugins.JianerAI.memory import (
    GeneratedMemory,
    IdentityAuthorizationError,
    JianerMemoryStore,
    MemoryConflictError,
    MemoryEvidence,
    MemoryMigrationRequiredError,
    MemoryStoreError,
    authorize,
    merge_identity,
    persona_partition_id,
)
from plugins.JianerAI.migration import LegacyMemoryMigrator


def _store(tmp_path: Path) -> JianerMemoryStore:
    return JianerMemoryStore(tmp_path / "normalized.db")


def _insert_memory(
    store: JianerMemoryStore,
    canonical: str,
    content: str,
    *,
    preset: str = "default",
    evidence: str | None = None,
):
    token = store.begin_generation(
        canonical_user_id=canonical, preset=preset
    )
    item = GeneratedMemory(
        content=content,
        weight=0.8,
        evidence=(
            (MemoryEvidence(content=evidence),) if evidence is not None else ()
        ),
    )
    return store.insert_generated_memories(token, (item,))


def _record(
    store: JianerMemoryStore,
    *,
    kind: str,
    conversation_id: str,
    message_id: str,
    user_id: str = "42",
    timestamp: int = 1_700_000_000,
    preset: str = "default",
):
    return store.record_transcript(
        protocol="onebot",
        self_id="bot-1",
        kind=kind,
        conversation_id=conversation_id,
        message_id=message_id,
        sender_protocol="onebot",
        sender_self_id="bot-1",
        sender_external_id=user_id,
        content=f"message {message_id}",
        timestamp=timestamp,
        preset=preset,
    )


def test_fresh_v5_has_only_prefixed_control_and_physical_content_tables(
    tmp_path: Path,
):
    store = _store(tmp_path)
    canonical = store.resolve_identity("onebot", "bot-1", "42")
    store.create_memory(
        canonical_user_id=canonical,
        preset="XingYu",
        canonical_fact="用户喜欢蓝莓蛋糕",
        content="我记得你一直很喜欢蓝莓蛋糕。",
    )
    _record(
        store,
        kind="group",
        conversation_id="2822554898",
        message_id="g-1",
        preset="XingYu",
    )
    _record(
        store,
        kind="private",
        conversation_id="42",
        message_id="u-1",
        preset="XingYu",
    )
    partition = store.ensure_persona_partition("XingYu")

    with sqlite3.connect(store.db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    forbidden = {
        "memory_facts",
        "memory_evidence",
        "memory_suppressions",
        "raw_transcript_messages",
        "persona_memory_partitions",
        "canonical_identities",
        "identity_aliases",
        "conversations",
        "session_settings",
        "memory_preferences",
    }
    assert not (forbidden & tables)
    assert {
        "sys_schema",
        "sys_personas",
        "sys_persona_partitions",
        "sys_chat_partitions",
        "sys_identities",
        "sys_identity_aliases",
        "sys_conversations",
        "cfg_conversation_settings",
        "cfg_memory_settings",
        "cfg_session_settings",
        "job_memory_reviews",
        "audit_memory_actions",
        "audit_migrations",
    }.issubset(tables)
    assert {
        partition.people_table,
        partition.groups_table,
        partition.evidence_table,
        partition.episodes_table,
        partition.suppressions_table,
    }.issubset(tables)
    assert partition.people_table == "mem_p0001_xingyu_people"
    chat_tables = {item.table_name for item in store.list_chat_partitions()}
    assert "chat_g000001_2822554898" in chat_tables
    assert "chat_u000002_qq42" in chat_tables


def test_non_numeric_conversation_ids_are_hashed_in_chat_table_names(
    tmp_path: Path,
):
    store = _store(tmp_path)
    external_id = "oc_sensitive-looking-feishu-chat-id"
    store.record_transcript(
        protocol="feishu",
        self_id="app-1",
        conversation_kind="private",
        conversation_id=external_id,
        message_id="fs-1",
        sender_protocol="feishu",
        sender_self_id="app-1",
        sender_external_id="ou-user",
        content="hello",
        preset="XingYu",
    )
    partition = store.list_chat_partitions()[0]
    expected_hash = hashlib.sha256(external_id.encode("utf-8")).hexdigest()[:8]
    assert partition.table_name == f"chat_u000001_feishu_{expected_hash}"
    assert external_id not in partition.table_name


def test_partition_numbers_do_not_skip_after_repeated_existing_writes(
    tmp_path: Path,
):
    store = _store(tmp_path)
    canonical = store.resolve_identity("onebot", "bot-1", "42")
    for index in range(8):
        store.create_memory(
            canonical_user_id=canonical,
            preset="XingYu",
            canonical_fact=f"稳定事实 {index}",
            content=f"我记得稳定事实 {index}。",
        )
        store.record_transcript(
            protocol="onebot",
            self_id="bot-1",
            conversation_kind="group",
            conversation_id="100",
            message_id=f"g-{index}",
            sender_canonical_id=canonical,
            content=f"message {index}",
            preset="XingYu",
        )
    second_persona = store.ensure_persona_partition("AnotherRole")
    store.record_transcript(
        protocol="onebot",
        self_id="bot-1",
        conversation_kind="private",
        conversation_id="42",
        message_id="u-1",
        sender_canonical_id=canonical,
        content="private message",
        preset="AnotherRole",
    )
    assert second_persona.people_table == "mem_p0002_anotherrole_people"
    assert [
        item.table_name for item in store.list_chat_partitions()
    ] == ["chat_g000001_100", "chat_u000002_qq42"]


def test_explicit_v4_to_v5_migration_deduplicates_mirror_and_drops_old_tables(
    tmp_path: Path,
):
    path = tmp_path / "v4.db"
    now = int(time.time())
    legacy_id = persona_partition_id("XingYu")
    prefix = f"persona_{legacy_id}"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            f"""
            PRAGMA foreign_keys=OFF;
            CREATE TABLE canonical_identities (
                canonical_id TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                merged_into TEXT
            );
            CREATE TABLE conversations (
                conversation_pk INTEGER PRIMARY KEY AUTOINCREMENT,
                protocol TEXT NOT NULL,
                self_id TEXT NOT NULL,
                conversation_kind TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL,
                UNIQUE(protocol, self_id, conversation_kind, conversation_id)
            );
            CREATE TABLE memory_facts (
                fact_id INTEGER PRIMARY KEY,
                canonical_id TEXT NOT NULL,
                preset_key TEXT NOT NULL,
                fact_fingerprint TEXT NOT NULL,
                content TEXT NOT NULL,
                weight REAL NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_confirmed_at INTEGER NOT NULL
            );
            CREATE TABLE memory_evidence (
                evidence_id INTEGER PRIMARY KEY,
                fact_id INTEGER NOT NULL,
                conversation_pk INTEGER,
                transcript_id INTEGER,
                content TEXT NOT NULL,
                observed_at INTEGER NOT NULL,
                evidence_fingerprint TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE TABLE memory_suppressions (
                canonical_id TEXT NOT NULL,
                preset_key TEXT NOT NULL,
                suppression_kind TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                content_snapshot TEXT NOT NULL,
                source_fact_id TEXT,
                reason TEXT NOT NULL,
                deleted_at INTEGER NOT NULL
            );
            CREATE TABLE raw_transcript_messages (
                transcript_id INTEGER PRIMARY KEY,
                conversation_pk INTEGER NOT NULL,
                message_key TEXT NOT NULL,
                external_message_id TEXT NOT NULL,
                sender_canonical_id TEXT,
                sender_protocol TEXT NOT NULL,
                sender_self_id TEXT NOT NULL,
                sender_external_id TEXT NOT NULL,
                preset_key TEXT NOT NULL,
                content TEXT NOT NULL,
                occurred_at INTEGER NOT NULL,
                message_type TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE persona_memory_partitions (
                preset_key TEXT PRIMARY KEY,
                partition_id TEXT NOT NULL,
                people_table TEXT NOT NULL,
                groups_table TEXT NOT NULL,
                episodes_table TEXT NOT NULL,
                suppressions_table TEXT NOT NULL
            );
            CREATE TABLE {prefix}_people (
                memory_id INTEGER PRIMARY KEY,
                canonical_id TEXT NOT NULL,
                content TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                weight REAL NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_confirmed_at INTEGER NOT NULL,
                origin TEXT NOT NULL
            );
            CREATE TABLE {prefix}_groups (
                memory_id INTEGER PRIMARY KEY,
                group_key TEXT NOT NULL,
                content TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                weight REAL NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_confirmed_at INTEGER NOT NULL,
                last_writer_canonical_id TEXT
            );
            CREATE TABLE {prefix}_episodes (
                episode_id INTEGER PRIMARY KEY,
                conversation_pk INTEGER NOT NULL,
                exchange_key TEXT NOT NULL,
                speaker_canonical_id TEXT NOT NULL,
                user_content TEXT NOT NULL,
                assistant_content TEXT NOT NULL,
                occurred_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE {prefix}_suppressions (
                scope TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                suppression_kind TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                content_snapshot TEXT NOT NULL,
                source_memory_id TEXT,
                reason TEXT NOT NULL,
                deleted_at INTEGER NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO canonical_identities VALUES(?, ?, ?, NULL)",
            ("qq:42", now, now),
        )
        conn.execute(
            "INSERT INTO conversations VALUES(1, 'onebot', 'bot-1', "
            "'group', '2822554898', ?, ?)",
            (now, now),
        )
        fact = "用户长期喜欢蓝莓蛋糕"
        fingerprint = "legacy-semantic-hash"
        conn.execute(
            "INSERT INTO memory_facts VALUES(1, 'qq:42', 'XingYu', ?, ?, "
            "0.8, ?, ?, ?)",
            (fingerprint, fact, now, now, now),
        )
        conn.execute(
            f"INSERT INTO {prefix}_people VALUES(1, 'qq:42', ?, ?, 0.8, "
            "?, ?, ?, 'mirror')",
            (fact, fingerprint, now, now, now),
        )
        conn.execute(
            "INSERT INTO memory_evidence VALUES(1, 1, 1, 1, ?, ?, ?, '{}')",
            ("用户明确说过", now, "evidence-hash"),
        )
        conn.execute(
            "INSERT INTO memory_suppressions VALUES('qq:42', 'XingYu', "
            "'fact', 'deleted-hash', '旧记忆', '9', 'user_deleted', ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO raw_transcript_messages VALUES(1, 1, 'id:m1', "
            "'m1', 'qq:42', 'onebot', 'bot-1', '42', 'XingYu', ?, ?, "
            "'text', 'payload-hash', ?)",
            ("最近的群消息", now, now),
        )
        conn.execute(
            "INSERT INTO persona_memory_partitions VALUES(?, ?, ?, ?, ?, ?)",
            (
                "XingYu",
                legacy_id,
                f"{prefix}_people",
                f"{prefix}_groups",
                f"{prefix}_episodes",
                f"{prefix}_suppressions",
            ),
        )

    store = JianerMemoryStore(path, initialize=False)
    backup = store.migrate_to_v5()
    assert backup is not None and backup.is_file()
    migrated = JianerMemoryStore(path)
    records = migrated.list_memories(
        canonical_user_id="qq:42",
        preset="XingYu",
    )
    assert len(records) == 1
    assert records[0].content == "用户长期喜欢蓝莓蛋糕"
    assert len(records[0].evidence) == 1
    assert len(
        migrated.list_suppressions(
            canonical_user_id="qq:42",
            preset="XingYu",
        )
    ) == 1
    assert migrated.count_transcripts(conversation_id="2822554898") == 1
    assert migrated.quick_check() == ("ok",)
    assert migrated.foreign_key_check() == ()
    with sqlite3.connect(path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        audit = conn.execute(
            "SELECT status, counts_json, verification_json "
            "FROM audit_migrations WHERE migration_key='schema-v5'"
        ).fetchone()
    assert "memory_facts" not in tables
    assert "memory_evidence" not in tables
    assert "memory_suppressions" not in tables
    assert "raw_transcript_messages" not in tables
    assert f"{prefix}_people" not in tables
    assert audit[0] == "completed"
    counts = json.loads(audit[1])
    verification = json.loads(audit[2])
    assert counts["source"]["people_unique"] == 1
    assert counts["source"]["recent_chat_unique"] == 1
    assert counts["target"]["people"] == 1
    assert counts["target"]["chat"] == 1
    assert verification["issues"] == []
    assert verification["comparisons"]["recent_chat"]["equal"] is True
    with sqlite3.connect(backup) as conn:
        backup_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "memory_facts" in backup_tables
    assert "raw_transcript_messages" in backup_tables


def test_v5_migration_refuses_legacy_foreign_key_corruption(tmp_path: Path):
    path = tmp_path / "foreign-key-corrupt-v4.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys=OFF;
            CREATE TABLE schema_meta (
                schema_name TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            INSERT INTO schema_meta VALUES('jianer_ai_memory', 4, 1);
            CREATE TABLE canonical_identities (
                canonical_id TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                merged_into TEXT
            );
            CREATE TABLE memory_facts (
                fact_id INTEGER PRIMARY KEY,
                canonical_id TEXT NOT NULL REFERENCES canonical_identities(canonical_id),
                preset_key TEXT NOT NULL,
                fact_fingerprint TEXT NOT NULL,
                content TEXT NOT NULL,
                weight REAL NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_confirmed_at INTEGER NOT NULL
            );
            INSERT INTO memory_facts VALUES(
                1, 'qq:missing', 'XingYu', 'hash', 'orphan', 1.0, 1, 1, 1
            );
            """
        )
    with pytest.raises(MemoryStoreError, match="foreign_key_check"):
        JianerMemoryStore(path, initialize=False).migrate_to_v5()
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM memory_facts"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT version FROM schema_meta"
        ).fetchone()[0] == 4


def test_failed_v5_staging_keeps_backup_and_failure_audit(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "failed-stage-v4.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_meta (
                schema_name TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            INSERT INTO schema_meta VALUES('jianer_ai_memory', 4, 1);
            """
        )
    original_check = JianerMemoryStore.foreign_key_check

    def force_staged_failure(store):
        if ".v5-staging-" in store.db_path.name:
            return (("forced-staging-error",),)
        return original_check(store)

    monkeypatch.setattr(
        JianerMemoryStore,
        "foreign_key_check",
        force_staged_failure,
    )
    with pytest.raises(MemoryStoreError, match="staged v5"):
        JianerMemoryStore(path, initialize=False).migrate_to_v5()

    backups = tuple(tmp_path.glob("failed-stage-v4.db.v4-backup-*"))
    staging = tuple(tmp_path.glob("failed-stage-v4.db.v5-staging-*"))
    assert len(backups) == 1
    assert len(staging) == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version FROM schema_meta").fetchone()[0] == 4
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='sys_schema'"
        ).fetchone()[0] == 0
    with sqlite3.connect(staging[0]) as conn:
        audit = conn.execute(
            "SELECT status, backup_path, error FROM audit_migrations "
            "WHERE migration_key='schema-v5'"
        ).fetchone()
    assert audit[0] == "failed"
    assert Path(audit[1]) == backups[0]
    assert "forced-staging-error" in audit[2]


def _create_legacy_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE memory_settings (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                is_private INTEGER NOT NULL,
                enabled INTEGER NOT NULL,
                interval_seconds INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (group_id, user_id, is_private)
            );
            CREATE TABLE memory_state (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                is_private INTEGER NOT NULL,
                preset_key TEXT NOT NULL,
                raw_table TEXT NOT NULL,
                last_seq INTEGER NOT NULL,
                last_generated_at INTEGER NOT NULL,
                PRIMARY KEY (group_id, user_id, is_private, preset_key)
            );
            CREATE TABLE mem_global (
                memory_id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_content TEXT NOT NULL,
                generated_at INTEGER NOT NULL,
                weight REAL NOT NULL
            );
            CREATE TABLE raw_g100 (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT,
                sender TEXT,
                content TEXT,
                timestamp INTEGER,
                message_type TEXT
            );
            CREATE TABLE raw_p_u77 (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT,
                sender TEXT,
                content TEXT,
                timestamp INTEGER,
                message_type TEXT
            );
            CREATE TABLE mem_p_u77_pdefault (
                memory_id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                memory_content TEXT NOT NULL,
                generated_at INTEGER NOT NULL,
                weight REAL NOT NULL
            );
            CREATE TABLE mem_g100_pdefault (
                memory_id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                memory_content TEXT NOT NULL,
                generated_at INTEGER NOT NULL,
                weight REAL NOT NULL
            );
            """
        )
        conn.executemany(
            """
            INSERT INTO raw_g100(
                message_id, sender, content, timestamp, message_type
            )
            VALUES(?, ?, ?, ?, 'group')
            """,
            (
                ("g-1", "77", "group one", 1_700_000_001),
                ("g-2", "88", "group two", 1_700_000_002),
            ),
        )
        conn.execute(
            """
            INSERT INTO raw_p_u77(
                message_id, sender, content, timestamp, message_type
            )
            VALUES('p-1', '77', 'private one', 1700000003, 'private')
            """
        )
        conn.executemany(
            """
            INSERT INTO memory_settings(
                group_id,
                user_id,
                is_private,
                enabled,
                interval_seconds,
                updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                ("100", "0", 0, 1, 3600, 1_700_000_100),
                ("p", "77", 1, 1, 7200, 1_700_000_100),
            ),
        )
        conn.execute(
            """
            INSERT INTO memory_state(
                group_id,
                user_id,
                is_private,
                preset_key,
                raw_table,
                last_seq,
                last_generated_at
            )
            VALUES('p', '77', 1, 'default', 'raw_p_u77', 1, 1700000200)
            """
        )
        conn.execute(
            """
            INSERT INTO mem_p_u77_pdefault(
                group_id,
                user_id,
                memory_content,
                generated_at,
                weight
            )
            VALUES('p', '77', '用户喜欢咖啡', 1700000100, 0.9)
            """
        )
        conn.execute(
            """
            INSERT INTO mem_g100_pdefault(
                group_id,
                user_id,
                memory_content,
                generated_at,
                weight
            )
            VALUES('100', '0', '本群每周聚会', 1700000100, 0.7)
            """
        )
        conn.execute(
            """
            INSERT INTO mem_global(memory_content, generated_at, weight)
            VALUES('没有用户归属的旧全局事实', 1700000100, 0.5)
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_list_conversation_sender_ids_is_scoped_and_recent_first(tmp_path: Path):
    store = _store(tmp_path)
    _record(
        store,
        kind="group",
        conversation_id="group-1",
        message_id="old",
        user_id="24680",
        timestamp=100,
    )
    _record(
        store,
        kind="group",
        conversation_id="group-1",
        message_id="new",
        user_id="13579",
        timestamp=200,
    )
    _record(
        store,
        kind="group",
        conversation_id="group-2",
        message_id="other-group",
        user_id="99999",
        timestamp=300,
    )

    assert store.list_conversation_sender_ids(
        protocol="onebot",
        self_id="bot-1",
        conversation_kind="group",
        conversation_id="group-1",
    ) == ("13579", "24680")


def test_qq_aliases_share_fixed_canonical_identity(tmp_path: Path):
    store = _store(tmp_path)

    onebot = store.resolve_identity("onebot", "bot-a", "123456")
    milky = store.resolve_identity("milky", "bot-b", "123456")

    assert onebot == milky == "qq:123456"
    assert {alias.protocol for alias in store.aliases_for(onebot)} == {
        "onebot",
        "milky",
    }


def test_group_transcript_is_stored_once_across_presets(tmp_path: Path):
    store = _store(tmp_path)

    first = _record(
        store,
        kind="group",
        conversation_id="100",
        message_id="same",
        preset="default",
    )
    second = _record(
        store,
        kind="group",
        conversation_id="100",
        message_id="same",
        preset="role-a",
    )

    assert first.inserted is True
    assert second.inserted is False
    assert second.transcript_id == first.transcript_id
    assert store.count_transcripts(
        protocol="onebot",
        self_id="bot-1",
        conversation_kind="group",
        conversation_id="100",
    ) == 1
    assert store.count_transcripts(preset="default") == 1
    assert store.count_transcripts(preset="role-a") == 0


def test_chat_retention_is_90_days_for_group_and_private(
    tmp_path: Path,
):
    store = _store(tmp_path)
    now = 2_000_000_000
    old = now - (91 * 86400)
    fresh = now - (89 * 86400)
    _record(
        store,
        kind="group",
        conversation_id="100",
        message_id="old-group",
        timestamp=old,
    )
    _record(
        store,
        kind="group",
        conversation_id="100",
        message_id="fresh-group",
        timestamp=fresh,
    )
    _record(
        store,
        kind="private",
        conversation_id="42",
        message_id="old-private",
        timestamp=old,
    )

    assert store.purge_transcripts(now=now) == 2
    assert store.count_transcripts(conversation_kind="group") == 1
    assert store.count_transcripts(conversation_kind="private") == 0


def test_session_defaults_and_preset_settings_are_isolated(tmp_path: Path):
    store = _store(tmp_path)
    group = store.get_session_settings(
        protocol="onebot",
        self_id="bot-1",
        conversation_kind="group",
        conversation_id="100",
        preset="default",
    )
    private = store.get_session_settings(
        protocol="onebot",
        self_id="bot-1",
        conversation_kind="private",
        conversation_id="42",
        preset="default",
    )
    role = store.set_session_settings(
        protocol="onebot",
        self_id="bot-1",
        kind="group",
        conversation_id="100",
        preset="role-a",
        model="model-b",
        tts=False,
        enabled=True,
        interval=600,
    )

    assert group.tts_enabled is True
    assert private.tts_enabled is False
    assert role.model == "model-b"
    assert role.tts_enabled is False
    assert role.memory_interval_seconds == 600
    assert role.session_id != group.session_id


def test_active_preset_persists_independently_from_session_rows(
    tmp_path: Path,
):
    store = _store(tmp_path)
    assert store.get_active_preset(
        protocol="onebot",
        self_id="bot-1",
        conversation_kind="group",
        conversation_id="100",
    ) is None

    assert store.set_active_preset(
        protocol="onebot",
        self_id="bot-1",
        conversation_kind="group",
        conversation_id="100",
        preset="role-a",
    ) == "role-a"
    assert store.get_active_preset(
        protocol="onebot",
        self_id="bot-1",
        conversation_kind="group",
        conversation_id="100",
    ) == "role-a"


def test_memory_preferences_are_user_preset_scoped_and_due_across_sessions(
    tmp_path: Path,
):
    store = _store(tmp_path)
    canonical = store.resolve_identity("onebot", "bot-1", "42")
    _record(
        store,
        kind="group",
        conversation_id="100",
        message_id="due-message",
        preset="role-a",
    )
    settings = store.set_memory_settings(
        canonical_user_id=canonical,
        preset="role-a",
        enabled=True,
        interval_seconds=60,
    )

    assert settings["enabled"] is True
    assert settings["interval_seconds"] == 60
    due = store.list_due_memory_scopes(now=2_000_000_000)
    role_scope = next(scope for scope in due if scope.preset == "role-a")
    assert role_scope.canonical_user_id == canonical
    assert role_scope.conversation_kind == "group"
    assert role_scope.conversation_id == "100"


def test_generation_batches_strictly_filter_preset_globally_and_per_session(
    tmp_path: Path,
):
    store = _store(tmp_path)
    canonical = store.resolve_identity("onebot", "bot-1", "42")
    role_a = _record(
        store,
        kind="group",
        conversation_id="100",
        message_id="role-a-message",
        preset="role-a",
    )
    role_b = _record(
        store,
        kind="group",
        conversation_id="100",
        message_id="role-b-message",
        preset="role-b",
    )

    global_a = store.fetch_generation_batch(
        canonical_user_id=canonical,
        preset="role-a",
    )
    assert global_a is not None
    assert [row.id for row in global_a.rows] == [str(role_a.transcript_id)]
    store.insert_generated_memories(
        global_a.token,
        ("role A fact",),
        last_transcript_id=global_a.last_transcript_id,
    )
    assert store.fetch_generation_batch(
        canonical_user_id=canonical,
        preset="role-a",
    ) is None

    global_b = store.fetch_generation_batch(
        canonical_user_id=canonical,
        preset="role-b",
    )
    assert global_b is not None
    assert [row.id for row in global_b.rows] == [str(role_b.transcript_id)]

    session_a = store.fetch_generation_batch(
        canonical_user_id=canonical,
        protocol="onebot",
        self_id="bot-1",
        kind="group",
        conversation_id="100",
        preset="role-a",
    )
    session_b = store.fetch_generation_batch(
        canonical_user_id=canonical,
        protocol="onebot",
        self_id="bot-1",
        kind="group",
        conversation_id="100",
        preset="role-b",
    )
    assert session_a is not None
    assert session_b is not None
    assert [row.id for row in session_a.rows] == [str(role_a.transcript_id)]
    assert [row.id for row in session_b.rows] == [str(role_b.transcript_id)]


def test_due_scopes_do_not_borrow_raw_messages_from_another_preset(
    tmp_path: Path,
):
    store = _store(tmp_path)
    canonical = store.resolve_identity("onebot", "bot-1", "42")
    _record(
        store,
        kind="private",
        conversation_id="42",
        message_id="only-a",
        preset="role-a",
    )
    store.set_memory_settings(
        canonical_user_id=canonical,
        preset="role-b",
        enabled=True,
        interval_seconds=60,
    )

    due = store.list_due_memory_scopes(now=2_000_000_000)
    assert {scope.preset for scope in due} == {"role-a"}

    _record(
        store,
        kind="private",
        conversation_id="42",
        message_id="now-b",
        preset="role-b",
    )
    due = store.list_due_memory_scopes(now=2_000_000_000)
    assert {scope.preset for scope in due} == {"role-a", "role-b"}


def test_long_memory_shares_across_conversations_but_not_presets(
    tmp_path: Path,
):
    store = _store(tmp_path)
    group_identity = store.resolve_identity("onebot", "bot-a", "42")
    private_identity = store.resolve_identity("milky", "bot-b", "42")
    assert group_identity == private_identity

    assert _insert_memory(
        store,
        group_identity,
        "用户喜欢蓝莓蛋糕",
        preset="default",
    ).inserted == 1
    assert _insert_memory(
        store,
        group_identity,
        "用户喜欢爵士乐",
        preset="role-a",
    ).inserted == 1

    default_items = store.query_memories(
        canonical_user_id=private_identity,
        preset="default",
        query="蓝莓",
        limit=5,
    )
    role_items = store.list_memories(
        canonical_user_id=private_identity,
        preset="role-a",
    )

    assert [item.content for item in default_items] == ["用户喜欢蓝莓蛋糕"]
    assert [item.content for item in role_items] == ["用户喜欢爵士乐"]


def test_persona_memory_is_physically_partitioned_by_people_groups_and_episodes(
    tmp_path: Path,
):
    store = _store(tmp_path)
    alice = store.resolve_identity("onebot", "bot-1", "42")
    bob = store.resolve_identity("onebot", "bot-1", "84")

    role_a = store.ensure_persona_partition("role-a")
    role_b = store.ensure_persona_partition("role-b")
    assert role_a.partition_id != role_b.partition_id
    assert role_a.people_table != role_b.people_table
    assert role_a.groups_table != role_b.groups_table
    assert role_a.episodes_table != role_b.episodes_table
    assert "role-a" not in role_a.people_table

    store.create_memory(
        canonical_user_id=alice,
        preset="role-a",
        content="我总会记得她偏爱蓝莓蛋糕呀。",
    )
    store.create_memory(
        canonical_user_id=alice,
        preset="role-b",
        content="我已理性记录：她偏爱蓝莓蛋糕。",
    )
    store.create_group_memory(
        preset="role-a",
        protocol="onebot",
        self_id="bot-1",
        group_id="group-100",
        canonical_user_id=alice,
        content="我记得这个群每周五会一起看电影呀。",
    )
    store.create_group_memory(
        preset="role-a",
        protocol="onebot",
        self_id="bot-1",
        group_id="group-200",
        canonical_user_id=bob,
        content="我记得另一个群周末会讨论音乐。",
    )
    store.record_conversation_episode(
        preset="role-a",
        protocol="onebot",
        self_id="bot-1",
        conversation_kind="group",
        conversation_id="group-100",
        speaker_canonical_id=alice,
        exchange_id="message-1",
        user_content="周五的电影改成星际穿越吧",
        assistant_content="好呀，我会记得我们周五看星际穿越。",
        occurred_at=1_800_000_000,
    )
    store.record_conversation_episode(
        preset="role-b",
        protocol="onebot",
        self_id="bot-1",
        conversation_kind="group",
        conversation_id="group-100",
        speaker_canonical_id=alice,
        exchange_id="message-1",
        user_content="这是另一个人设的讨论",
        assistant_content="该片段只属于 role-b。",
        occurred_at=1_800_000_001,
    )
    store.record_conversation_episode(
        preset="role-a",
        protocol="onebot",
        self_id="bot-1",
        conversation_kind="private",
        conversation_id="42",
        speaker_canonical_id=alice,
        exchange_id="private-message-1",
        user_content="私聊里我们谈过我的旅行计划",
        assistant_content="我会记住你的旅行计划呀。",
        occurred_at=1_800_000_002,
    )

    assert [
        item.content
        for item in store.list_memories(
            canonical_user_id=alice,
            preset="role-a",
        )
    ] == ["我总会记得她偏爱蓝莓蛋糕呀。"]
    assert store.list_memories(
        canonical_user_id=bob,
        preset="role-a",
    ) == ()
    assert [
        item.content
        for item in store.list_group_memories(
            preset="role-a",
            protocol="onebot",
            self_id="bot-1",
            group_id="group-100",
        )
    ] == ["我记得这个群每周五会一起看电影呀。"]
    assert [
        item.content
        for item in store.list_group_memories(
            preset="role-a",
            protocol="onebot",
            self_id="bot-1",
            group_id="group-200",
        )
    ] == ["我记得另一个群周末会讨论音乐。"]
    episodes = store.query_conversation_episodes(
        preset="role-a",
        protocol="onebot",
        self_id="bot-1",
        conversation_kind="group",
        conversation_id="group-100",
        query="星际穿越",
    )
    assert [(item.user_content, item.assistant_content) for item in episodes] == [
        (
            "周五的电影改成星际穿越吧",
            "好呀，我会记得我们周五看星际穿越。",
        )
    ]
    private_episodes = store.query_conversation_episodes(
        preset="role-a",
        protocol="milky",
        self_id="bot-2",
        conversation_kind="private",
        conversation_id="42",
        speaker_canonical_id=alice,
        query="旅行计划",
    )
    assert [item.user_content for item in private_episodes] == [
        "私聊里我们谈过我的旅行计划"
    ]

    with sqlite3.connect(store.db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            role_a.people_table,
            role_a.groups_table,
            role_a.episodes_table,
            role_a.suppressions_table,
            role_b.people_table,
            role_b.groups_table,
            role_b.episodes_table,
            role_b.suppressions_table,
        }.issubset(tables)
        assert conn.execute(
            f"SELECT COUNT(*) FROM {role_a.people_table}"
        ).fetchone()[0] == 1
        assert conn.execute(
            f"SELECT COUNT(*) FROM {role_a.groups_table}"
        ).fetchone()[0] == 2
        assert conn.execute(
            f"SELECT COUNT(*) FROM {role_a.episodes_table}"
        ).fetchone()[0] == 2
        assert conn.execute(
            f"SELECT COUNT(*) FROM {role_b.episodes_table}"
        ).fetchone()[0] == 1
    assert store.foreign_key_check() == ()


def test_v5_memories_persist_in_readable_persona_physical_tables(
    tmp_path: Path,
):
    path = tmp_path / "v3-memory.db"
    store = JianerMemoryStore(path)
    canonical = store.resolve_identity("onebot", "bot", "42")
    created = store.create_memory(
        canonical_user_id=canonical,
        preset="role-a",
        content="我记得这个旧版本偏好。",
    )
    partition = store.ensure_persona_partition("role-a")

    with sqlite3.connect(path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert partition.evidence_table in tables
        assert partition.people_table.startswith("mem_p0001_role_a_")
        assert "memory_facts" not in tables
        assert "memory_evidence" not in tables
        assert "memory_suppressions" not in tables
        assert "raw_transcript_messages" not in tables

    migrated = JianerMemoryStore(path)
    migrated_partition = migrated.list_persona_partitions()[0]
    assert migrated_partition.preset == "role-a"
    records = migrated.list_memories(
        canonical_user_id=canonical,
        preset="role-a",
    )
    assert [(item.fact_id, item.content) for item in records] == [
        (created.fact_id, "我记得这个旧版本偏好。")
    ]
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            f"SELECT COUNT(*) FROM {migrated_partition.people_table}"
        ).fetchone()[0] == 1


def test_direct_memory_create_and_update_are_scoped_and_barrier_safe(
    tmp_path: Path,
):
    store = _store(tmp_path)
    canonical = store.resolve_identity("onebot", "bot", "42")
    other = store.resolve_identity("onebot", "bot", "84")
    stale_create = store.begin_generation(
        canonical_user_id=canonical,
        preset="role-a",
    )

    created = store.create_memory(
        canonical_user_id=canonical,
        preset="role-a",
        content="用户喜欢蓝莓蛋糕",
    )
    assert created.outcome == "inserted"
    assert created.content == "用户喜欢蓝莓蛋糕"
    assert created.weight == 1.0
    confirmed = store.create_memory(
        canonical_user_id=canonical,
        preset="role-a",
        content=" 用户喜欢蓝莓蛋糕 ",
    )
    assert confirmed.fact_id == created.fact_id
    assert confirmed.outcome == "updated"
    assert store.insert_generated_memories(
        stale_create,
        ("旧提炼结果不应写入",),
    ).stale_generation is True

    assert store.update_memory(
        canonical_user_id=other,
        preset="role-a",
        memory_id=created.memory_id,
        content="不允许修改别人的记忆",
    ) is None
    assert store.update_memory(
        canonical_user_id=canonical,
        preset="other-role",
        memory_id=created.memory_id,
        content="不允许跨角色修改记忆",
    ) is None

    stale_update = store.begin_generation(
        canonical_user_id=canonical,
        preset="role-a",
    )
    updated = store.update_memory(
        canonical_user_id=canonical,
        preset="role-a",
        memory_id=created.memory_id,
        content="用户喜欢草莓蛋糕",
    )
    assert updated is not None
    assert updated.fact_id == created.fact_id
    assert updated.outcome == "updated"
    assert store.insert_generated_memories(
        stale_update,
        ("另一个旧提炼结果不应写入",),
    ).stale_generation is True
    records = store.list_memories(
        canonical_user_id=canonical,
        preset="role-a",
    )
    assert [(item.fact_id, item.content) for item in records] == [
        (created.fact_id, "用户喜欢草莓蛋糕")
    ]
    assert {
        (item["suppression_kind"], item["content_snapshot"])
        for item in store.list_suppressions(
            canonical_user_id=canonical,
            preset="role-a",
        )
    } == {("fact", "用户喜欢蓝莓蛋糕")}

    duplicate = store.create_memory(
        canonical_user_id=canonical,
        preset="role-a",
        content="用户喜欢爵士乐",
    )
    with pytest.raises(MemoryConflictError):
        store.update_memory(
            canonical_user_id=canonical,
            preset="role-a",
            memory_id=updated.memory_id,
            content="用户喜欢爵士乐",
        )
    assert {
        item.fact_id for item in store.list_memories(
            canonical_user_id=canonical,
            preset="role-a",
        )
    } == {updated.fact_id, duplicate.fact_id}
    assert store.foreign_key_check() == ()


def test_deleted_memory_cannot_return_from_inflight_or_fresh_generation(
    tmp_path: Path,
):
    store = _store(tmp_path)
    canonical = store.resolve_identity("onebot", "bot", "42")
    evidence = "用户说：我最喜欢蓝莓蛋糕"
    created = _insert_memory(
        store, canonical, "用户喜欢蓝莓蛋糕", evidence=evidence
    )
    assert created.inserted == 1
    fact = store.list_memories(canonical_user_id=canonical)[0]

    inflight = store.begin_generation(canonical_user_id=canonical)
    assert store.delete_memory(
        canonical_user_id=canonical, fact_id=fact.fact_id
    )
    stale = store.insert_generated_memories(
        inflight,
        (
            GeneratedMemory(
                content="用户喜欢蓝莓蛋糕",
                evidence=(MemoryEvidence(content=evidence),),
            ),
        ),
    )
    assert stale.accepted is False
    assert stale.stale_generation is True

    fresh = store.begin_generation(canonical_user_id=canonical)
    suppressed = store.insert_generated_memories(
        fresh,
        (
            GeneratedMemory(
                content="该用户偏爱蓝莓口味甜点",
                evidence=(MemoryEvidence(content=evidence),),
            ),
        ),
    )
    assert suppressed.accepted is True
    assert suppressed.inserted == 0
    assert suppressed.skipped_suppressed == 1
    assert store.list_memories(canonical_user_id=canonical) == ()
    kinds = {item["suppression_kind"] for item in store.list_suppressions(
        canonical_user_id=canonical
    )}
    assert kinds == {"fact", "evidence"}


def test_tombstones_are_preset_isolated_and_can_be_restored(tmp_path: Path):
    store = _store(tmp_path)
    canonical = store.resolve_identity("onebot", "bot", "42")
    content = "用户养了一只黑猫"
    _insert_memory(store, canonical, content, preset="default")
    fact = store.list_memories(
        canonical_user_id=canonical, preset="default"
    )[0]
    store.delete_memory(
        canonical_user_id=canonical,
        preset="default",
        fact_id=fact.fact_id,
    )

    role_insert = _insert_memory(
        store, canonical, content, preset="role-a"
    )
    assert role_insert.inserted == 1
    assert store.restore_memory(
        canonical_user_id=canonical,
        preset="default",
        content=content,
    )
    default_insert = _insert_memory(
        store, canonical, content, preset="default"
    )
    assert default_insert.inserted == 1


def test_feishu_merge_requires_authorization_and_is_idempotent(
    tmp_path: Path,
):
    store = _store(tmp_path)
    feishu = store.resolve_identity("feishu", "app-1", "ou-user")
    qq = store.resolve_identity("onebot", "bot-1", "42")
    assert feishu != qq
    _insert_memory(store, feishu, "用户偏好无糖饮料")

    with pytest.raises(IdentityAuthorizationError):
        merge_identity(
            store,
            source_protocol="feishu",
            source_self_id="app-1",
            source_external_id="ou-user",
            target_external_id="42",
        )

    assert authorize(
        store,
        protocol="feishu",
        self_id="app-1",
        external_id="ou-user",
        canonical_user_id="qq:42",
    )
    assert authorize(
        store,
        protocol="feishu",
        self_id="app-1",
        external_id="ou-user",
        canonical_user_id="qq:42",
    )
    assert merge_identity(
        store,
        source_protocol="feishu",
        source_self_id="app-1",
        source_external_id="ou-user",
        target_external_id="42",
    )
    assert merge_identity(
        store,
        source_protocol="feishu",
        source_self_id="app-1",
        source_external_id="ou-user",
        target_external_id="42",
    )

    assert store.resolve_identity("feishu", "app-1", "ou-user") == "qq:42"
    assert [
        item.content
        for item in store.list_memories(canonical_user_id="qq:42")
    ] == ["用户偏好无糖饮料"]
    conn = sqlite3.connect(store.db_path)
    try:
        assert conn.execute(
                "SELECT COUNT(*) FROM sys_identity_authorizations"
        ).fetchone()[0] == 1
        assert conn.execute(
                "SELECT COUNT(*) FROM audit_identity_merges"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_feishu_rebind_never_merges_two_qq_identities(tmp_path: Path):
    store = _store(tmp_path)
    qq_a = store.resolve_identity("onebot", "bot-1", "42")
    qq_b = store.resolve_identity("milky", "bot-1", "77")
    _insert_memory(store, qq_a, "QQ A 的既有记忆")

    assert store.authorize(
        protocol="feishu",
        self_id="app-1",
        external_id="ou-user",
        canonical_user_id=qq_a,
        reason="feishu_binding",
    )
    assert store.merge_identity(
        source_protocol="feishu",
        source_self_id="app-1",
        source_external_id="ou-user",
        target_protocol="qq",
        target_self_id="",
        target_external_id="42",
        reason="feishu_binding",
    )
    assert store.resolve_identity("feishu", "app-1", "ou-user") == qq_a

    assert store.authorize(
        protocol="feishu",
        self_id="app-1",
        external_id="ou-user",
        canonical_user_id=qq_b,
        reason="feishu_binding",
    )
    assert store.merge_identity(
        source_protocol="feishu",
        source_self_id="app-1",
        source_external_id="ou-user",
        target_protocol="qq",
        target_self_id="",
        target_external_id="77",
        reason="feishu_binding",
    )

    assert store.resolve_identity("feishu", "app-1", "ou-user") == qq_b
    assert store.resolve_identity("onebot", "bot-1", "42") == qq_a
    assert store.resolve_identity("milky", "bot-1", "77") == qq_b
    assert qq_a != qq_b
    assert [
        item.content
        for item in store.list_memories(canonical_user_id=qq_a)
    ] == ["QQ A 的既有记忆"]
    assert store.list_memories(canonical_user_id=qq_b) == ()

    conn = sqlite3.connect(store.db_path)
    try:
        active_targets = {
            row[0]
            for row in conn.execute(
                """
                SELECT target_canonical_id
                    FROM sys_identity_authorizations
                WHERE source_protocol='feishu'
                  AND source_self_id='app-1'
                  AND source_external_id='ou-user'
                  AND reason='feishu_binding'
                  AND revoked_at IS NULL
                """
            )
        }
        outcome = conn.execute(
            """
            SELECT outcome
                FROM audit_identity_merges
            WHERE target_external_id='77'
            """
        ).fetchone()[0]
    finally:
        conn.close()
    assert active_targets == {qq_b}
    assert outcome == "alias_reassigned"


def test_generation_batch_progress_prevents_reprocessing(tmp_path: Path):
    store = _store(tmp_path)
    canonical = store.resolve_identity("onebot", "bot-1", "42")
    _record(
        store,
        kind="group",
        conversation_id="100",
        message_id="m-1",
    )
    _record(
        store,
        kind="group",
        conversation_id="100",
        message_id="m-2",
    )
    batch = store.fetch_generation_batch(
        canonical_user_id=canonical,
        protocol="onebot",
        self_id="bot-1",
        kind="group",
        conversation_id="100",
    )
    assert batch is not None
    assert len(batch.messages) == 2
    result = store.insert_generated_memories(
        batch.token,
        ("用户参与了群聊",),
        session_id=batch.session_id,
        last_transcript_id=batch.last_transcript_id,
    )
    assert result.accepted
    assert store.fetch_generation_batch(
        canonical_user_id=canonical,
        protocol="onebot",
        self_id="bot-1",
        kind="group",
        conversation_id="100",
    ) is None


def test_service_compatible_generation_and_memory_id_restore(tmp_path: Path):
    store = _store(tmp_path)
    canonical = store.resolve_identity("onebot", "bot-1", "42")
    write = store.record_transcript(
        protocol="onebot",
        self_id="bot-1",
        conversation_kind="private",
        conversation_id="42",
        message_id="service-message",
        sender_canonical_id=canonical,
        content="我喜欢喝黑咖啡",
        timestamp=1_700_000_000,
        preset="default",
    )
    assert bool(write)
    batch = store.fetch_generation_batch(
        canonical_user_id=canonical,
        preset="default",
        min_rows=1,
        limit=200,
    )
    assert batch is not None
    assert batch.rows[0].id == str(write.transcript_id)
    evidence_fingerprint = "a" * 64
    inserted = store.insert_generated_memories(
        canonical_user_id=canonical,
        preset="default",
        memories=[
            {
                "content": "用户喜欢喝黑咖啡",
                "weight": 0.9,
                "evidence_fingerprint": evidence_fingerprint,
            }
        ],
        generation_barrier=batch.barrier_token,
        transcript_ids=[row.id for row in batch.rows],
    )
    assert int(inserted) == 1
    status = store.get_memory_status(
        canonical_user_id=canonical, preset="default"
    )
    assert status["memory_count"] == 1
    assert status["new_raw_count"] == 0

    record = store.list_memories(canonical_user_id=canonical)[0]
    assert record.id == record.memory_id == str(record.fact_id)
    assert store.delete_memory(
        canonical_user_id=canonical,
        memory_id=record.memory_id,
    )
    tombstones = store.list_suppressions(canonical_user_id=canonical)
    assert {item["memory_id"] for item in tombstones} == {record.memory_id}
    assert store.restore_memory(
        canonical_user_id=canonical,
        memory_id=record.memory_id,
    )
    assert store.list_suppressions(canonical_user_id=canonical) == ()


def test_migration_dry_run_is_complete_and_does_not_touch_target(
    tmp_path: Path,
):
    legacy = tmp_path / "legacy.db"
    target = tmp_path / "target.db"
    _create_legacy_database(legacy)
    migrator = LegacyMemoryMigrator(target)

    plan = migrator.plan(legacy)
    report = migrator.dry_run(legacy)

    assert plan.source_quick_check == ("ok",)
    assert plan.table_counts["raw_g100"] == 2
    assert report.verified is True
    assert report.dry_run is True
    assert report.imported_counts == {
        "raw_transcripts": 3,
        "session_settings": 2,
        "memory_facts": 1,
    }
    assert report.quarantined_count == 3
    assert target.exists() is False


def test_normal_startup_refuses_legacy_content_until_explicit_migration(
    tmp_path: Path,
):
    database = tmp_path / "pre-preset-schema.db"
    conn = sqlite3.connect(database)
    try:
        conn.executescript(
            """
            CREATE TABLE raw_transcript_messages (
                transcript_id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_pk INTEGER NOT NULL,
                message_key TEXT NOT NULL,
                external_message_id TEXT NOT NULL,
                sender_canonical_id TEXT,
                sender_protocol TEXT NOT NULL,
                sender_self_id TEXT NOT NULL,
                sender_external_id TEXT NOT NULL,
                content TEXT NOT NULL,
                occurred_at INTEGER NOT NULL,
                message_type TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                UNIQUE(conversation_pk, message_key)
            );
            INSERT INTO raw_transcript_messages(
                conversation_pk,
                message_key,
                external_message_id,
                sender_canonical_id,
                sender_protocol,
                sender_self_id,
                sender_external_id,
                content,
                occurred_at,
                message_type,
                payload_hash,
                created_at
            )
            VALUES(
                1,
                'id:legacy',
                'legacy',
                NULL,
                'onebot',
                'bot',
                '42',
                'legacy normalized row',
                1700000000,
                'private',
                'hash',
                1700000000
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(MemoryMigrationRequiredError):
        JianerMemoryStore(database)


def test_legacy_migration_assigns_unknown_raw_preset_to_default(
    tmp_path: Path,
):
    legacy = tmp_path / "legacy.db"
    staged = tmp_path / "staged.db"
    _create_legacy_database(legacy)

    report = LegacyMemoryMigrator(tmp_path / "unused-live.db").stage(
        legacy, staged
    )
    assert report.verified
    conn = sqlite3.connect(staged)
    try:
        presets = {
            row[0]
            for row in conn.execute(
                """
                SELECT DISTINCT p.preset_key
                FROM sys_chat_message_index i
                JOIN sys_personas p
                  ON p.persona_id = i.active_persona_id
                """
            )
        }
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        conn.close()
    assert presets == {"default"}
    assert "raw_transcript_messages" not in tables


def test_migration_apply_and_rollback_restore_preexisting_target(
    tmp_path: Path,
):
    legacy = tmp_path / "legacy.db"
    source_copy = tmp_path / "legacy-copy.db"
    target = tmp_path / "target.db"
    backup = tmp_path / "target-before-migration.db"
    _create_legacy_database(legacy)
    target_store = JianerMemoryStore(target)
    _record(
        target_store,
        kind="group",
        conversation_id="seed",
        message_id="seed-message",
        user_id="999",
    )

    migrator = LegacyMemoryMigrator(target)
    migrator.copy_source(legacy, source_copy)
    report = migrator.apply(
        source_copy,
        backup_path=backup,
        migration_id="migration-for-rollback",
    )
    assert report.verified is True
    assert backup.is_file()
    assert target_store.count_transcripts() == 4

    assert migrator.rollback("migration-for-rollback") is True
    assert target_store.count_transcripts() == 1
    assert target_store.count_transcripts(conversation_id="seed") == 1
    assert target_store.count_transcripts(conversation_id="100") == 0
    conn = sqlite3.connect(target)
    try:
        assert conn.execute(
            """
            SELECT status
                FROM audit_legacy_migrations
            WHERE migration_id='migration-for-rollback'
            """
        ).fetchone()[0] == "rolled_back"
    finally:
        conn.close()


def test_memory_preference_uses_runtime_defaults(tmp_path: Path):
    store = JianerMemoryStore(
        tmp_path / "defaults.db",
        default_memory_enabled=False,
        default_memory_interval_seconds=900,
    )
    canonical = store.resolve_identity("onebot", "bot", "42")
    status = store.get_memory_status(
        canonical_user_id=canonical,
        preset="role-a",
    )

    assert status["enabled"] is False
    assert status["interval_seconds"] == 900
    assert status["next_retry_at"] == 0
    assert status["failure_count"] == 0


def test_failed_generation_has_durable_backoff_and_success_resets_it(
    tmp_path: Path,
):
    store = JianerMemoryStore(
        tmp_path / "backoff.db",
        default_memory_interval_seconds=60,
    )
    canonical = store.resolve_identity("onebot", "bot", "42")
    store.record_transcript(
        protocol="onebot",
        self_id="bot",
        conversation_kind="group",
        conversation_id="100",
        message_id="m1",
        sender_canonical_id=canonical,
        preset="role-a",
        content="需要稍后重试的事实",
    )
    batch = store.fetch_generation_batch(
        canonical_user_id=canonical,
        preset="role-a",
    )
    assert batch is not None

    first = store.defer_generation(
        batch.token,
        retry_base_seconds=60,
        retry_max_seconds=600,
    )
    assert first["accepted"] is True
    assert first["failure_count"] == 1
    assert store.list_due_memory_scopes(
        now=first["next_retry_at"] - 1
    ) == ()

    retry_batch = store.fetch_generation_batch(
        canonical_user_id=canonical,
        preset="role-a",
    )
    second = store.defer_generation(
        retry_batch.token,
        retry_base_seconds=60,
        retry_max_seconds=600,
    )
    assert second["failure_count"] == 2
    assert second["next_retry_at"] >= first["next_retry_at"] + 60

    success_batch = store.fetch_generation_batch(
        canonical_user_id=canonical,
        preset="role-a",
    )
    result = store.insert_generated_memories(
        success_batch.token,
        (),
        last_transcript_id=success_batch.last_transcript_id,
    )
    assert result.accepted is True
    status = store.get_memory_status(
        canonical_user_id=canonical,
        preset="role-a",
    )
    assert status["failure_count"] == 0
    assert status["next_retry_at"] == 0
    assert (
        store.fetch_generation_batch(
            canonical_user_id=canonical,
            preset="role-a",
        )
        is None
    )
