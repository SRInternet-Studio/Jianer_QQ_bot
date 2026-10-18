import os
from dataclasses import replace
from io import BytesIO
from pathlib import Path

from PIL import Image

from plugins.JianerMate.config import MemeOptions
from plugins.JianerMate.memes import MemeLibrary
from plugins.JianerMate.store import MateStore


def _png(color, size=(120, 120)):
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _library(tmp_path, **overrides):
    store = MateStore(tmp_path / "mate.db")
    options = replace(MemeOptions(), **overrides)
    library = MemeLibrary(store, options, directory=tmp_path / "memes")
    return store, library


def test_conversation_and_message_lifecycle(tmp_path):
    store = MateStore(tmp_path / "mate.db")
    conversation = store.ensure_conversation(
        "milky", "10001", "group", "222", now=1000
    )
    assert conversation.enabled is False
    store.update_conversation(
        conversation.ref, enabled=True, talk_value=0.7, now=1001
    )
    updated = store.get_conversation("milky", "10001", "group", "222")
    assert updated.enabled is True
    assert updated.talk_value == 0.7
    assert store.list_conversations(enabled_only=True) == (updated,)

    store.append_message(
        conversation.ref,
        direction="incoming",
        content="你好",
        platform_message_id="m1",
        sender_id="5",
        now=1002,
    )
    store.append_message(
        conversation.ref,
        direction="outgoing",
        content="在的",
        platform_message_id="m2",
        now=1003,
    )
    recent = store.recent_messages(conversation.ref, limit=10)
    assert [record.content for record in recent] == ["你好", "在的"]
    assert store.last_outgoing_at(conversation.ref) == 1003

    for index in range(10):
        store.append_message(
            conversation.ref,
            direction="incoming",
            content=f"消息{index}",
            now=2000 + index,
        )
    removed = store.prune_messages(
        conversation.ref, keep=3, retention_days=7, now=3000
    )
    assert removed > 0
    assert len(store.recent_messages(conversation.ref, limit=50)) == 3
    store.close()


def test_meme_ingest_deduplicate_and_limits(tmp_path):
    store, library = _library(tmp_path, max_entries=2)
    first = library.ingest(_png("red"), source_group="1", source_user="9")
    assert first.status == "added"
    duplicate = library.ingest(_png("red"), source_group="1")
    assert duplicate.status == "duplicate"
    second = library.ingest(_png("blue"))
    assert second.status == "added"
    third = library.ingest(_png("green"))
    assert third.status == "added"
    assert store.count_memes() == 2
    remaining = {record.id for record in store.list_memes(limit=10)}
    assert third.meme_id in remaining
    assert first.meme_id not in remaining

    tiny = library.ingest(_png("black", size=(4, 4)))
    assert tiny.status == "rejected"
    store.close()


def test_meme_size_cap_rejects_oversized(tmp_path):
    store, library = _library(tmp_path, max_size_mb=0.0001)
    result = library.ingest(_png("white", size=(256, 256)))
    assert result.status == "rejected"
    assert "过大" in result.reason
    store.close()


def test_meme_candidates_skip_recent_and_file_uri_guard(tmp_path):
    store, library = _library(tmp_path, reuse_cooldown_seconds=3600)
    first = library.ingest(_png("red"), now=100)
    second = library.ingest(_png("blue"), now=101)
    assert first.meme_id and second.meme_id
    store.mark_meme_used(first.meme_id, now=1000)

    candidates = library.candidates(now=1000)
    assert [candidate.id for candidate in candidates] == [second.meme_id]

    uri = library.file_uri(second.meme_id)
    assert uri is not None and uri.startswith("file://")

    outside = tmp_path / "outside.png"
    outside.write_bytes(_png("purple"))
    store.add_meme(
        sha256="outside",
        path=str(outside),
        mime="image/png",
        width=10,
        height=10,
        size=outside.stat().st_size,
        now=200,
    )
    outside_record = store.get_meme_by_sha("outside")
    assert outside_record is not None
    assert library.file_uri(outside_record.id) is None

    store.update_meme_tags(second.meme_id, "猫,无语")
    assert store.get_meme(second.meme_id).tags == "猫,无语"
    assert library.delete(second.meme_id) is True
    assert store.get_meme(second.meme_id) is None
    store.close()


def test_decisions_are_pruned_per_conversation(tmp_path):
    store = MateStore(tmp_path / "mate.db")
    conversation = store.ensure_conversation(
        "milky", "10001", "group", "222", now=1
    )
    for index in range(210):
        store.add_decision(
            conversation.ref,
            gate_passed=True,
            gate_reason=f"reason-{index}",
            now=1000 + index,
        )
    assert store.count_decisions(conversation.ref) == 200
    newest = store.recent_decisions(conversation.ref, limit=1)
    assert newest[0].gate_reason == "reason-209"
    store.close()


def test_orphan_meme_files_are_cleaned(tmp_path):
    store, library = _library(tmp_path)
    ingested = library.ingest(_png("red"))
    assert ingested.status == "added"
    keep_path = Path(store.get_meme(ingested.meme_id).path)
    assert keep_path.is_file()

    stale = library.directory / ("a" * 32 + ".png")
    stale.write_bytes(_png("blue"))
    old = 1_000_000
    os.utime(stale, (old, old))

    fresh = library.directory / ("b" * 32 + ".png")
    fresh.write_bytes(_png("green"))

    unrelated = library.directory / "notes.txt"
    unrelated.write_text("keep me", encoding="utf-8")
    os.utime(unrelated, (old, old))

    removed = library.cleanup_orphans(now=2_000_000)
    assert removed == 1
    assert keep_path.is_file()
    assert not stale.exists()
    assert fresh.is_file()
    assert unrelated.is_file()
    store.close()
