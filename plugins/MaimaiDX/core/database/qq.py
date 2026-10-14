from pathlib import Path
from typing import Any

from sqlalchemy import Column, Enum, MetaData, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import Field, SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from ...core.clients.exceptions import UserNotBindError
from ...config import PROJECT_ROOT
from ...resources import data_dir, state_dir
from ...security import harden_private_path
from ..clients.lxns.models.oauth import OAuth2Token
from ..merge.models import ServiceName, Theme
from ..token_protection import protect_token, token_needs_migration, unprotect_token

db = state_dir / "user.db"
legacy_db = data_dir / "user.db"

metadata_user = MetaData()


class UserBase(SQLModel):
    __abstract__ = True
    metadata = metadata_user


class User(UserBase, table=True):
    ID: int = Field(default=None, primary_key=True, index=True, exclude=True)
    qqid: int = Field(unique=True)
    friend_code: int | None = Field(default=None)
    access_token: str | None = Field(default=None)
    refresh_token: str | None = Field(default=None)
    service: ServiceName = Field(
        default=ServiceName.DIVINGFISH, sa_column=Column(Enum(ServiceName))
    )
    theme: Theme = Field(default=Theme.PRISM_PLUS, sa_column=Column(Enum(Theme)))


engine = create_async_engine(f"sqlite+aiosqlite:///{str(db)}", echo=False)


def _latest_non_null(rows: list[dict[str, Any]], field: str) -> Any:
    return next((row[field] for row in rows if row[field] is not None), None)


def _merge_duplicate_users(connection: Connection) -> None:
    duplicate_qqids = connection.execute(
        text(
            'SELECT qqid FROM "user" '
            "GROUP BY qqid HAVING COUNT(*) > 1"
        )
    ).scalars().all()

    for qqid in duplicate_qqids:
        rows = [
            dict(row)
            for row in connection.execute(
                text(
                    'SELECT "ID", qqid, friend_code, access_token, '
                    'refresh_token, service, theme FROM "user" '
                    'WHERE qqid = :qqid ORDER BY "ID" DESC'
                ),
                {"qqid": qqid},
            ).mappings()
        ]
        if len(rows) < 2:
            continue

        canonical_id = rows[0]["ID"]
        complete_token = next(
            (
                row
                for row in rows
                if row["access_token"] is not None
                and row["refresh_token"] is not None
            ),
            None,
        )
        access_token = (
            complete_token["access_token"]
            if complete_token
            else _latest_non_null(rows, "access_token")
        )
        refresh_token = (
            complete_token["refresh_token"]
            if complete_token
            else _latest_non_null(rows, "refresh_token")
        )

        connection.execute(
            text(
                'UPDATE "user" SET friend_code = :friend_code, '
                "access_token = :access_token, refresh_token = :refresh_token, "
                "service = :service, theme = :theme WHERE \"ID\" = :canonical_id"
            ),
            {
                "canonical_id": canonical_id,
                "friend_code": _latest_non_null(rows, "friend_code"),
                "access_token": access_token,
                "refresh_token": refresh_token,
                "service": _latest_non_null(rows, "service"),
                "theme": _latest_non_null(rows, "theme"),
            },
        )
        connection.execute(
            text(
                'DELETE FROM "user" '
                'WHERE qqid = :qqid AND "ID" != :canonical_id'
            ),
            {"qqid": qqid, "canonical_id": canonical_id},
        )


def _has_unique_qqid_index(connection: Connection) -> bool:
    schema = inspect(connection)
    candidates = [
        *schema.get_unique_constraints("user"),
        *schema.get_indexes("user"),
    ]
    return any(
        candidate.get("unique", True)
        and candidate.get("column_names") == ["qqid"]
        for candidate in candidates
    )


def _migrate_user_schema(connection: Connection) -> None:
    _merge_duplicate_users(connection)
    if not _has_unique_qqid_index(connection):
        connection.execute(
            text('CREATE UNIQUE INDEX "ux_user_qqid" ON "user" (qqid)')
        )


def _migrate_legacy_database() -> None:
    legacy_sidecars = [
        (Path(str(legacy_db) + suffix), Path(str(db) + suffix))
        for suffix in ("-wal", "-shm", "-journal")
    ]
    has_legacy_sidecars = any(source.exists() for source, _ in legacy_sidecars)
    if not legacy_db.exists() and not has_legacy_sidecars:
        return
    if legacy_db.exists() and db.exists():
        raise RuntimeError("both legacy and private MaimaiDX user databases exist")
    if not legacy_db.exists() and not db.exists():
        raise RuntimeError("legacy MaimaiDX database sidecars have no main database")

    # Sidecars move first and the main database is the commit point. If any
    # sidecar move fails, the legacy main file remains in place and the next
    # startup can safely resume the remaining moves.
    for source, destination in legacy_sidecars:
        if source.exists():
            source.replace(destination)
    if legacy_db.exists():
        legacy_db.replace(db)


async def create_database():
    harden_private_path(PROJECT_ROOT / ".env", directory=False)
    state_dir.mkdir(parents=True, exist_ok=True)
    harden_private_path(state_dir, directory=True)
    if legacy_db.exists():
        harden_private_path(legacy_db, directory=False)
    for suffix in ("-wal", "-shm", "-journal"):
        legacy_sidecar = Path(str(legacy_db) + suffix)
        if legacy_sidecar.exists():
            harden_private_path(legacy_sidecar, directory=False)
    _migrate_legacy_database()
    async with engine.begin() as connect:
        await connect.run_sync(metadata_user.create_all)
        await connect.run_sync(_migrate_user_schema)
    harden_private_path(db, directory=False)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(db) + suffix)
        if sidecar.exists():
            harden_private_path(sidecar, directory=False)


async def _decrypt_and_migrate_user(session: AsyncSession, user: User) -> User:
    stored_access = user.access_token
    stored_refresh = user.refresh_token
    access = unprotect_token(stored_access)
    refresh = unprotect_token(stored_refresh)
    if token_needs_migration(stored_access) or token_needs_migration(stored_refresh):
        user.access_token = protect_token(access)
        user.refresh_token = protect_token(refresh)
        await session.commit()
        await session.refresh(user)
    user.access_token = access
    user.refresh_token = refresh
    return user


async def get_user(qqid: int) -> User:
    async with AsyncSession(engine) as session:
        statement = select(User).where(User.qqid == qqid)
        result = await session.exec(statement)
        user = result.first()
        if user is None:
            raise UserNotBindError
        return await _decrypt_and_migrate_user(session, user)


async def update_user(
    qqid: int,
    *,
    friend_code: int | None = None,
    service: ServiceName | None = None,
    token: OAuth2Token | None = None,
    theme: Theme | None = None,
) -> User:
    update_data = {
        "friend_code": friend_code,
        "service": service,
        "access_token": protect_token(token.access_token) if token else None,
        "refresh_token": protect_token(token.refresh_token) if token else None,
        "theme": theme,
    }
    update_data = {k: v for k, v in update_data.items() if v is not None}

    async with AsyncSession(engine) as session:
        statement = select(User).where(User.qqid == qqid)
        result = await session.exec(statement)
        if user := result.first():
            user.sqlmodel_update(update_data)
            inserted = False
        else:
            user = User(qqid=qqid, **update_data)
            session.add(user)
            inserted = True
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            if not inserted:
                raise
            result = await session.exec(statement)
            user = result.first()
            if user is None:
                raise
            user.sqlmodel_update(update_data)
            await session.commit()
        await session.refresh(user)
        return await _decrypt_and_migrate_user(session, user)


async def delete_user(qqid: int) -> bool:
    async with AsyncSession(engine) as session:
        statement = select(User).where(User.qqid == qqid)
        result = await session.exec(statement)
        if user := result.first():
            await session.delete(user)
            await session.commit()
            return True
        return False
