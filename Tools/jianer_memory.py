import asyncio
import json
import logging
import math
import queue
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


logger = logging.getLogger("jianer_memory")


def _now_ts() -> int:
    return int(time.time())


def _digits_only(value: Any) -> str:
    s = str(value)
    m = re.findall(r"\d+", s)
    return "".join(m) if m else "0"


def _scope_ids_from_event(group_id: Any, user_id: Any) -> Tuple[str, str, bool]:
    uid = _digits_only(user_id)
    if group_id is None:
        return "p", uid, True
    gid = _digits_only(group_id)
    return gid, uid, False


def raw_table_name(group_id: str, user_id: str, is_private: bool) -> str:
    if is_private:
        return f"raw_p_u{user_id}"
    return f"raw_g{group_id}_u{user_id}"


def mem_table_name(group_id: str, user_id: str, is_private: bool) -> str:
    if is_private:
        return f"mem_p_u{user_id}"
    return f"mem_g{group_id}_u{user_id}"


@dataclass(frozen=True)
class RawMessageRow:
    table: str
    group_id: str
    user_id: str
    is_private: int
    message_id: str
    sender: str
    content: str
    timestamp: int
    message_type: str


class MemorySQLiteStore:
    def __init__(self, db_path: str, default_enabled: int = 1, default_interval_seconds: int = 6 * 3600):
        self.db_path = db_path
        self.default_enabled = int(default_enabled)
        self.default_interval_seconds = int(default_interval_seconds)
        self._q: "queue.Queue[RawMessageRow]" = queue.Queue(maxsize=20000)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._writer_loop, name="jianer_memory_writer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        if not self._started:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def enqueue_raw_message(
        self,
        group_id: Any,
        user_id: Any,
        message_id: Any,
        sender: Any,
        content: str,
        timestamp: Optional[int],
        message_type: str,
    ) -> bool:
        gid, uid, is_private = _scope_ids_from_event(group_id, user_id)
        table = raw_table_name(gid, uid, is_private)
        row = RawMessageRow(
            table=table,
            group_id=gid,
            user_id=uid,
            is_private=1 if is_private else 0,
            message_id=str(message_id) if message_id is not None else "",
            sender=_digits_only(sender),
            content=content,
            timestamp=int(timestamp) if timestamp is not None else _now_ts(),
            message_type=str(message_type or ""),
        )
        try:
            self._q.put_nowait(row)
            return True
        except queue.Full:
            logger.warning("memory raw queue full, drop message")
            return False

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _ensure_meta_tables(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_settings (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                is_private INTEGER NOT NULL,
                enabled INTEGER NOT NULL,
                interval_seconds INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (group_id, user_id, is_private)
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_state (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                is_private INTEGER NOT NULL,
                raw_table TEXT NOT NULL,
                last_seq INTEGER NOT NULL,
                last_generated_at INTEGER NOT NULL,
                PRIMARY KEY (group_id, user_id, is_private)
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mem_global (
                memory_id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_content TEXT NOT NULL,
                generated_at INTEGER NOT NULL,
                weight REAL NOT NULL
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_global_at ON mem_global(generated_at);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_global_weight ON mem_global(weight);")

    def _ensure_raw_table(self, conn: sqlite3.Connection, table: str) -> None:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT,
                sender TEXT,
                content TEXT,
                timestamp INTEGER,
                message_type TEXT
            );
            """
        )
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_ts ON {table}(timestamp);")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_msgid ON {table}(message_id);")

    def _writer_loop(self) -> None:
        try:
            conn = self._connect()
            self._ensure_meta_tables(conn)
            created_tables: set[str] = set()
            pending: List[RawMessageRow] = []
            last_commit = time.time()
            while not self._stop.is_set():
                try:
                    item = self._q.get(timeout=0.5)
                    pending.append(item)
                    while len(pending) < 200:
                        try:
                            pending.append(self._q.get_nowait())
                        except queue.Empty:
                            break
                except queue.Empty:
                    pass

                if not pending:
                    continue

                try:
                    with conn:
                        for row in pending:
                            if row.table not in created_tables:
                                self._ensure_raw_table(conn, row.table)
                                created_tables.add(row.table)
                            conn.execute(
                                f"INSERT INTO {row.table}(message_id, sender, content, timestamp, message_type) VALUES(?,?,?,?,?);",
                                (row.message_id, row.sender, row.content, row.timestamp, row.message_type),
                            )
                            conn.execute(
                                """
                                INSERT INTO memory_settings(group_id, user_id, is_private, enabled, interval_seconds, updated_at)
                                VALUES(?,?,?,?,?,?)
                                ON CONFLICT(group_id, user_id, is_private) DO NOTHING;
                                """,
                                (row.group_id, row.user_id, row.is_private, self.default_enabled, self.default_interval_seconds, _now_ts()),
                            )
                            conn.execute(
                                """
                                INSERT INTO memory_state(group_id, user_id, is_private, raw_table, last_seq, last_generated_at)
                                VALUES(?,?,?,?,?,?)
                                ON CONFLICT(group_id, user_id, is_private) DO NOTHING;
                                """,
                                (row.group_id, row.user_id, row.is_private, row.table, 0, 0),
                            )
                    pending.clear()
                    last_commit = time.time()
                except Exception as e:
                    logger.exception("memory writer error: %s", e)
                    pending.clear()
                    if time.time() - last_commit > 1.0:
                        try:
                            conn.close()
                        except Exception:
                            pass
                        conn = self._connect()
                        self._ensure_meta_tables(conn)
                        created_tables.clear()
            try:
                while True:
                    try:
                        pending.append(self._q.get_nowait())
                    except queue.Empty:
                        break
                if pending:
                    with conn:
                        for row in pending:
                            if row.table not in created_tables:
                                self._ensure_raw_table(conn, row.table)
                                created_tables.add(row.table)
                            conn.execute(
                                f"INSERT INTO {row.table}(message_id, sender, content, timestamp, message_type) VALUES(?,?,?,?,?);",
                                (row.message_id, row.sender, row.content, row.timestamp, row.message_type),
                            )
                            conn.execute(
                                """
                                INSERT INTO memory_settings(group_id, user_id, is_private, enabled, interval_seconds, updated_at)
                                VALUES(?,?,?,?,?,?)
                                ON CONFLICT(group_id, user_id, is_private) DO NOTHING;
                                """,
                                (row.group_id, row.user_id, row.is_private, self.default_enabled, self.default_interval_seconds, _now_ts()),
                            )
                            conn.execute(
                                """
                                INSERT INTO memory_state(group_id, user_id, is_private, raw_table, last_seq, last_generated_at)
                                VALUES(?,?,?,?,?,?)
                                ON CONFLICT(group_id, user_id, is_private) DO NOTHING;
                                """,
                                (row.group_id, row.user_id, row.is_private, row.table, 0, 0),
                            )
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception as e:
            logger.exception("memory writer fatal: %s", e)


@dataclass(frozen=True)
class MemoryItem:
    content: str
    weight: float


@dataclass(frozen=True)
class DueScope:
    group_id: str
    user_id: str
    is_private: int
    interval_seconds: int
    last_seq: int
    last_generated_at: int
    raw_table: str


class JianerMemoryService:
    def __init__(
        self,
        db_path: str,
        memory_mode: str,
        default_enabled: bool = True,
        default_interval_seconds: int = 6 * 3600,
        scheduler_tick_seconds: int = 30,
        max_raw_rows_per_generation: int = 200,
        min_new_rows_to_generate: int = 12,
        max_chars_per_generation: int = 12000,
        cleanup_interval_seconds: int = 6 * 3600,
        cleanup_keep_days: int = 30,
        cleanup_min_weight: float = 0.25,
        cleanup_keep_max_rows: int = 3000,
        global_optimize_interval_seconds: int = 24 * 3600,
        global_candidate_limit: int = 200,
    ):
        self.db_path = db_path
        self.memory_mode = str(memory_mode or "").strip()
        self.default_enabled = 1 if default_enabled else 0
        self.default_interval_seconds = int(default_interval_seconds)
        self.scheduler_tick_seconds = int(scheduler_tick_seconds)
        self.max_raw_rows_per_generation = int(max_raw_rows_per_generation)
        self.min_new_rows_to_generate = int(min_new_rows_to_generate)
        self.max_chars_per_generation = int(max_chars_per_generation)
        self.cleanup_interval_seconds = int(cleanup_interval_seconds)
        self.cleanup_keep_days = int(cleanup_keep_days)
        self.cleanup_min_weight = float(cleanup_min_weight)
        self.cleanup_keep_max_rows = int(cleanup_keep_max_rows)
        self.global_optimize_interval_seconds = int(global_optimize_interval_seconds)
        self.global_candidate_limit = int(global_candidate_limit)

        self.store = MemorySQLiteStore(db_path, default_enabled=self.default_enabled, default_interval_seconds=self.default_interval_seconds)
        self._scheduler_task: Optional[asyncio.Task] = None
        self._stop = False

        self._persona_cache: Dict[str, str] = {}
        self._memory_user_lists: Dict[str, List[Dict[str, str]]] = {}

        self._gen_sem = asyncio.Semaphore(1)
        self._last_cleanup_at = 0
        self._last_global_optimize_at = 0

    def start(self) -> None:
        self.store.start()
        if self._scheduler_task is None:
            try:
                loop = asyncio.get_running_loop()
                self._scheduler_task = loop.create_task(self._scheduler_loop())
            except RuntimeError:
                self._scheduler_task = None

    def stop(self) -> None:
        self._stop = True
        self.store.stop()

    def update_persona(self, user_id: Any, sys_prompt: str) -> None:
        uid = _digits_only(user_id)
        if not uid:
            return
        self._persona_cache[uid] = (sys_prompt or "").strip()

    def capture_message_event(self, event: Any, Segments: Any) -> None:
        try:
            user_id = getattr(event, "user_id", None)
            self_id = getattr(event, "self_id", None)
            if user_id is None or self_id is None:
                return
            if str(user_id) == str(self_id):
                return

            message = getattr(event, "message", None)
            if not message:
                return

            group_id = getattr(event, "group_id", None)
            message_id = getattr(event, "message_id", None)
            ts = getattr(event, "time", None)
            if ts is None:
                ts = getattr(event, "time_str", None)

            content = self._message_to_content(message, Segments)
            if not content:
                return

            msg_type = "private" if group_id is None else "group"
            self.store.enqueue_raw_message(
                group_id=group_id,
                user_id=user_id,
                message_id=message_id,
                sender=user_id,
                content=content,
                timestamp=int(ts) if str(ts).isdigit() else _now_ts(),
                message_type=msg_type,
            )
        except Exception as e:
            logger.exception("capture_message_event error: %s", e)

    def _message_to_content(self, message: Any, Segments: Any) -> str:
        parts: List[str] = []
        for seg in list(message):
            try:
                if isinstance(seg, Segments.Text):
                    t = (seg.text or "").strip()
                    if t:
                        parts.append(t)
                elif isinstance(seg, Segments.Image):
                    url = seg.file if getattr(seg, "file", "").startswith("http") else getattr(seg, "url", "")
                    url = (url or "").strip()
                    parts.append(f"[图片:{url}]" if url else "[图片]")
                elif isinstance(seg, Segments.At):
                    qq = getattr(seg, "qq", "")
                    parts.append(f"@{qq}")
                elif isinstance(seg, Segments.Reply):
                    rid = getattr(seg, "id", "")
                    parts.append(f"[回复:{rid}]")
                else:
                    parts.append(f"[{seg.__class__.__name__}]")
            except Exception:
                parts.append("[未知]")
        return " ".join([p for p in parts if p]).strip()

    async def _scheduler_loop(self) -> None:
        while not self._stop:
            try:
                await self.generate_due_memories()
                await self._maybe_cleanup()
                await self._maybe_global_optimize()
            except Exception as e:
                logger.exception("scheduler loop error: %s", e)
            await asyncio.sleep(self.scheduler_tick_seconds)

    async def generate_due_memories(self) -> int:
        now = _now_ts()
        due_scopes = await asyncio.to_thread(self._fetch_due_scopes, now)
        if not due_scopes:
            return 0

        created = 0
        for scope in due_scopes:
            async with self._gen_sem:
                ok = await self._generate_for_scope(scope)
                created += int(ok)
        return created

    async def set_enabled(self, group_id: Any, user_id: Any, is_private: bool, enabled: bool) -> None:
        gid = "p" if is_private else _digits_only(group_id)
        uid = _digits_only(user_id)
        if not uid:
            return
        await asyncio.to_thread(self._set_enabled_sync, gid, uid, 1 if is_private else 0, 1 if enabled else 0)

    def _set_enabled_sync(self, gid: str, uid: str, is_private: int, enabled: int) -> None:
        conn = self._connect_rw()
        try:
            raw_table = raw_table_name(gid, uid, bool(is_private))
            with conn:
                conn.execute(
                    """
                    INSERT INTO memory_settings(group_id, user_id, is_private, enabled, interval_seconds, updated_at)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(group_id, user_id, is_private)
                    DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at;
                    """,
                    (gid, uid, int(is_private), int(enabled), int(self.default_interval_seconds), _now_ts()),
                )
                conn.execute(
                    """
                    INSERT INTO memory_state(group_id, user_id, is_private, raw_table, last_seq, last_generated_at)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(group_id, user_id, is_private) DO NOTHING;
                    """,
                    (gid, uid, int(is_private), raw_table, 0, 0),
                )
        finally:
            conn.close()

    async def set_interval_seconds(self, group_id: Any, user_id: Any, is_private: bool, seconds: int) -> None:
        gid = "p" if is_private else _digits_only(group_id)
        uid = _digits_only(user_id)
        if not uid:
            return
        seconds = max(60, int(seconds))
        await asyncio.to_thread(self._set_interval_sync, gid, uid, 1 if is_private else 0, seconds)

    def _set_interval_sync(self, gid: str, uid: str, is_private: int, seconds: int) -> None:
        conn = self._connect_rw()
        try:
            raw_table = raw_table_name(gid, uid, bool(is_private))
            with conn:
                conn.execute(
                    """
                    INSERT INTO memory_settings(group_id, user_id, is_private, enabled, interval_seconds, updated_at)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(group_id, user_id, is_private)
                    DO UPDATE SET interval_seconds=excluded.interval_seconds, updated_at=excluded.updated_at;
                    """,
                    (gid, uid, int(is_private), int(self.default_enabled), int(seconds), _now_ts()),
                )
                conn.execute(
                    """
                    INSERT INTO memory_state(group_id, user_id, is_private, raw_table, last_seq, last_generated_at)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(group_id, user_id, is_private) DO NOTHING;
                    """,
                    (gid, uid, int(is_private), raw_table, 0, 0),
                )
        finally:
            conn.close()

    async def generate_now(self, group_id: Any, user_id: Any, is_private: bool) -> bool:
        gid = "p" if is_private else _digits_only(group_id)
        uid = _digits_only(user_id)
        if not uid:
            return False
        scope = await asyncio.to_thread(self._fetch_scope_sync, gid, uid, 1 if is_private else 0)
        if not scope:
            return False
        async with self._gen_sem:
            return await self._generate_for_scope(scope, force=True)

    def _fetch_scope_sync(self, gid: str, uid: str, is_private: int) -> Optional[DueScope]:
        conn = self._connect_rw()
        try:
            row = conn.execute(
                """
                SELECT
                    s.group_id, s.user_id, s.is_private, s.interval_seconds,
                    st.last_seq, st.last_generated_at, st.raw_table
                FROM memory_settings s
                JOIN memory_state st
                    ON s.group_id = st.group_id AND s.user_id = st.user_id AND s.is_private = st.is_private
                WHERE s.group_id = ? AND s.user_id = ? AND s.is_private = ?
                LIMIT 1;
                """,
                (gid, uid, int(is_private)),
            ).fetchone()
            if not row:
                raw_table = raw_table_name(gid, uid, bool(is_private))
                with conn:
                    conn.execute(
                        """
                        INSERT INTO memory_settings(group_id, user_id, is_private, enabled, interval_seconds, updated_at)
                        VALUES(?,?,?,?,?,?)
                        ON CONFLICT(group_id, user_id, is_private) DO NOTHING;
                        """,
                        (gid, uid, int(is_private), int(self.default_enabled), int(self.default_interval_seconds), _now_ts()),
                    )
                    conn.execute(
                        """
                        INSERT INTO memory_state(group_id, user_id, is_private, raw_table, last_seq, last_generated_at)
                        VALUES(?,?,?,?,?,?)
                        ON CONFLICT(group_id, user_id, is_private) DO NOTHING;
                        """,
                        (gid, uid, int(is_private), raw_table, 0, 0),
                    )
                row = conn.execute(
                    """
                    SELECT
                        s.group_id, s.user_id, s.is_private, s.interval_seconds,
                        st.last_seq, st.last_generated_at, st.raw_table
                    FROM memory_settings s
                    JOIN memory_state st
                        ON s.group_id = st.group_id AND s.user_id = st.user_id AND s.is_private = st.is_private
                    WHERE s.group_id = ? AND s.user_id = ? AND s.is_private = ?
                    LIMIT 1;
                    """,
                    (gid, uid, int(is_private)),
                ).fetchone()
            if not row:
                return None
            return DueScope(
                group_id=str(row["group_id"]),
                user_id=str(row["user_id"]),
                is_private=int(row["is_private"]),
                interval_seconds=int(row["interval_seconds"]),
                last_seq=int(row["last_seq"]),
                last_generated_at=int(row["last_generated_at"]),
                raw_table=str(row["raw_table"]),
            )
        finally:
            conn.close()

    async def get_status(self, group_id: Any, user_id: Any, is_private: bool) -> Dict[str, Any]:
        gid = "p" if is_private else _digits_only(group_id)
        uid = _digits_only(user_id)
        if not uid:
            return {}
        return await asyncio.to_thread(self._get_status_sync, gid, uid, 1 if is_private else 0)

    def _get_status_sync(self, gid: str, uid: str, is_private: int) -> Dict[str, Any]:
        conn = self._connect_rw()
        try:
            row = conn.execute(
                """
                SELECT
                    s.enabled, s.interval_seconds, s.updated_at,
                    st.last_seq, st.last_generated_at, st.raw_table
                FROM memory_settings s
                JOIN memory_state st
                    ON s.group_id = st.group_id AND s.user_id = st.user_id AND s.is_private = st.is_private
                WHERE s.group_id = ? AND s.user_id = ? AND s.is_private = ?
                LIMIT 1;
                """,
                (gid, uid, int(is_private)),
            ).fetchone()
            if not row:
                return {"enabled": 0, "interval_seconds": self.default_interval_seconds, "last_generated_at": 0}

            raw_table = str(row["raw_table"])
            raw_count = 0
            new_raw_count = 0
            try:
                raw_count = int(conn.execute(f"SELECT COUNT(1) AS c FROM {raw_table};").fetchone()["c"])
                new_raw_count = int(
                    conn.execute(f"SELECT COUNT(1) AS c FROM {raw_table} WHERE seq > ?;", (int(row["last_seq"]),)).fetchone()[
                        "c"
                    ]
                )
            except sqlite3.OperationalError:
                pass

            mem_table = mem_table_name(gid, uid, bool(is_private))
            mem_count = 0
            try:
                mem_count = int(conn.execute(f"SELECT COUNT(1) AS c FROM {mem_table};").fetchone()["c"])
            except sqlite3.OperationalError:
                pass

            global_count = int(conn.execute("SELECT COUNT(1) AS c FROM mem_global;").fetchone()["c"])

            return {
                "enabled": int(row["enabled"]),
                "interval_seconds": int(row["interval_seconds"]),
                "updated_at": int(row["updated_at"]),
                "last_seq": int(row["last_seq"]),
                "last_generated_at": int(row["last_generated_at"]),
                "raw_count": raw_count,
                "new_raw_count": new_raw_count,
                "mem_count": mem_count,
                "global_count": global_count,
            }
        finally:
            conn.close()

    def _connect_rw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _ensure_mem_table(self, conn: sqlite3.Connection, table: str) -> None:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
                memory_id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                memory_content TEXT NOT NULL,
                generated_at INTEGER NOT NULL,
                weight REAL NOT NULL
            );
            """
        )
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_at ON {table}(generated_at);")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_weight ON {table}(weight);")

    def _fetch_due_scopes(self, now_ts: int) -> List[DueScope]:
        conn = self._connect_rw()
        try:
            rows = conn.execute(
                """
                SELECT
                    s.group_id, s.user_id, s.is_private, s.interval_seconds,
                    st.last_seq, st.last_generated_at, st.raw_table
                FROM memory_settings s
                JOIN memory_state st
                    ON s.group_id = st.group_id AND s.user_id = st.user_id AND s.is_private = st.is_private
                WHERE s.enabled = 1
                  AND (st.last_generated_at + s.interval_seconds) <= ?
                ORDER BY st.last_generated_at ASC
                LIMIT 50;
                """,
                (int(now_ts),),
            ).fetchall()
            scopes: List[DueScope] = []
            for r in rows:
                scopes.append(
                    DueScope(
                        group_id=str(r["group_id"]),
                        user_id=str(r["user_id"]),
                        is_private=int(r["is_private"]),
                        interval_seconds=int(r["interval_seconds"]),
                        last_seq=int(r["last_seq"]),
                        last_generated_at=int(r["last_generated_at"]),
                        raw_table=str(r["raw_table"]),
                    )
                )
            return scopes
        finally:
            conn.close()

    def _fetch_new_raw_rows(self, scope: DueScope) -> Tuple[List[sqlite3.Row], int]:
        conn = self._connect_rw()
        try:
            rows = conn.execute(
                f"""
                SELECT seq, sender, content, timestamp, message_type
                FROM {scope.raw_table}
                WHERE seq > ?
                ORDER BY seq ASC
                LIMIT ?;
                """,
                (int(scope.last_seq), int(self.max_raw_rows_per_generation)),
            ).fetchall()
            if not rows:
                return [], scope.last_seq
            max_seq = int(rows[-1]["seq"])
            return list(rows), max_seq
        finally:
            conn.close()

    async def _generate_for_scope(self, scope: DueScope, force: bool = False) -> bool:
        raw_rows, max_seq = await asyncio.to_thread(self._fetch_new_raw_rows, scope)
        if len(raw_rows) < (1 if force else self.min_new_rows_to_generate):
            return False

        raw_text_lines: List[str] = []
        total_chars = 0
        for r in raw_rows:
            line = f'{r["timestamp"]} {r["sender"]}: {r["content"]}'
            total_chars += len(line) + 1
            if total_chars > self.max_chars_per_generation:
                break
            raw_text_lines.append(line)

        persona = self._persona_cache.get(scope.user_id, "")
        sys_prompt = (persona + "\n\n" if persona else "") + (
            "你现在负责把聊天增量提炼成长期记忆。输出必须是严格JSON，且只能输出JSON。"
            "JSON格式：{\"memories\":[{\"content\":\"...\",\"weight\":0.0}]}。"
            "content用简洁中文描述事实型信息，避免复述原句，避免敏感信息。weight范围0到1。"
        )

        msg = "以下是新增聊天记录，请提炼记忆：\n" + "\n".join(raw_text_lines)
        text = await self._call_memory_ai(scope, msg, sys_prompt)
        items = self._parse_memory_items(text)
        if not items:
            items = [MemoryItem(content=text.strip()[:800], weight=0.3)] if text and text.strip() else []
        if not items:
            return False

        ok = await asyncio.to_thread(self._store_memories_and_advance, scope, items, max_seq)
        return ok

    async def _call_memory_ai(self, scope: DueScope, msg: str, sys_prompt: str) -> str:
        import Tools.ARC_AI as ARC_AI

        uid = f"mem_{scope.group_id}_{scope.user_id}_{scope.is_private}"
        response_stream = ARC_AI.get_response_stream(
            self.memory_mode,
            msg,
            self._memory_user_lists,
            uid,
            sys_prompt,
            images=[],
        )
        out = ""
        async for partial, r_type in response_stream:
            if r_type == "message":
                out += str(partial)
        return out

    def _parse_memory_items(self, text: str) -> List[MemoryItem]:
        if not text:
            return []
        candidate = text.strip()
        candidate = re.sub(r"^```(json)?", "", candidate, flags=re.IGNORECASE).strip()
        candidate = re.sub(r"```$", "", candidate).strip()
        if "{" in candidate and "}" in candidate:
            candidate = candidate[candidate.find("{") : candidate.rfind("}") + 1]
        try:
            data = json.loads(candidate)
        except Exception:
            return []
        memories = data.get("memories")
        if not isinstance(memories, list):
            return []
        items: List[MemoryItem] = []
        for m in memories:
            if not isinstance(m, dict):
                continue
            content = str(m.get("content") or "").strip()
            if not content:
                continue
            try:
                weight = float(m.get("weight", 0.3))
            except Exception:
                weight = 0.3
            weight = max(0.0, min(1.0, weight))
            items.append(MemoryItem(content=content[:1200], weight=weight))
        return items[:20]

    def _store_memories_and_advance(self, scope: DueScope, items: List[MemoryItem], max_seq: int) -> bool:
        conn = self._connect_rw()
        try:
            table = mem_table_name(scope.group_id, scope.user_id, bool(scope.is_private))
            self._ensure_mem_table(conn, table)
            now = _now_ts()
            with conn:
                for it in items:
                    conn.execute(
                        f"INSERT INTO {table}(group_id, user_id, memory_content, generated_at, weight) VALUES(?,?,?,?,?);",
                        (scope.group_id, scope.user_id, it.content, now, it.weight),
                    )
                conn.execute(
                    """
                    UPDATE memory_state
                    SET last_seq = ?, last_generated_at = ?
                    WHERE group_id = ? AND user_id = ? AND is_private = ?;
                    """,
                    (int(max_seq), int(now), scope.group_id, scope.user_id, int(scope.is_private)),
                )
            return True
        except Exception as e:
            logger.exception("store memories error: %s", e)
            return False
        finally:
            conn.close()

    async def build_memory_context(
        self,
        group_id: Any,
        user_id: Any,
        is_private: bool,
        query_text: str,
        topk: int = 6,
        max_chars: int = 1800,
    ) -> str:
        gid = "p" if is_private else _digits_only(group_id)
        uid = _digits_only(user_id)
        if not uid:
            return ""
        table = mem_table_name(gid, uid, is_private)
        candidates = await asyncio.to_thread(self._fetch_memory_candidates, table)
        if not candidates:
            return ""
        selected = self._rank_memories(candidates, query_text, topk=topk)
        if not selected:
            return ""
        lines: List[str] = []
        total = 0
        for content, score in selected:
            line = f"- {content}"
            total += len(line) + 1
            if total > max_chars:
                break
            lines.append(line)
        if not lines:
            return ""
        return "简儿记忆（与当前对话相关的长期信息）：\n" + "\n".join(lines)

    def _fetch_memory_candidates(self, mem_table: str) -> List[Tuple[str, float, int]]:
        conn = self._connect_rw()
        try:
            items: List[Tuple[str, float, int]] = []
            rows = conn.execute(
                f"""
                SELECT memory_content, weight, generated_at
                FROM {mem_table}
                ORDER BY generated_at DESC
                LIMIT 60;
                """
            ).fetchall()
            for r in rows:
                items.append((str(r["memory_content"]), float(r["weight"]), int(r["generated_at"])))

            rows_g = conn.execute(
                """
                SELECT memory_content, weight, generated_at
                FROM mem_global
                ORDER BY weight DESC, generated_at DESC
                LIMIT 30;
                """
            ).fetchall()
            for r in rows_g:
                items.append((str(r["memory_content"]), float(r["weight"]), int(r["generated_at"])))
            return items
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    def _rank_memories(
        self,
        candidates: List[Tuple[str, float, int]],
        query_text: str,
        topk: int,
    ) -> List[Tuple[str, float]]:
        now = _now_ts()
        q_tokens = self._tokenize(query_text or "")
        scored: List[Tuple[str, float]] = []
        for content, weight, generated_at in candidates:
            if not content:
                continue
            c_tokens = self._tokenize(content)
            overlap = 0.0
            if q_tokens and c_tokens:
                inter = len(q_tokens.intersection(c_tokens))
                overlap = min(1.0, inter / 8.0)

            age_s = max(0, now - int(generated_at))
            recency = math.exp(-age_s / (7 * 24 * 3600))
            w = max(0.0, min(1.0, float(weight)))
            score = 0.55 * w + 0.35 * recency + 0.10 * overlap
            scored.append((content, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        out: List[Tuple[str, float]] = []
        seen: set[str] = set()
        for content, score in scored:
            key = content.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append((key, score))
            if len(out) >= int(topk):
                break
        return out

    def _tokenize(self, text: str) -> set[str]:
        t = (text or "").lower()
        tokens: set[str] = set()
        for w in re.findall(r"[a-z0-9_]{2,}", t):
            tokens.add(w)
        for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", t):
            if len(chunk) <= 4:
                tokens.add(chunk)
            for i in range(len(chunk) - 1):
                tokens.add(chunk[i : i + 2])
        return tokens

    async def _maybe_cleanup(self) -> None:
        now = _now_ts()
        if self.cleanup_interval_seconds <= 0:
            return
        if self._last_cleanup_at and now - self._last_cleanup_at < self.cleanup_interval_seconds:
            return
        self._last_cleanup_at = now
        await asyncio.to_thread(self._cleanup_memories)

    def _cleanup_memories(self) -> None:
        cutoff = _now_ts() - max(1, self.cleanup_keep_days) * 24 * 3600
        conn = self._connect_rw()
        try:
            tables = conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type='table' AND (name LIKE 'mem_g%' OR name LIKE 'mem_p%')
                ORDER BY name;
                """
            ).fetchall()
            with conn:
                for r in tables:
                    table = str(r["name"])
                    conn.execute(
                        f"DELETE FROM {table} WHERE generated_at < ? AND weight < ?;",
                        (int(cutoff), float(self.cleanup_min_weight)),
                    )
                    over = conn.execute(f"SELECT COUNT(1) AS c FROM {table};").fetchone()
                    if over and int(over["c"]) > self.cleanup_keep_max_rows:
                        to_del = int(over["c"]) - self.cleanup_keep_max_rows
                        conn.execute(
                            f"""
                            DELETE FROM {table}
                            WHERE memory_id IN (
                                SELECT memory_id FROM {table}
                                ORDER BY generated_at ASC
                                LIMIT ?
                            );
                            """,
                            (int(to_del),),
                        )

            conn.execute(
                "DELETE FROM mem_global WHERE generated_at < ? AND weight < ?;",
                (int(cutoff), float(self.cleanup_min_weight)),
            )
        except Exception as e:
            logger.exception("cleanup error: %s", e)
        finally:
            conn.close()

    async def _maybe_global_optimize(self) -> None:
        now = _now_ts()
        if self.global_optimize_interval_seconds <= 0:
            return
        if self._last_global_optimize_at and now - self._last_global_optimize_at < self.global_optimize_interval_seconds:
            return
        self._last_global_optimize_at = now
        await self.generate_global_memory()

    async def generate_global_memory(self) -> bool:
        candidates = await asyncio.to_thread(self._collect_global_candidates)
        if not candidates:
            return False

        sys_prompt = (
            "你现在负责从多处记忆中提炼全局记忆。输出必须是严格JSON，且只能输出JSON。"
            "JSON格式：{\"memories\":[{\"content\":\"...\",\"weight\":0.0}]}。"
            "content要去重、抽象总结，可跨群共用。weight范围0到1。"
        )
        msg = "以下是候选记忆，请生成全局记忆：\n" + "\n".join([f"- {c}" for c in candidates])
        text = await self._call_memory_ai_text("mem_global", msg, sys_prompt)
        items = self._parse_memory_items(text)
        if not items:
            return False
        ok = await asyncio.to_thread(self._store_global_memories, items)
        return ok

    def _collect_global_candidates(self) -> List[str]:
        conn = self._connect_rw()
        try:
            tables = conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type='table' AND (name LIKE 'mem_g%' OR name LIKE 'mem_p%')
                ORDER BY name;
                """
            ).fetchall()
            collected: List[Tuple[str, float, int]] = []
            for r in tables:
                table = str(r["name"])
                try:
                    rows = conn.execute(
                        f"""
                        SELECT memory_content, weight, generated_at
                        FROM {table}
                        ORDER BY weight DESC, generated_at DESC
                        LIMIT 20;
                        """
                    ).fetchall()
                except sqlite3.OperationalError:
                    continue
                for x in rows:
                    collected.append((str(x["memory_content"]), float(x["weight"]), int(x["generated_at"])))

            collected.sort(key=lambda x: (x[1], x[2]), reverse=True)
            out: List[str] = []
            seen: set[str] = set()
            for content, weight, _ in collected:
                k = content.strip()
                if not k or k in seen:
                    continue
                seen.add(k)
                out.append(k)
                if len(out) >= self.global_candidate_limit:
                    break
            return out
        finally:
            conn.close()

    async def _call_memory_ai_text(self, uid: str, msg: str, sys_prompt: str) -> str:
        import Tools.ARC_AI as ARC_AI

        response_stream = ARC_AI.get_response_stream(
            self.memory_mode,
            msg,
            self._memory_user_lists,
            uid,
            sys_prompt,
            images=[],
        )
        out = ""
        async for partial, r_type in response_stream:
            if r_type == "message":
                out += str(partial)
        return out

    def _store_global_memories(self, items: List[MemoryItem]) -> bool:
        conn = self._connect_rw()
        try:
            now = _now_ts()
            with conn:
                for it in items[:30]:
                    conn.execute(
                        "INSERT INTO mem_global(memory_content, generated_at, weight) VALUES(?,?,?);",
                        (it.content, int(now), float(it.weight)),
                    )
            return True
        except Exception as e:
            logger.exception("store global error: %s", e)
            return False
        finally:
            conn.close()
