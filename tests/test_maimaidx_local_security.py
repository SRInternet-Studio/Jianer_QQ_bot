from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_divingfish_token_is_never_attached_to_third_party_proxy(monkeypatch):
    from plugins.MaimaiDX.core.clients.divingfish import client as module

    monkeypatch.setattr(
        module,
        "dfconfig",
        SimpleNamespace(divingfish_token="secret-value"),
    )
    module.DivingFishAPI.base_url = module.DivingFishAPI.proxy_url + "/maimaidxprober"
    try:
        api = module.DivingFishAPI(qqid=1)
        assert api.base_url.startswith(module.DivingFishAPI.proxy_url)
        assert "developer-token" not in api.headers
        assert module.DivingFishAPI.set_proxy() is False
        assert module.DivingFishAPI.base_url == module.DivingFishAPI.origin_url
    finally:
        module.DivingFishAPI.reset_origin()


def test_divingfish_token_is_attached_only_to_official_origin(monkeypatch):
    from plugins.MaimaiDX.core.clients.divingfish import client as module

    monkeypatch.setattr(
        module,
        "dfconfig",
        SimpleNamespace(divingfish_token="secret-value"),
    )
    module.DivingFishAPI.reset_origin()
    api = module.DivingFishAPI(qqid=1)
    assert api.headers == {"developer-token": "secret-value"}


def test_token_protection_round_trip_and_no_plaintext():
    from plugins.MaimaiDX.core.token_protection import protect_token, unprotect_token

    raw = "oauth-token-that-must-not-be-stored"
    protected = protect_token(raw)
    assert protected != raw
    assert raw not in protected
    assert unprotect_token(protected) == raw


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
def test_windows_acl_hardening_removes_preexisting_explicit_everyone_ace(tmp_path):
    from plugins.MaimaiDX.security import harden_private_path

    secret = tmp_path / "secret.env"
    secret.write_text("credential=value", encoding="utf-8")
    grant = subprocess.run(
        ["icacls.exe", str(secret), "/grant", "*S-1-1-0:(R)"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert grant.returncode == 0

    harden_private_path(secret, directory=False)

    script = (
        "& { param($Target) "
        "(Get-Acl -LiteralPath $Target).Access | ForEach-Object { "
        "$_.IdentityReference.Translate("
        "[System.Security.Principal.SecurityIdentifier]).Value } }"
    )
    inspected = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script, str(secret)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    sids = {line.strip() for line in inspected.stdout.splitlines() if line.strip()}
    assert "S-1-1-0" not in sids
    assert "S-1-5-18" in sids
    assert "S-1-5-32-544" in sids


def test_user_database_persists_protected_tokens(monkeypatch, tmp_path):
    from plugins.MaimaiDX.core.clients.lxns.models.oauth import OAuth2Token
    from plugins.MaimaiDX.core.database import qq as database

    test_db = tmp_path / "private" / "user.db"
    test_db.parent.mkdir(parents=True)
    test_engine = database.create_async_engine(
        f"sqlite+aiosqlite:///{test_db}", echo=False
    )
    monkeypatch.setattr(database, "engine", test_engine)
    monkeypatch.setattr(database, "db", test_db)
    monkeypatch.setattr(database, "legacy_db", tmp_path / "legacy-user.db")
    monkeypatch.setattr(database, "state_dir", test_db.parent)
    monkeypatch.setattr(database, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(database, "harden_private_path", lambda *args, **kwargs: None)

    async def exercise():
        await database.create_database()
        token = OAuth2Token(
            access_token="access-plaintext",
            refresh_token="refresh-plaintext",
            token_type="Bearer",
            expires_in=3600,
            scope="read_player",
        )
        returned = await database.update_user(123, token=token)
        assert returned.access_token == token.access_token
        assert returned.refresh_token == token.refresh_token

        with sqlite3.connect(test_db) as connection:
            stored = connection.execute(
                'SELECT access_token, refresh_token FROM "user" WHERE qqid = 123'
            ).fetchone()
        assert stored is not None
        assert token.access_token not in stored[0]
        assert token.refresh_token not in stored[1]

        loaded = await database.get_user(123)
        assert loaded.access_token == token.access_token
        assert loaded.refresh_token == token.refresh_token
        await test_engine.dispose()

    asyncio.run(exercise())


def test_plaintext_token_migration_returns_a_fully_loaded_user(monkeypatch, tmp_path):
    from plugins.MaimaiDX.core.database import qq as database

    test_db = tmp_path / "private" / "user.db"
    test_db.parent.mkdir(parents=True)
    test_engine = database.create_async_engine(
        f"sqlite+aiosqlite:///{test_db}", echo=False
    )
    monkeypatch.setattr(database, "engine", test_engine)
    monkeypatch.setattr(database, "db", test_db)
    monkeypatch.setattr(database, "legacy_db", tmp_path / "legacy-user.db")
    monkeypatch.setattr(database, "state_dir", test_db.parent)
    monkeypatch.setattr(database, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(database, "harden_private_path", lambda *args, **kwargs: None)

    async def exercise():
        await database.create_database()
        with sqlite3.connect(test_db) as connection:
            connection.execute(
                'INSERT INTO "user" '
                '(qqid, friend_code, access_token, refresh_token, service, theme) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (
                    456,
                    123456789012345,
                    "legacy-access-plaintext",
                    "legacy-refresh-plaintext",
                    "DIVINGFISH",
                    "PRISM_PLUS",
                ),
            )
            connection.commit()

        loaded = await database.get_user(456)
        assert loaded.qqid == 456
        assert loaded.friend_code == 123456789012345
        assert loaded.service is database.ServiceName.DIVINGFISH
        assert loaded.theme is database.Theme.PRISM_PLUS
        assert loaded.access_token == "legacy-access-plaintext"
        assert loaded.refresh_token == "legacy-refresh-plaintext"

        with sqlite3.connect(test_db) as connection:
            stored = connection.execute(
                'SELECT access_token, refresh_token FROM "user" WHERE qqid = 456'
            ).fetchone()
        assert stored is not None
        assert "legacy-access-plaintext" not in stored[0]
        assert "legacy-refresh-plaintext" not in stored[1]
        await test_engine.dispose()

    asyncio.run(exercise())


def test_concurrent_first_user_creation_keeps_one_consistent_row(
    monkeypatch, tmp_path
):
    from plugins.MaimaiDX.core.clients.lxns.models.oauth import OAuth2Token
    from plugins.MaimaiDX.core.database import qq as database

    test_db = tmp_path / "private" / "user.db"
    test_db.parent.mkdir(parents=True)
    test_engine = database.create_async_engine(
        f"sqlite+aiosqlite:///{test_db}", echo=False
    )
    monkeypatch.setattr(database, "engine", test_engine)
    monkeypatch.setattr(database, "db", test_db)
    monkeypatch.setattr(database, "legacy_db", tmp_path / "legacy-user.db")
    monkeypatch.setattr(database, "state_dir", test_db.parent)
    monkeypatch.setattr(database, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(database, "harden_private_path", lambda *args, **kwargs: None)

    async def exercise():
        await database.create_database()
        token = OAuth2Token(
            access_token="concurrent-access",
            refresh_token="concurrent-refresh",
            token_type="Bearer",
            expires_in=3600,
            scope="read_player",
        )
        start = asyncio.Event()

        async def set_credentials():
            await start.wait()
            return await database.update_user(
                789,
                service=database.ServiceName.LXNS,
                token=token,
            )

        async def set_preferences():
            await start.wait()
            return await database.update_user(
                789,
                friend_code=123456789012345,
                theme=database.Theme.CIRCLE,
            )

        tasks = [
            asyncio.create_task(set_credentials()),
            asyncio.create_task(set_preferences()),
        ]
        await asyncio.sleep(0)
        start.set()
        results = await asyncio.gather(*tasks)
        loaded = await database.get_user(789)

        assert all(user.qqid == 789 for user in results)
        assert loaded.friend_code == 123456789012345
        assert loaded.service is database.ServiceName.LXNS
        assert loaded.theme is database.Theme.CIRCLE
        assert loaded.access_token == token.access_token
        assert loaded.refresh_token == token.refresh_token

        with sqlite3.connect(test_db) as connection:
            rows = connection.execute(
                'SELECT qqid, friend_code, service, theme FROM "user" '
                "WHERE qqid = 789"
            ).fetchall()
        assert rows == [(789, 123456789012345, "LXNS", "CIRCLE")]
        await test_engine.dispose()

    asyncio.run(exercise())


def test_existing_duplicate_users_are_merged_before_unique_index(
    monkeypatch, tmp_path
):
    from plugins.MaimaiDX.core.database import qq as database

    test_db = tmp_path / "private" / "user.db"
    test_db.parent.mkdir(parents=True)
    with sqlite3.connect(test_db) as connection:
        connection.execute(
            'CREATE TABLE "user" ('
            '"ID" INTEGER PRIMARY KEY AUTOINCREMENT, '
            "qqid INTEGER NOT NULL, friend_code INTEGER, "
            "access_token VARCHAR, refresh_token VARCHAR, "
            "service VARCHAR NOT NULL, theme VARCHAR NOT NULL)"
        )
        connection.executemany(
            'INSERT INTO "user" '
            '(qqid, friend_code, access_token, refresh_token, service, theme) '
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    987,
                    999999999999999,
                    "preserved-access",
                    "preserved-refresh",
                    "DIVINGFISH",
                    "PRISM_PLUS",
                ),
                (987, None, None, None, "LXNS", "CIRCLE"),
            ],
        )

    test_engine = database.create_async_engine(
        f"sqlite+aiosqlite:///{test_db}", echo=False
    )
    monkeypatch.setattr(database, "engine", test_engine)
    monkeypatch.setattr(database, "db", test_db)
    monkeypatch.setattr(database, "legacy_db", tmp_path / "legacy-user.db")
    monkeypatch.setattr(database, "state_dir", test_db.parent)
    monkeypatch.setattr(database, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(database, "harden_private_path", lambda *args, **kwargs: None)

    async def exercise():
        await database.create_database()
        await database.create_database()

        with sqlite3.connect(test_db) as connection:
            rows = connection.execute(
                'SELECT "ID", qqid, friend_code, access_token, refresh_token, '
                'service, theme FROM "user" WHERE qqid = 987'
            ).fetchall()
        assert rows == [
            (
                2,
                987,
                999999999999999,
                "preserved-access",
                "preserved-refresh",
                "LXNS",
                "CIRCLE",
            )
        ]

        loaded = await database.get_user(987)
        assert loaded.friend_code == 999999999999999
        assert loaded.access_token == "preserved-access"
        assert loaded.refresh_token == "preserved-refresh"
        assert loaded.service is database.ServiceName.LXNS
        assert loaded.theme is database.Theme.CIRCLE
        await test_engine.dispose()

    asyncio.run(exercise())

    with sqlite3.connect(test_db) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                'INSERT INTO "user" '
                "(qqid, service, theme) VALUES (?, ?, ?)",
                (987, "DIVINGFISH", "PRISM_PLUS"),
            )


def test_private_state_is_outside_static_resource_tree():
    from plugins.MaimaiDX.resources import state_dir, static

    assert state_dir != static
    assert static not in state_dir.parents


def test_create_database_atomically_migrates_legacy_state(monkeypatch, tmp_path):
    from plugins.MaimaiDX.core.database import qq as database

    legacy_db = tmp_path / "static" / "data" / "user.db"
    private_db = tmp_path / "private" / "user.db"
    legacy_db.parent.mkdir(parents=True)
    connection = sqlite3.connect(legacy_db)
    try:
        connection.execute("CREATE TABLE migration_probe (value INTEGER)")
        connection.execute("INSERT INTO migration_probe VALUES (7)")
        connection.commit()
    finally:
        connection.close()

    test_engine = database.create_async_engine(
        f"sqlite+aiosqlite:///{private_db}", echo=False
    )
    monkeypatch.setattr(database, "engine", test_engine)
    monkeypatch.setattr(database, "db", private_db)
    monkeypatch.setattr(database, "legacy_db", legacy_db)
    monkeypatch.setattr(database, "state_dir", private_db.parent)
    monkeypatch.setattr(database, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(database, "harden_private_path", lambda *args, **kwargs: None)

    async def exercise():
        await database.create_database()
        await test_engine.dispose()

    asyncio.run(exercise())
    assert private_db.exists()
    assert not legacy_db.exists()
    with sqlite3.connect(private_db) as connection:
        assert connection.execute("SELECT value FROM migration_probe").fetchone() == (7,)


def test_legacy_sidecar_migration_is_resumable_before_main_switch(
    monkeypatch, tmp_path
):
    from plugins.MaimaiDX.core.database import qq as database

    legacy_db = tmp_path / "static" / "data" / "user.db"
    private_db = tmp_path / "private" / "user.db"
    legacy_db.parent.mkdir(parents=True)
    private_db.parent.mkdir(parents=True)
    legacy_db.write_bytes(b"legacy-main")
    Path(str(legacy_db) + "-wal").write_bytes(b"legacy-wal")
    Path(str(legacy_db) + "-shm").write_bytes(b"legacy-shm")
    monkeypatch.setattr(database, "legacy_db", legacy_db)
    monkeypatch.setattr(database, "db", private_db)

    original_replace = Path.replace
    failed = False

    def fail_once_on_shm(self, target):
        nonlocal failed
        if str(self).endswith("-shm") and not failed:
            failed = True
            raise OSError("simulated sidecar move failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_once_on_shm)
    with pytest.raises(OSError, match="simulated sidecar"):
        database._migrate_legacy_database()

    assert legacy_db.exists()
    assert not private_db.exists()
    assert Path(str(private_db) + "-wal").read_bytes() == b"legacy-wal"
    assert Path(str(legacy_db) + "-shm").exists()

    monkeypatch.setattr(Path, "replace", original_replace)
    database._migrate_legacy_database()

    assert private_db.read_bytes() == b"legacy-main"
    assert Path(str(private_db) + "-wal").read_bytes() == b"legacy-wal"
    assert Path(str(private_db) + "-shm").read_bytes() == b"legacy-shm"
    assert not legacy_db.exists()
