from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from plugins.JianerAI.memory import (
    GeneratedMemory,
    IdentityAuthorizationError,
    JianerMemoryStore,
    MemoryEvidence,
    authorize,
    merge_identity,
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


def test_group_retention_is_30_days_and_does_not_delete_private(
    tmp_path: Path,
):
    store = _store(tmp_path)
    now = 2_000_000_000
    old = now - (31 * 86400)
    fresh = now - (29 * 86400)
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

    assert store.purge_transcripts(now=now) == 1
    assert store.count_transcripts(conversation_kind="group") == 1
    assert store.count_transcripts(conversation_kind="private") == 1


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
            "SELECT COUNT(*) FROM identity_authorizations"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM identity_merge_ledger"
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
                FROM identity_authorizations
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
            FROM identity_merge_ledger
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


def test_existing_normalized_schema_backfills_transcript_preset(
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

    JianerMemoryStore(database)
    conn = sqlite3.connect(database)
    try:
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(raw_transcript_messages)"
            )
        }
        preset = conn.execute(
            "SELECT preset_key FROM raw_transcript_messages"
        ).fetchone()[0]
    finally:
        conn.close()
    assert "preset_key" in columns
    assert preset == "default"


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
                "SELECT DISTINCT preset_key FROM raw_transcript_messages"
            )
        }
    finally:
        conn.close()
    assert presets == {"default"}


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
            FROM migration_ledger
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
