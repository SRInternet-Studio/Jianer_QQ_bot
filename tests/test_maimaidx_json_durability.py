import asyncio
import json
from types import SimpleNamespace

import pytest


def test_atomic_write_failure_preserves_old_file_and_cleans_temp(
    monkeypatch, tmp_path
):
    from plugins.MaimaiDX.core import tool

    target = tmp_path / "state.json"
    target.write_text('{"version": "old"}', encoding="utf-8")

    def fail_replace(source, destination):
        assert destination == target
        raise OSError("simulated replace failure")

    monkeypatch.setattr(tool.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        asyncio.run(tool.writefile(target, {"version": "new"}))

    assert target.read_text(encoding="utf-8") == '{"version": "old"}'
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []


def test_switch_updates_are_serialized_without_lost_entries(monkeypatch, tmp_path):
    from plugins.MaimaiDX.core import tool
    from plugins.MaimaiDX.core import service

    alias_path = tmp_path / "group_alias.json"
    guess_path = tmp_path / "guess.json"
    monkeypatch.setattr(service, "group_alias_file", alias_path)
    monkeypatch.setattr(service, "guess_file", guess_path)
    group_alias = service.GroupAlias()
    guess = service.Guess()
    real_writefile = tool.writefile

    async def force_stale_first_write_to_finish_last(path, data):
        if data.get("enable") in ([101], [201]):
            await asyncio.sleep(0.05)
        return await real_writefile(path, data)

    monkeypatch.setattr(
        service,
        "writefile",
        force_stale_first_write_to_finish_last,
    )

    async def exercise():
        await asyncio.gather(group_alias.on(101), group_alias.on(102))
        await asyncio.gather(guess.on(201), guess.on(202))

    asyncio.run(exercise())

    alias_data = json.loads(alias_path.read_text(encoding="utf-8"))
    guess_data = json.loads(guess_path.read_text(encoding="utf-8"))
    assert alias_data["enable"] == [101, 102]
    assert guess_data["enable"] == [201, 202]


def test_concurrent_local_alias_updates_do_not_lose_entries(monkeypatch, tmp_path):
    from plugins.MaimaiDX.core import service
    from plugins.MaimaiDX.core.merge.alias_list import AliasList

    local_alias_path = tmp_path / "local_alias.json"
    monkeypatch.setattr(service, "local_alias_file", local_alias_path)
    monkeypatch.setattr(service, "_local_alias_lock", asyncio.Lock())
    monkeypatch.setattr(
        service.mai,
        "total_alias_list",
        AliasList(root=[]),
        raising=False,
    )
    monkeypatch.setattr(
        service.mai,
        "total_list",
        SimpleNamespace(by_id=lambda song_id: None),
        raising=False,
    )

    async def exercise():
        return await asyncio.gather(
            service.update_local_alias(42, "Alpha"),
            service.update_local_alias(42, "Beta"),
        )

    assert asyncio.run(exercise()) == [True, True]
    saved = json.loads(local_alias_path.read_text(encoding="utf-8"))
    assert saved == {"42": ["alpha", "beta"]}


def test_corrupt_switch_files_are_backed_up_without_overwrite(
    monkeypatch, tmp_path
):
    from plugins.MaimaiDX.core import service

    alias_path = tmp_path / "group_alias.json"
    guess_path = tmp_path / "guess.json"
    alias_path.write_text("{broken-alias", encoding="utf-8")
    guess_path.write_text("{broken-guess", encoding="utf-8")
    previous_backup = tmp_path / "group_alias.json.corrupt"
    previous_backup.write_text("previous backup", encoding="utf-8")
    warnings = []

    monkeypatch.setattr(service, "group_alias_file", alias_path)
    monkeypatch.setattr(service, "guess_file", guess_path)
    monkeypatch.setattr(
        service,
        "log",
        SimpleNamespace(warning=lambda message: warnings.append(str(message))),
    )

    group_alias = service.GroupAlias()
    guess = service.Guess()

    assert group_alias.push.enable == []
    assert group_alias.push.disable == []
    assert guess.switch.enable == []
    assert guess.switch.disable == []
    assert not alias_path.exists()
    assert not guess_path.exists()
    assert previous_backup.read_text(encoding="utf-8") == "previous backup"
    assert (tmp_path / "group_alias.json.corrupt.1").read_text(
        encoding="utf-8"
    ) == "{broken-alias"
    assert (tmp_path / "guess.json.corrupt").read_text(
        encoding="utf-8"
    ) == "{broken-guess"
    assert len(warnings) == 2
    assert all("状态文件损坏" in warning for warning in warnings)
