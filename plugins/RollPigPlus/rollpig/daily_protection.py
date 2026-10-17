from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from ..host import logger

from .daily_report import NormalizedDailyEvent, normalize_daily_events
from .runtime import ROLLPIG_TIMEZONE, is_group_rollpig_enabled, rollpig_now
from .store.base import RollpigStore


# ================================ 保护规则与补偿记录 ================================ #
# 短重试耗尽后最多补试五次。次数和绝对时间一同落盘，重启不能重置预算。
PROTECTION_RECOVERY_DELAYS = (30, 60, 120, 300, 600)


def select_daily_protected_user_ids(events: Sequence[NormalizedDailyEvent]) -> list[str]:
    """沿用现有规则：被成功烤至少两次且次数最高的一人获得次日保护。"""

    roasted_counter: Counter[str] = Counter()
    for event in events:
        if event.event_type == "success" and event.target_id and event.target_id != event.attacker_id:
            roasted_counter[event.target_id] += 1
    if not roasted_counter:
        return []
    user_id, count = roasted_counter.most_common(1)[0]
    return [user_id] if count >= 2 else []


@dataclass(frozen=True)
class ProtectionRecoveryRecord:
    group_id: str
    date_str: str
    protect_date: str
    cutoff_at: str
    protected_ids: tuple[str, ...] | None
    attempt_count: int
    next_attempt_at: str

    @property
    def key(self) -> tuple[str, str]:
        return self.date_str, self.group_id


class ProtectionRecoveryQueue:
    """仅持久化失败群的保护结算，不保存事件或承担日报投递。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._records: dict[tuple[str, str], ProtectionRecoveryRecord] = {}
        self._loaded = False
        self._lock = asyncio.Lock()
        self._run_lock = asyncio.Lock()

    # ================================ 本地原子持久化 ================================ #
    def _read_sync(self) -> dict[tuple[str, str], ProtectionRecoveryRecord]:
        """恢复固定日期和预算；损坏文件不按空队列覆盖，保留人工恢复机会。"""

        if not self.path.exists():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("保护补偿文件格式无效")
        rows = payload.get("records")
        if not isinstance(rows, list):
            raise ValueError("保护补偿 records 必须为列表")
        records = {}
        for row in rows:
            record = ProtectionRecoveryRecord(**row)
            date = dt.date.fromisoformat(record.date_str)
            protect_date = dt.date.fromisoformat(record.protect_date)
            cutoff = dt.datetime.fromisoformat(record.cutoff_at)
            next_attempt = dt.datetime.fromisoformat(record.next_attempt_at)
            if (
                not isinstance(record.group_id, str) or not record.group_id
                or protect_date != date + dt.timedelta(days=1)
                or cutoff.tzinfo is None or next_attempt.tzinfo is None
                or cutoff.astimezone(ROLLPIG_TIMEZONE).date() != date
                or type(record.attempt_count) is not int
                or not 0 <= record.attempt_count <= len(PROTECTION_RECOVERY_DELAYS)
                or (record.protected_ids is not None and (
                    not isinstance(record.protected_ids, list)
                    or not all(isinstance(user_id, str) and user_id for user_id in record.protected_ids)
                ))
                or record.key in records
            ):
                raise ValueError("保护补偿记录无效")
            records[record.key] = replace(
                record,
                protected_ids=tuple(record.protected_ids) if record.protected_ids is not None else None,
            )
        return records

    async def _load(self) -> None:
        """在队列锁内惰性加载，空队列检查不重复读盘。"""

        if not self._loaded:
            self._records = await asyncio.to_thread(self._read_sync)
            self._loaded = True

    def _write_sync(self, records: dict[tuple[str, str], ProtectionRecoveryRecord]) -> None:
        """原子替换轻量 JSON，避免中途退出留下半个文件。"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {"version": 1, "records": [asdict(record) for record in records.values()]}
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    async def _save(self, records: dict[tuple[str, str], ProtectionRecoveryRecord]) -> None:
        """写入成功才更新内存；取消时等写线程收尾后再释放锁。"""

        writing = asyncio.create_task(asyncio.to_thread(self._write_sync, records))
        try:
            await asyncio.shield(writing)
        except asyncio.CancelledError:
            await writing
            self._records = records
            raise
        self._records = records

    async def enqueue(
        self, group_id: str, date_str: str, protect_date: str, cutoff_at: str,
        *, protected_ids: Sequence[str] | None,
    ) -> None:
        """登记短重试耗尽的群；重复登记不重置次数，可补齐已算出的名单。"""

        async with self._lock:
            await self._load()
            key = (date_str, group_id)
            existing = self._records.get(key)
            if existing is None:
                record = ProtectionRecoveryRecord(
                    group_id, date_str, protect_date, cutoff_at,
                    tuple(protected_ids) if protected_ids is not None else None, 0,
                    (rollpig_now() + dt.timedelta(seconds=PROTECTION_RECOVERY_DELAYS[0])).isoformat(),
                )
            else:
                if existing.protect_date != protect_date or existing.cutoff_at != cutoff_at:
                    raise ValueError("同群同日的保护补偿截止点不可变更")
                record = replace(existing, protected_ids=tuple(protected_ids)) if (
                    existing.protected_ids is None and protected_ids is not None
                ) else existing
                if record == existing:
                    return
            await self._save({**self._records, key: record})
        logger.warning(f"[猪圈日报] 次日保护已登记补偿: group={group_id} date={date_str}")

    async def discard(self, date_str: str, group_id: str) -> None:
        """结算成功后立即移除该群，不影响其他失败群。"""

        async with self._lock:
            await self._load()
            key = (date_str, group_id)
            if key in self._records:
                await self._save({key_value: value for key_value, value in self._records.items() if key_value != key})

    # ================================ 有限补偿与重启恢复 ================================ #
    async def run_due(self, report_store: RollpigStore, *, now: dt.datetime | None = None) -> None:
        """按持久化时间补写到期失败群；串行执行，过保护日或关群即停止。"""

        if self._run_lock.locked():
            return
        async with self._run_lock:
            current = (now or rollpig_now()).astimezone(ROLLPIG_TIMEZONE)
            async with self._lock:
                await self._load()
                retained = {
                    key: record for key, record in self._records.items()
                    if record.protect_date >= current.date().isoformat()
                    and is_group_rollpig_enabled(record.group_id)
                }
                if retained != self._records:
                    await self._save(retained)
                due_keys = [
                    key for key, record in self._records.items()
                    if record.attempt_count < len(PROTECTION_RECOVERY_DELAYS)
                    and dt.datetime.fromisoformat(record.next_attempt_at) <= current
                ]
            for key in due_keys:
                await self._recover_group(report_store, key, now=now)

    async def _recover_group(
        self, report_store: RollpigStore, key: tuple[str, str], *, now: dt.datetime | None,
    ) -> None:
        """先落盘本次预算，再补查或幂等写入；响应丢失也不会重复发日报。"""

        current = (now or rollpig_now()).astimezone(ROLLPIG_TIMEZONE)
        async with self._lock:
            record = self._records.get(key)
            if record is None:
                return
            if record.protect_date < current.date().isoformat() or not is_group_rollpig_enabled(record.group_id):
                await self._save({key_value: value for key_value, value in self._records.items() if key_value != key})
                return
            attempt = record.attempt_count + 1
            delay = PROTECTION_RECOVERY_DELAYS[min(attempt, len(PROTECTION_RECOVERY_DELAYS) - 1)]
            record = replace(
                record, attempt_count=attempt,
                next_attempt_at=(current + dt.timedelta(seconds=delay)).isoformat(),
            )
            await self._save({**self._records, key: record})

        try:
            if record.protected_ids is None:
                query = await report_store.query_daily_events(
                    date_str=record.date_str, group_id=record.group_id, cutoff_at=record.cutoff_at,
                )
                if not query.available:
                    raise RuntimeError("保护补偿事件记录暂时不可用")
                protected_ids = select_daily_protected_user_ids(normalize_daily_events(
                    query.items, group_id=record.group_id, cutoff_at=record.cutoff_at,
                ))
                async with self._lock:
                    if key not in self._records:
                        return
                    record = replace(record, protected_ids=tuple(protected_ids))
                    await self._save({**self._records, key: record})

            # 查询或写盘期间可能跨日、关群；保护补偿只补权益，不追溯已过期日期。
            current = (now or rollpig_now()).astimezone(ROLLPIG_TIMEZONE)
            if record.protect_date < current.date().isoformat() or not is_group_rollpig_enabled(record.group_id):
                await self.discard(record.date_str, record.group_id)
                return
            await report_store.replace_group_protections(
                record.group_id, list(record.protected_ids), record.protect_date,
            )
            await self.discard(record.date_str, record.group_id)
            logger.info(f"[猪圈日报] 次日保护补偿成功: group={record.group_id} date={record.date_str}")
        except Exception as error:
            exhausted = record.attempt_count >= len(PROTECTION_RECOVERY_DELAYS)
            logger.warning(
                f"[猪圈日报] 保护补偿{'次数耗尽，停止重试' if exhausted else '失败，等待下次重试'}: "
                f"group={record.group_id} date={record.date_str} "
                f"attempt={record.attempt_count}/{len(PROTECTION_RECOVERY_DELAYS)} error={error}"
            )
