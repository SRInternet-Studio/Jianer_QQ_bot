import json
import asyncio
import datetime
import shutil
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import List, Optional

from ..host import logger
from ..host import localstore as store

from .runtime import rollpig_date_str, rollpig_today, resolve_roast_cooldown_seconds
from .store.models import (
    CatalogSnapshot,
    CooldownConsumeResult,
    DailyFeedResult,
    DailyReportDeliveryClaim,
    DailyReportDeliveryClaimResult,
    DailyReportDeliveryTransitionResult,
    DailyReportProfileSnapshot,
    DailyRollResult,
    DailyRollSnapshot,
    DrawState,
    GroupRoastRefillCompleteResult,
    GroupRoastRefillPrepareResult,
    GroupRoastRefillRequest,
    PigProgress,
    RoastEvent,
    RoastReservation,
    RoastReservationClaimResult,
    RoastReservationParticipant,
    RoastReservationPrepareResult,
    UnrolledRoastAttemptResult,
    ROAST_REFILL_MIN_DISTINCT_VOTERS,
    ROAST_REFILL_VOTE_WEIGHT_POLICY,
    expert_level_from_copies,
    parse_applied_daily_feed,
    roast_refill_threshold,
)

ROAST_COOLDOWN_SECONDS = resolve_roast_cooldown_seconds()
DEFAULT_ROAST_CHARGE_MAX = 2
ROAST_RESERVATION_MAX_PARTICIPANTS = 12
ROAST_RESERVATION_CLAIM_TIMEOUT_SECONDS = 5 * 60
ROAST_RESERVATION_CROSS_DAY_GRACE_SECONDS = 10 * 60
ROAST_REFILL_TTL_SECONDS = 10 * 60


# ================================ 预约跨日重试 ================================ #


def _matches_reservation_claim_date(raw: dict, target_date: str, now: datetime.datetime) -> bool:
    """匹配当日预约，并为刚在零点后释放的昨日发送前任务保留短暂重试窗口。"""

    reservation_date = str(raw.get("date_str") or "")
    if reservation_date == target_date:
        return True
    if raw.get("status") not in {"ready", "processing", "prepared"}:
        return False
    released_at_raw = raw.get("released_at")
    if not isinstance(released_at_raw, str):
        return False
    try:
        reservation_day = datetime.date.fromisoformat(reservation_date)
        target_day = datetime.date.fromisoformat(target_date)
        released_at = datetime.datetime.fromisoformat(released_at_raw)
    except ValueError:
        return False
    if reservation_day != target_day - datetime.timedelta(days=1):
        return False
    if released_at.tzinfo is None:
        released_at = released_at.replace(tzinfo=datetime.timezone.utc)
    elapsed = (now - released_at).total_seconds()
    return 0 <= elapsed <= ROAST_RESERVATION_CROSS_DAY_GRACE_SECONDS


def _normalize_charge_settings(cooldown_seconds: Optional[int], max_charges: Optional[int]) -> tuple[int, int]:
    """本地 JSON 与 cloud 使用同一组边界，避免两种后端表现不一致。"""
    cooldown = max(60, int(cooldown_seconds or ROAST_COOLDOWN_SECONDS))
    charge_max = max(1, min(6, int(max_charges or DEFAULT_ROAST_CHARGE_MAX)))
    return cooldown, charge_max


def _legacy_roast_state(last_use: float, now: float, cooldown: int, max_charges: int) -> tuple[int, float]:
    """把旧单时间戳 CD 宽松迁移为充能桶：最近一次使用后视为还剩 1 格。"""
    if last_use <= 0:
        return max_charges, now
    elapsed = max(0.0, now - last_use)
    recovered = int(elapsed // cooldown)
    charges = min(max_charges, 1 + recovered)
    updated_ts = now if charges >= max_charges else last_use + recovered * cooldown
    return int(charges), float(updated_ts)


def _recover_roast_charges(charges: int, updated_ts: float, now: float, cooldown: int, max_charges: int) -> tuple[int, float]:
    """按 token bucket 恢复普通烤群友次数；满格后把锚点归到当前时间。"""
    charges = max(0, min(max_charges, int(charges)))
    updated_ts = float(updated_ts or now)
    if charges >= max_charges:
        return max_charges, now
    elapsed = max(0.0, now - updated_ts)
    recovered = int(elapsed // cooldown)
    if recovered <= 0:
        return charges, updated_ts
    charges = min(max_charges, charges + recovered)
    updated_ts = now if charges >= max_charges else updated_ts + recovered * cooldown
    return int(charges), float(updated_ts)


def _next_charge_seconds(charges: int, updated_ts: float, now: float, cooldown: int, max_charges: int) -> int:
    if charges >= max_charges:
        return 0
    elapsed = max(0.0, now - float(updated_ts or now))
    return max(1, int(cooldown - (elapsed % cooldown)))

# ================= 数据管理 =================

DATA_FILE = store.get_plugin_data_file("pig_data.json")
DATA_BACKUP_COUNT = 2
DATA_BACKUP_MIN_INTERVAL_SECONDS = 60.0
DATA_BACKUP_MAX_WRITES = 10


class LocalStoreUnavailableError(RuntimeError):
    """本地账本无法安全写入；调用入口应转换为用户可读提示。"""


class PigDataManager:
    """
    负责插件所有持久化数据的读写。

    数据结构：
    - history    : {date: {user_id: pig_id}}  ← 新格式，仅存 pig_id（14天后自动清理）
                   旧版存完整 pig dict，_migrate() 会自动转换
    - daily_roll_snapshots: {date: {user_id: snapshot}} ← 抽取时成长与资源快照
    - group_rolls: {date: {group_id: {user_id: pig_id}}} ← 群内“今日已抽/已显形”记录
    - group_roll_seen_at: {date: {group_id: {user_id: ISO时间}}} ← 日报固定截止快照
    - collection : {user_id: [pig_id, ...]}   ← 永久保留，图鉴数据
    - pig_progress: {user_id: {pig_id: {copies, first_obtained_at}}} ← P1A 抽到次数/专家等级
    - daily_feeds: {date: {user_id: feed_result}} ← 每日用户级加餐结果
    - draw_state : {user_id: {duplicate_streak}} ← P1A 连续重复次数，用于伪保底
    - usage      : {user_id: {last_roast_ts, roast_charges, roast_charge_updated_ts}} ← 普通烤群友充能
    - force_usage: {user_id: "YYYY-MM-DD"}    ← 后门口令每日计数
    - daily_events: {date: [event, ...]}      ← 群内烧烤事件（用于日报）
    - unrolled_roast_attempts: {date: {user_id: count}} ← 未抽猪先烤的每日违规次数
    - roast_reservations: {reservation_id: reservation} ← 延迟到目标抽猪后结算的群预约
    - group_daily_active_users: {date: {group_id: [user_id, ...]}} ← 群日活玩家
    - group_daily_active_at: {date: {group_id: {user_id: ISO时间}}} ← 群日活首次登记时间
    - roast_refill_requests: {request_id: request} ← 烤箱补货投票及结果
    - daily_report_deliveries: {date: {group_id: delivery}} ← Local 日报投递恢复状态

    写操作通过 asyncio.Lock 串行化，文件使用原子替换（.tmp → rename）防止 JSON 损坏。
    """

    def __init__(self):
        self.file = DATA_FILE
        self._lock = asyncio.Lock()
        self._load_failed = False
        self._skip_backup_rotation_once = False
        self._writes_since_backup_rotation = 0
        self._last_backup_rotation_monotonic = time.monotonic()
        self.data = self._load()
        self._restore_backup_rotation_clock()

    # ---- 加载与迁移 ----

    def _default_data(self) -> dict:
        return {
            "history": {},
            "daily_roll_snapshots": {},
            "group_rolls": {},
            "group_roll_seen_at": {},
            "collection": {},
            "pig_progress": {},
            "daily_feeds": {},
            "draw_state": {},
            "usage": {},
            "force_usage": {},
            "daily_events": {},
            "protected": {},
            "unrolled_roast_attempts": {},
            "roast_reservations": {},
            "group_daily_active_users": {},
            "group_daily_active_at": {},
            "roast_refill_requests": {},
            "daily_report_deliveries": {},
        }

    def _load(self) -> dict:
        if not self.file.exists():
            default = self._default_data()
            self.file.parent.mkdir(parents=True, exist_ok=True)
            self.file.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
            return default
        try:
            raw = json.loads(self.file.read_text("utf-8"))
            return self._migrate(raw)
        except Exception as e:
            self._load_failed = True
            logger.error(f"pig_data.json 读取失败，进入写保护模式以避免覆盖旧数据: {e}")
            self._preserve_broken_file()

            recovered = self._load_backup()
            if recovered is not None:
                logger.warning("pig_data.json 已从备份恢复，并写回主文件。")
                self._load_failed = False
                self.data = recovered
                self._skip_backup_rotation_once = True
                self._sync_save()
                return recovered

            logger.error("pig_data.json 没有可用备份；本地存储写操作将被拒绝，请手动修复数据文件。")
            return self._default_data()

    def _migrate(self, data: dict, *, persist: bool = True) -> dict:
        """将旧版 history（存完整 pig dict）迁移为新版（只存 pig_id 字符串）。
        迁移完成后立即同步落盘，防止进程在第一次写入前已退出导致磁盘仍为旧格式。
        """
        if not isinstance(data, dict):
            data = {}

        migrated = False
        for key in (
            "history",
            "daily_roll_snapshots",
            "group_rolls",
            "group_roll_seen_at",
            "collection",
            "pig_progress",
            "daily_feeds",
            "draw_state",
            "usage",
            "force_usage",
            "daily_events",
            "unrolled_roast_attempts",
            "roast_reservations",
            "group_daily_active_users",
            "group_daily_active_at",
            "roast_refill_requests",
            "daily_report_deliveries",
        ):
            if not isinstance(data.get(key), dict):
                data[key] = {}
                migrated = True
        if not isinstance(data.get("protected"), dict):
            data["protected"] = {}
            migrated = True

        history = data.get("history", {})
        for date_str, records in history.items():
            if not isinstance(records, dict):
                continue
            for uid, val in list(records.items()):
                if isinstance(val, dict) and "id" in val:
                    records[uid] = val["id"]
                    migrated = True

        # ================================ 历史快照容错 ================================ #
        # 快照只是 history 的可选补充；单个日期桶损坏时直接隔离，不能让主数据文件
        # 整体进入写保护，也不能影响旧 history 继续提供昨日身份。
        daily_roll_snapshots = data.get("daily_roll_snapshots", {})
        for date_str, records in list(daily_roll_snapshots.items()):
            if isinstance(records, dict):
                continue
            logger.warning(f"rollpig 每日抽取快照日期桶损坏，已隔离: date={date_str}")
            daily_roll_snapshots[date_str] = {}
            migrated = True

        # ================================ P1A成长状态回填 ================================ #
        # 旧版本地数据只有 collection，只能确认“曾经拥有过”，无法还原真实重复次数。
        # 因此升级时保守初始化为 copies=1，之后每天首次抽到重复猪才继续递增。
        collection = data.get("collection", {})
        pig_progress = data.setdefault("pig_progress", {})
        draw_state = data.setdefault("draw_state", {})
        for user_id, pig_ids in collection.items():
            if not isinstance(pig_ids, list):
                continue
            user_progress = pig_progress.setdefault(str(user_id), {})
            if not isinstance(user_progress, dict):
                user_progress = {}
                pig_progress[str(user_id)] = user_progress
                migrated = True
            for pig_id in pig_ids:
                pig_id = str(pig_id)
                item = user_progress.get(pig_id)
                if not isinstance(item, dict):
                    user_progress[pig_id] = {
                        "copies": 1,
                        "growth_bonus": 0,
                        "first_obtained_at": None,
                    }
                    migrated = True
                elif _safe_int(item.get("copies"), 0) <= 0:
                    item["copies"] = 1
                    migrated = True
                if isinstance(item, dict) and _safe_int(item.get("growth_bonus"), -1) < 0:
                    item["growth_bonus"] = 0
                    migrated = True
            state = draw_state.get(str(user_id))
            if not isinstance(state, dict):
                draw_state[str(user_id)] = {"duplicate_streak": 0}
                migrated = True
            elif _safe_int(state.get("duplicate_streak"), 0) < 0:
                state["duplicate_streak"] = 0
                migrated = True

        # ================================ 每日加餐记录容错 ================================ #
        # 加餐记录只保存真实成长结果。单个日期桶或条目损坏时直接隔离，避免后续
        # 结算在 setdefault 链路上报错，也不能让整份本地账本进入写保护。
        daily_feeds = data.setdefault("daily_feeds", {})
        normalized_daily_feeds: dict[str, dict[str, dict]] = {}
        for date_str, day_feeds in daily_feeds.items():
            if not _is_valid_date(str(date_str)) or not isinstance(day_feeds, dict):
                continue
            normalized_day: dict[str, dict] = {}
            for user_id, raw_feed in day_feeds.items():
                if not user_id or not isinstance(raw_feed, dict):
                    continue
                pig_id = str(raw_feed.get("pig_id") or "")
                normalized_user_id = str(user_id)
                # 兼容早期省略身份/状态的本地记录，但不能覆盖显式错误值，
                # 更不能先钳制坏等级、再把它当作有效升级保存。
                candidate = {"status": "fed", "user_id": normalized_user_id, **raw_feed}
                feed = parse_applied_daily_feed(candidate, pig_id=pig_id, user_id=normalized_user_id)
                if pig_id and feed is not None:
                    normalized_day[normalized_user_id] = self._daily_feed_to_raw(feed)
            if normalized_day:
                normalized_daily_feeds[str(date_str)] = normalized_day
        if normalized_daily_feeds != daily_feeds:
            data["daily_feeds"] = normalized_daily_feeds
            migrated = True

        protected = data.get("protected", {})
        if "date" in protected and isinstance(protected.get("users"), list):
            protect_date = str(protected.get("date") or "")
            users = [str(user_id) for user_id in protected.get("users", []) if user_id]
            data["protected"] = {protect_date: {"__all__": users}} if protect_date else {}
            migrated = True
        else:
            normalized_protected: dict[str, dict[str, list[str]]] = {}
            for protect_date, group_map in protected.items():
                if not _is_valid_date(str(protect_date)):
                    continue
                if not isinstance(group_map, dict):
                    continue
                normalized_group_map: dict[str, list[str]] = {}
                for group_id, user_ids in group_map.items():
                    if not isinstance(user_ids, list):
                        continue
                    normalized_group_map[str(group_id)] = [str(user_id) for user_id in user_ids if user_id]
                normalized_protected[str(protect_date)] = normalized_group_map
            if normalized_protected != protected:
                data["protected"] = normalized_protected
                migrated = True

        # ================================ 群日活回填 ================================ #
        # 升级当天不能要求老用户重新触发一次命令；从已有群抽猪、烧烤事件和预约记录补齐日活集合。
        active_map = data.setdefault("group_daily_active_users", {})
        normalized_active: dict[str, dict[str, set[str]]] = {}
        for date_str, group_map in active_map.items():
            if not isinstance(group_map, dict):
                continue
            for group_id, user_ids in group_map.items():
                if isinstance(user_ids, list):
                    normalized_active.setdefault(str(date_str), {}).setdefault(str(group_id), set()).update(
                        str(user_id) for user_id in user_ids if user_id
                    )
        for date_str, group_map in data.get("group_rolls", {}).items():
            if not isinstance(group_map, dict):
                continue
            for group_id, rolls in group_map.items():
                if isinstance(rolls, dict):
                    normalized_active.setdefault(str(date_str), {}).setdefault(str(group_id), set()).update(
                        str(user_id) for user_id in rolls if user_id
                    )
        for date_str, events in data.get("daily_events", {}).items():
            if not isinstance(events, list):
                continue
            for event in events:
                if not isinstance(event, dict) or not event.get("group_id"):
                    continue
                participant_ids = event.get("participant_ids")
                if not isinstance(participant_ids, list):
                    participant_ids = []
                users = [event.get("attacker"), *participant_ids]
                if event.get("type") != "bot_backfire":
                    users.append(event.get("target"))
                normalized_active.setdefault(str(date_str), {}).setdefault(str(event["group_id"]), set()).update(
                    str(user_id) for user_id in users if user_id
                )
        for reservation in data.get("roast_reservations", {}).values():
            if not isinstance(reservation, dict) or not reservation.get("date_str") or not reservation.get("group_id"):
                continue
            participant_ids = [
                item.get("user_id") for item in reservation.get("participants", []) if isinstance(item, dict)
            ]
            normalized_active.setdefault(str(reservation["date_str"]), {}).setdefault(
                str(reservation["group_id"]), set()
            ).update(str(user_id) for user_id in participant_ids if user_id)
        serialized_active = {
            date_str: {group_id: sorted(user_ids) for group_id, user_ids in group_map.items()}
            for date_str, group_map in normalized_active.items()
        }
        if serialized_active != active_map:
            data["group_daily_active_users"] = serialized_active
            migrated = True

        # ================================ 预约投递状态迁移 ================================ #
        # 旧版本可能在 QQ 消息成功后、completed 落盘前遗留 processing + outcome。
        # 这类记录是否已经发送无法可靠判断，必须按“可能已发送”处理，禁止超时重领。
        for reservation in data.get("roast_reservations", {}).values():
            if (
                isinstance(reservation, dict)
                and reservation.get("status") == "processing"
                and isinstance(reservation.get("outcome_snapshot"), dict)
            ):
                reservation["status"] = "sending"
                migrated = True

        # ================================ Local 日报投递恢复 ================================ #
        # claimed 说明尚未提交发送意图，重启后可以安全重领；sending 可能已经发出
        # QQ 消息，只能冻结为 uncertain，避免进程重启后重复发送同一份日报。
        daily_report_deliveries = data.get("daily_report_deliveries", {})
        normalized_deliveries: dict[str, dict[str, dict]] = {}
        valid_delivery_statuses = {
            "pending",
            "claimed",
            "sending",
            "sent",
            "skipped",
            "uncertain",
            "failed",
        }
        for date_str, group_map in daily_report_deliveries.items():
            if not _is_valid_date(str(date_str)) or not isinstance(group_map, dict):
                continue
            normalized_group_map: dict[str, dict] = {}
            for group_id, raw in group_map.items():
                if not group_id or not isinstance(raw, dict):
                    continue
                status = str(raw.get("status") or "pending")
                if status not in valid_delivery_statuses:
                    status = "pending"
                elif status == "claimed":
                    status = "pending"
                elif status == "sending":
                    status = "uncertain"
                next_attempt_at = str(raw.get("next_attempt_at") or "")
                if next_attempt_at:
                    try:
                        datetime.datetime.fromisoformat(next_attempt_at.replace("Z", "+00:00"))
                    except ValueError:
                        next_attempt_at = ""
                normalized_group_map[str(group_id)] = {
                    "status": status,
                    "attempt_count": max(0, _safe_int(raw.get("attempt_count"), 0)),
                    "delivery_bot_id": str(raw.get("delivery_bot_id") or ""),
                    "cutoff_at": str(raw.get("cutoff_at") or ""),
                    "claim_token": "" if status == "pending" else str(raw.get("claim_token") or ""),
                    "next_attempt_at": next_attempt_at if status == "pending" else "",
                    "message_id": str(raw.get("message_id") or ""),
                    "last_error": str(raw.get("last_error") or ""),
                    "updated_at": str(raw.get("updated_at") or ""),
                }
            if normalized_group_map:
                normalized_deliveries[str(date_str)] = normalized_group_map
        if normalized_deliveries != daily_report_deliveries:
            data["daily_report_deliveries"] = normalized_deliveries
            migrated = True
        if migrated:
            logger.info("pig_data.json 数据结构已自动迁移/补全，开始落盘...")
            if persist:
                self.data = data
                self._sync_save()  # 迁移后立即落盘，防止重启丢失
        return data

    # ================================ 烤箱补货序列化 ================================ #

    def _refill_from_raw(self, raw: dict) -> GroupRoastRefillRequest:
        """把补货申请恢复为只读领域对象，集合字段统一去重排序。"""

        benefited = tuple(sorted({str(user_id) for user_id in raw.get("benefited_user_ids", []) if user_id}))
        return GroupRoastRefillRequest(
            request_id=str(raw.get("request_id") or ""),
            date_str=str(raw.get("date_str") or ""),
            group_id=str(raw.get("group_id") or ""),
            initiator_id=str(raw.get("initiator_id") or ""),
            initiator_name=str(raw.get("initiator_name") or ""),
            delivery_bot_id=str(raw.get("delivery_bot_id") or ""),
            message_id=str(raw.get("message_id") or ""),
            active_count_snapshot=max(0, _safe_int(raw.get("active_count_snapshot"), 0)),
            required_ratio=max(0, _safe_int(raw.get("required_ratio"), 25)),
            required_votes=max(2, _safe_int(raw.get("required_votes"), 2)),
            success_count_before=max(0, _safe_int(raw.get("success_count_before"), 0)),
            status=str(raw.get("status") or "voting"),
            created_at=str(raw.get("created_at") or ""),
            expires_at=str(raw.get("expires_at") or ""),
            completed_at=str(raw.get("completed_at") or ""),
            benefited_user_ids=benefited,
            failure_reason=str(raw.get("failure_reason") or ""),
        )

    def _find_refill_locked(self, request_id: str) -> Optional[dict]:
        raw = self.data.setdefault("roast_refill_requests", {}).get(str(request_id))
        return raw if isinstance(raw, dict) else None

    def _expire_refill_locked(self, raw: dict, now: datetime.datetime) -> bool:
        """懒过期 voting 申请；失败和成功记录保持不可逆。"""

        if raw.get("status") != "voting":
            return False
        try:
            expires_at = datetime.datetime.fromisoformat(str(raw.get("expires_at") or ""))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            expires_at = now
        if expires_at > now:
            return False
        raw["status"] = "expired"
        raw["failure_reason"] = "expired"
        return True

    def _group_active_users_locked(self, date_str: str, group_id: str) -> set[str]:
        day_map = self.data.setdefault("group_daily_active_users", {}).setdefault(date_str, {})
        raw_users = day_map.setdefault(str(group_id), [])
        if not isinstance(raw_users, list):
            raw_users = []
            day_map[str(group_id)] = raw_users
        return {str(user_id) for user_id in raw_users if user_id}

    def _mark_group_active_users_locked(self, date_str: str, group_id: str, user_ids: list[str]) -> bool:
        if not group_id:
            return False
        current = self._group_active_users_locked(date_str, group_id)
        updated = current | {str(user_id) for user_id in user_ids if user_id}
        if updated == current:
            return False
        active_at = self.data.setdefault("group_daily_active_at", {}).setdefault(
            date_str,
            {},
        ).setdefault(str(group_id), {})
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        for user_id in updated - current:
            active_at.setdefault(user_id, now_iso)
        self.data["group_daily_active_users"][date_str][str(group_id)] = sorted(updated)
        return True

    # ================================ 预约烤猪序列化 ================================ #

    @staticmethod
    def _daily_feed_from_raw(raw: dict) -> DailyFeedResult:
        """恢复单条加餐结果；数值统一钳制，避免坏缓存污染展示。"""

        return DailyFeedResult(
            status=str(raw.get("status") or "already_fed"),
            user_id=str(raw.get("user_id") or ""),
            pig_id=str(raw.get("pig_id") or ""),
            previous_level=max(0, min(5, _safe_int(raw.get("previous_level"), 0))),
            new_level=max(0, min(5, _safe_int(raw.get("new_level"), 0))),
            source_type=str(raw.get("source_type") or "reservation"),
            source_id=str(raw.get("source_id") or ""),
            created_at=str(raw.get("created_at") or ""),
        )

    def _reservation_from_raw(self, raw: dict) -> RoastReservation:
        """把持久化字典恢复成只读领域对象；坏参与者记录会被忽略。"""

        participants = tuple(
            RoastReservationParticipant(
                user_id=str(item.get("user_id") or ""),
                display_name=str(item.get("display_name") or ""),
                pig_id=str(item.get("pig_id") or ""),
            )
            for item in raw.get("participants", [])
            if isinstance(item, dict) and item.get("user_id")
        )
        snapshot = raw.get("outcome_snapshot")
        feed_results = tuple(
            self._daily_feed_from_raw(item)
            for item in (raw.get("daily_feed_results") or [])
            if isinstance(item, dict) and item.get("user_id")
        )
        return RoastReservation(
            reservation_id=str(raw.get("reservation_id") or ""),
            date_str=str(raw.get("date_str") or ""),
            group_id=str(raw.get("group_id") or ""),
            target_id=str(raw.get("target_id") or ""),
            target_name=str(raw.get("target_name") or ""),
            owner_id=str(raw.get("owner_id") or ""),
            owner_name=str(raw.get("owner_name") or ""),
            owner_pig_id=str(raw.get("owner_pig_id") or ""),
            participants=participants,
            delivery_bot_id=str(raw.get("delivery_bot_id") or ""),
            force_mode=raw.get("force_mode"),
            status=str(raw.get("status") or "pending"),
            target_pig_id=str(raw.get("target_pig_id") or ""),
            outcome_snapshot=dict(snapshot) if isinstance(snapshot, dict) else None,
            daily_feed_results=feed_results,
            claim_token=str(raw.get("claim_token") or ""),
        )

    def _bind_reservation_event_locked(self, raw: dict, event: RoastEvent) -> RoastEvent:
        """用预约快照覆盖事件身份字段，避免客户端把日报写到错误群或角色名下。"""

        reservation = self._reservation_from_raw(raw)
        return RoastEvent(
            event_type=event.event_type,
            attacker_id=reservation.owner_id,
            target_id=reservation.target_id,
            attacker_name=reservation.owner_name,
            target_name=reservation.target_name,
            food=event.food,
            group_id=reservation.group_id,
            reservation_id=reservation.reservation_id,
            participant_ids=tuple(item.user_id for item in reservation.participants),
            participant_names=tuple(item.display_name for item in reservation.participants),
            participant_count=reservation.participant_count,
            backfire_victim_id=event.backfire_victim_id,
            backfire_victim_name=event.backfire_victim_name,
            special_reason=event.special_reason,
            event_id=event.event_id,
            created_at=event.created_at,
        )

    def _find_reservation_locked(self, reservation_id: str) -> Optional[dict]:
        raw = self.data.setdefault("roast_reservations", {}).get(str(reservation_id))
        return raw if isinstance(raw, dict) else None

    def _find_pending_reservation_locked(self, date_str: str, group_id: str, target_id: str) -> Optional[dict]:
        for raw in self.data.setdefault("roast_reservations", {}).values():
            if not isinstance(raw, dict):
                continue
            if (
                raw.get("date_str") == date_str
                and raw.get("group_id") == group_id
                and raw.get("target_id") == target_id
                and raw.get("status") == "pending"
            ):
                return raw
        return None

    def _consume_roast_usage_locked(
        self,
        user_id: str,
        *,
        now: float,
        cooldown_seconds: Optional[int],
        max_charges: Optional[int],
    ) -> CooldownConsumeResult:
        """在预约事务已有锁的情况下消费普通充能，避免二次加锁。"""

        cooldown, charge_max = _normalize_charge_settings(cooldown_seconds, max_charges)
        usage = self.data.setdefault("usage", {})
        raw_state = usage.get(user_id, 0)
        if isinstance(raw_state, dict):
            charges = _safe_int(raw_state.get("roast_charges"), 0)
            updated_ts = float(raw_state.get("roast_charge_updated_ts") or now)
        else:
            charges, updated_ts = _legacy_roast_state(float(raw_state or 0), now, cooldown, charge_max)
        charges, updated_ts = _recover_roast_charges(charges, updated_ts, now, cooldown, charge_max)
        if charges <= 0:
            remaining = _next_charge_seconds(charges, updated_ts, now, cooldown, charge_max)
            usage[user_id] = {
                "last_roast_ts": float(raw_state or 0) if not isinstance(raw_state, dict) else raw_state.get("last_roast_ts"),
                "roast_charges": charges,
                "roast_charge_updated_ts": updated_ts,
            }
            return CooldownConsumeResult(False, remaining, 0, charge_max, remaining)

        was_full = charges >= charge_max
        charges -= 1
        if was_full:
            updated_ts = now
        usage[user_id] = {
            "last_roast_ts": now,
            "roast_charges": charges,
            "roast_charge_updated_ts": updated_ts,
        }
        return CooldownConsumeResult(
            True,
            0,
            charges,
            charge_max,
            _next_charge_seconds(charges, updated_ts, now, cooldown, charge_max),
        )

    def _consume_force_usage_locked(self, user_id: str, date_str: str) -> bool:
        usage = self.data.setdefault("force_usage", {})
        if usage.get(user_id) == date_str:
            return False
        usage[user_id] = date_str
        return True

    # ---- 原子写 ----

    def _sync_save(self):
        """同步原子写（仅用于启动期迁移，运行期写操作应使用 _atomic_save）。"""
        self._ensure_writable()
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._rotate_backups()
        tmp.replace(self.file)

    async def _atomic_save(self):
        """异步原子写：把 JSON 序列化和磁盘 IO 放到线程，避免阻塞 NoneBot 事件循环。"""
        # 调用方仍在 self._lock 临界区内等待写入完成；这里只是把同步文件 IO 挪出事件循环。
        await asyncio.to_thread(self._sync_save)

    def _ensure_writable(self):
        if self._load_failed:
            raise LocalStoreUnavailableError(
                "pig_data.json 读取失败，已拒绝写入以避免覆盖旧数据。请先修复数据文件或恢复备份。"
            )

    def _backup_paths(self) -> list[Path]:
        return [self.file.with_name(f"{self.file.name}.bak{'' if index == 0 else f'.{index}'}") for index in range(DATA_BACKUP_COUNT + 1)]

    def _restore_backup_rotation_clock(self) -> None:
        """按现有主备份时间恢复降频窗口，避免重启反复把轮换时间清零。"""

        primary_backup = self._backup_paths()[0]
        if not primary_backup.exists():
            return
        try:
            backup_age = max(0.0, time.time() - primary_backup.stat().st_mtime)
        except OSError:
            return
        self._last_backup_rotation_monotonic -= min(
            backup_age,
            DATA_BACKUP_MIN_INTERVAL_SECONDS,
        )

    def _rotate_backups(self) -> None:
        """按时间或写入次数轮换备份；主文件本身仍在每次业务变更时原子落盘。"""

        if self._skip_backup_rotation_once:
            # 从备份恢复时主文件仍是坏数据，绝不能把它推进现有好备份链。
            self._skip_backup_rotation_once = False
            self._writes_since_backup_rotation = 0
            self._last_backup_rotation_monotonic = time.monotonic()
            return
        if not self.file.exists():
            return

        backup_paths = self._backup_paths()
        self._writes_since_backup_rotation += 1
        elapsed = time.monotonic() - self._last_backup_rotation_monotonic
        if (
            backup_paths[0].exists()
            and self._writes_since_backup_rotation < DATA_BACKUP_MAX_WRITES
            and elapsed < DATA_BACKUP_MIN_INTERVAL_SECONDS
        ):
            return

        for index in range(len(backup_paths) - 1, -1, -1):
            source = self.file if index == 0 else backup_paths[index - 1]
            target = backup_paths[index]
            if not source.exists():
                continue
            if target.exists():
                target.unlink()
            shutil.copy2(source, target)
        self._writes_since_backup_rotation = 0
        self._last_backup_rotation_monotonic = time.monotonic()

    def _preserve_broken_file(self) -> None:
        """把无法读取的主文件另存为 broken 备份，方便人工排查和恢复。"""
        if not self.file.exists():
            return
        broken_path = self.file.with_name(f"{self.file.name}.broken.{int(time.time())}.bak")
        try:
            shutil.copy2(self.file, broken_path)
            logger.error(f"pig_data.json 损坏文件已保留: {broken_path}")
        except Exception as error:
            logger.error(f"pig_data.json 损坏文件备份失败: {error}")

    def _load_backup(self) -> Optional[dict]:
        for backup_path in self._backup_paths():
            if not backup_path.exists():
                continue
            try:
                raw = json.loads(backup_path.read_text("utf-8"))
                logger.warning(f"尝试从 pig_data.json 备份恢复: {backup_path}")
                return self._migrate(raw, persist=False)
            except Exception as error:
                logger.warning(f"pig_data.json 备份不可用: {backup_path}: {error}")
        return None

    # ---- 今日/历史 抽猪记录 ----

    def get_today_pig(self, user_id: str, date_str: Optional[str] = None) -> Optional[str]:
        """返回今日已抽的 pig_id，未抽返回 None。"""
        target_date = date_str or rollpig_date_str()
        return self.data["history"].get(target_date, {}).get(user_id)

    def get_daily_rolls(self, date_str: Optional[str] = None) -> dict:
        target_date = date_str or rollpig_date_str()
        return dict(self.data.get("history", {}).get(target_date, {}))

    # ================================ 每日抽取历史快照 ================================ #

    def _snapshot_from_raw(
        self,
        *,
        date_str: str,
        user_id: str,
        pig_id: str,
        raw: object,
    ) -> DailyRollSnapshot:
        """恢复单条快照；坏条目只降级为历史身份，不拖垮整个数据文件。"""

        # 抽取快照损坏不影响独立结算的加餐事实，也不恢复不可信的抽取字段。
        daily_feed = parse_applied_daily_feed(
            self.data.get("daily_feeds", {}).get(date_str, {}).get(user_id),
            pig_id=pig_id,
            user_id=user_id,
        )
        identity_snapshot = DailyRollSnapshot(
            date_str=date_str, pig_id=pig_id, daily_feed_result=daily_feed,
            is_makeup=isinstance(raw, dict) and raw.get("is_makeup") is True,
        )
        if not isinstance(raw, dict) or str(raw.get("pig_id") or "") != pig_id:
            if raw is not None:
                logger.warning(
                    "rollpig 每日抽取快照损坏，已忽略动态字段: "
                    f"date={date_str} user={user_id} pig_id={pig_id}"
                )
            return identity_snapshot

        is_new_raw = raw.get("is_new_pig")
        is_new_pig = is_new_raw if isinstance(is_new_raw, bool) else None
        previous_copies = _optional_nonnegative_int(raw.get("previous_copies"))
        copies_after_roll = _optional_nonnegative_int(raw.get("copies_after_roll"))
        previous_expert_level = _optional_nonnegative_int(raw.get("previous_expert_level"))
        expert_level_after_roll = _optional_nonnegative_int(raw.get("expert_level_after_roll"))
        if previous_expert_level is not None and previous_expert_level > 5:
            previous_expert_level = None
        if expert_level_after_roll is not None and expert_level_after_roll > 5:
            expert_level_after_roll = None
        collection_size = _optional_nonnegative_int(raw.get("collection_size_after_roll"))
        resolved_level = _optional_nonnegative_int(raw.get("resolved_variant_level"))
        if resolved_level is not None and resolved_level > 5:
            resolved_level = None

        raw_levels = raw.get("unlocked_variant_levels")
        unlocked_levels = tuple(sorted({
            int(level)
            for level in raw_levels
            if not isinstance(level, bool) and isinstance(level, int) and 1 <= level <= 5
        })) if isinstance(raw_levels, list) else ()
        raw_fields = raw.get("unlocked_variant_fields")
        unlocked_fields = frozenset(
            str(field)
            for field in raw_fields
            if str(field) in {"image", "description", "analysis"}
        ) if isinstance(raw_fields, list) else frozenset()

        snapshot = DailyRollSnapshot(
            date_str=date_str,
            pig_id=pig_id,
            is_new_pig=is_new_pig,
            previous_copies=previous_copies,
            copies_after_roll=copies_after_roll,
            previous_expert_level=previous_expert_level,
            expert_level_after_roll=expert_level_after_roll,
            daily_feed_result=daily_feed,
            collection_size_after_roll=collection_size,
            resource_version=str(raw.get("resource_version") or ""),
            resolved_variant_level=resolved_level,
            resolved_image_name=str(raw.get("resolved_image_name") or ""),
            unlocked_variant_levels=unlocked_levels,
            unlocked_variant_fields=unlocked_fields,
            is_makeup=raw.get("is_makeup") is True,
        )
        if not snapshot.outcome_available:
            logger.warning(
                "rollpig 每日抽取快照字段残缺，已忽略抽取动态字段: "
                f"date={date_str} user={user_id} pig_id={pig_id}"
            )
            return identity_snapshot
        return snapshot

    @staticmethod
    def _snapshot_to_raw(snapshot: DailyRollSnapshot) -> dict:
        """把领域快照序列化为稳定 JSON；集合字段固定排序便于幂等比较。"""

        return {
            "pig_id": snapshot.pig_id,
            "is_makeup": snapshot.is_makeup,
            "is_new_pig": snapshot.is_new_pig,
            "previous_copies": snapshot.previous_copies,
            "copies_after_roll": snapshot.copies_after_roll,
            "previous_expert_level": snapshot.previous_expert_level,
            "expert_level_after_roll": snapshot.expert_level_after_roll,
            "collection_size_after_roll": snapshot.collection_size_after_roll,
            "resource_version": snapshot.resource_version,
            "resolved_variant_level": snapshot.resolved_variant_level,
            "resolved_image_name": snapshot.resolved_image_name,
            "unlocked_variant_levels": list(snapshot.unlocked_variant_levels),
            "unlocked_variant_fields": sorted(snapshot.unlocked_variant_fields),
        }

    def get_daily_roll_snapshot(self, user_id: str, date_str: str) -> Optional[DailyRollSnapshot]:
        """读取指定日期历史快照；旧记录返回仅含日期与 pig_id 的降级对象。"""

        normalized_user_id = str(user_id)
        pig_id = self.data.get("history", {}).get(date_str, {}).get(normalized_user_id)
        if not pig_id:
            return None
        day_snapshots = self.data.get("daily_roll_snapshots", {}).get(date_str, {})
        raw = day_snapshots.get(normalized_user_id) if isinstance(day_snapshots, dict) else None
        return self._snapshot_from_raw(
            date_str=date_str,
            user_id=normalized_user_id,
            pig_id=str(pig_id),
            raw=raw,
        )

    def _attach_created_snapshot_locked(
        self,
        *,
        user_id: str,
        date_str: str,
        result: DailyRollResult,
    ) -> DailyRollResult:
        """在抽取临界区内写入无法事后可靠倒推的成长结果。"""

        collection_size = len(self.data.setdefault("collection", {}).get(user_id, []))
        snapshot = DailyRollSnapshot(
            date_str=date_str,
            pig_id=result.pig_id,
            is_new_pig=result.is_new_pig,
            previous_copies=result.previous_copies,
            copies_after_roll=result.copies,
            previous_expert_level=result.previous_expert_level,
            expert_level_after_roll=result.expert_level,
            collection_size_after_roll=collection_size,
        )
        raw_snapshot = self._snapshot_to_raw(snapshot)
        # Local 没有 DailyRoll 数据库行，必须在首次创建时单独保存时间，日报才能
        # 区分截止点前后的抽取；资源补全只能继承这个时间，不能重新生成。
        raw_snapshot["created_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.data.setdefault("daily_roll_snapshots", {}).setdefault(date_str, {})[user_id] = raw_snapshot
        return replace(result, snapshot=snapshot)

    async def complete_daily_roll_snapshot(
        self,
        user_id: str,
        snapshot: DailyRollSnapshot,
    ) -> bool:
        """幂等补全资源快照；首次写入后禁止另一实例静默改写历史上下文。"""

        normalized_user_id = str(user_id)
        if not snapshot.outcome_available or not snapshot.resource_version:
            raise ValueError("每日抽取快照缺少基础结果或资源版本")

        async with self._lock:
            current_pig_id = self.data.get("history", {}).get(snapshot.date_str, {}).get(normalized_user_id)
            if str(current_pig_id or "") != snapshot.pig_id:
                raise ValueError("每日抽取快照与 history 不一致")

            snapshots = self.data.setdefault("daily_roll_snapshots", {})
            day_snapshots = snapshots.get(snapshot.date_str)
            if not isinstance(day_snapshots, dict):
                # 运行期间也按日期桶隔离损坏数据，避免用户必须重启后才能恢复写入。
                logger.warning(
                    "rollpig 每日抽取快照日期桶损坏，补全请求已拒绝: "
                    f"date={snapshot.date_str}"
                )
                raise ValueError("每日抽取快照日期桶损坏")
            existing = self._snapshot_from_raw(
                date_str=snapshot.date_str,
                user_id=normalized_user_id,
                pig_id=snapshot.pig_id,
                raw=day_snapshots.get(normalized_user_id),
            )
            if not existing.outcome_available:
                raise ValueError("旧抽取记录不支持补全历史快照")
            if (
                existing.is_new_pig,
                existing.is_makeup,
                existing.previous_copies,
                existing.copies_after_roll,
                existing.previous_expert_level,
                existing.expert_level_after_roll,
                existing.collection_size_after_roll,
            ) != (
                snapshot.is_new_pig,
                snapshot.is_makeup,
                snapshot.previous_copies,
                snapshot.copies_after_roll,
                snapshot.previous_expert_level,
                snapshot.expert_level_after_roll,
                snapshot.collection_size_after_roll,
            ):
                raise ValueError("每日抽取成长快照冲突")

            if existing.resource_version:
                if self._snapshot_to_raw(existing) != self._snapshot_to_raw(snapshot):
                    raise ValueError("每日抽取资源快照已由首次客户端写入")
                return True

            serialized = self._snapshot_to_raw(snapshot)
            raw_existing = day_snapshots.get(normalized_user_id)
            if isinstance(raw_existing, dict) and raw_existing.get("created_at"):
                serialized["created_at"] = raw_existing["created_at"]
            day_snapshots[normalized_user_id] = serialized
            await self._atomic_save()
            return True

    def _record_group_roll(self, date_str: str, group_id: str, user_id: str, pig_id: str) -> bool:
        """在群维度登记今日已出现的猪形态，用于群内日报与随机烤群友。"""
        if not group_id:
            return False
        group_rolls = self.data.setdefault("group_rolls", {})
        day_rolls = group_rolls.setdefault(date_str, {})
        group_roll_map = day_rolls.setdefault(group_id, {})
        changed = group_roll_map.get(user_id) != pig_id
        group_roll_map[user_id] = pig_id
        if changed:
            seen_at = self.data.setdefault("group_roll_seen_at", {}).setdefault(
                date_str,
                {},
            ).setdefault(group_id, {})
            seen_at.setdefault(
                user_id,
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
            )
        return self._mark_group_active_users_locked(date_str, group_id, [user_id]) or changed

    # ================================ P1A抽猪成长状态 ================================ #
    # 本地模式没有数据库事务，因此所有写入都必须在调用方持有 self._lock 时完成。
    # 与 cloud 的 P1A 语义保持一致：只有当天 DailyRoll 首次创建成功时，才允许 copies / duplicate_streak 变化，重复发送命令只读取既有结果。

    def get_draw_state(self, user_id: str) -> DrawState:
        """返回用户图鉴成长状态；旧 collection 数据会按 copies=1 兜底聚合。"""
        user_id = str(user_id)
        collection = self.data.setdefault("collection", {})
        raw_collection = collection.get(user_id, [])
        collection_ids = [str(pig_id) for pig_id in raw_collection] if isinstance(raw_collection, list) else []

        raw_progress = self.data.setdefault("pig_progress", {}).get(user_id, {})
        progress: dict[str, PigProgress] = {}
        if isinstance(raw_progress, dict):
            for pig_id, item in raw_progress.items():
                if not isinstance(item, dict):
                    continue
                progress[str(pig_id)] = PigProgress(
                    copies=max(0, _safe_int(item.get("copies"), 0)),
                    growth_bonus=max(0, _safe_int(item.get("growth_bonus"), 0)),
                    first_obtained_at=item.get("first_obtained_at"),
                )

        for pig_id in collection_ids:
            progress.setdefault(
                pig_id,
                PigProgress(copies=1, growth_bonus=0, first_obtained_at=None),
            )

        raw_state = self.data.setdefault("draw_state", {}).get(user_id, {})
        duplicate_streak = _safe_int(raw_state.get("duplicate_streak"), 0) if isinstance(raw_state, dict) else 0
        return DrawState(
            pig_ids=sorted(progress),
            progress=dict(sorted(progress.items())),
            duplicate_streak=max(0, duplicate_streak),
        )

    def _apply_created_roll_progress_locked(self, user_id: str, pig_id: str) -> DailyRollResult:
        collection = self.data.setdefault("collection", {})
        user_collection = collection.setdefault(user_id, [])
        if not isinstance(user_collection, list):
            user_collection = []
            collection[user_id] = user_collection

        pig_progress = self.data.setdefault("pig_progress", {})
        user_progress = pig_progress.setdefault(user_id, {})
        if not isinstance(user_progress, dict):
            user_progress = {}
            pig_progress[user_id] = user_progress

        draw_state = self.data.setdefault("draw_state", {})
        state = draw_state.setdefault(user_id, {"duplicate_streak": 0})
        if not isinstance(state, dict):
            state = {"duplicate_streak": 0}
            draw_state[user_id] = state

        previous_duplicate_streak = max(0, _safe_int(state.get("duplicate_streak"), 0))
        previous_item = user_progress.get(pig_id)
        has_progress = isinstance(previous_item, dict)
        already_collected = pig_id in user_collection
        previous_copies = (
            max(1, _safe_int(previous_item.get("copies"), 1))
            if has_progress
            else (1 if already_collected else 0)
        )
        is_new_pig = previous_copies <= 0 and not already_collected
        growth_bonus = (
            max(0, _safe_int(previous_item.get("growth_bonus"), 0))
            if has_progress
            else 0
        )

        if pig_id not in user_collection:
            user_collection.append(pig_id)

        if is_new_pig:
            copies = 1
            duplicate_streak = 0
            first_obtained_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        else:
            copies = previous_copies + 1
            duplicate_streak = previous_duplicate_streak + 1
            first_obtained_at = (
                previous_item.get("first_obtained_at")
                if has_progress and previous_item.get("first_obtained_at")
                else None
            )

        user_progress[pig_id] = {
            "copies": copies,
            "growth_bonus": growth_bonus,
            "first_obtained_at": first_obtained_at,
        }
        state["duplicate_streak"] = duplicate_streak
        return DailyRollResult(
            pig_id=pig_id,
            created=True,
            is_new_pig=is_new_pig,
            previous_copies=previous_copies,
            copies=copies,
            previous_expert_level=expert_level_from_copies(previous_copies, growth_bonus),
            expert_level=expert_level_from_copies(copies, growth_bonus),
            previous_duplicate_streak=previous_duplicate_streak,
            duplicate_streak=duplicate_streak,
        )

    def _build_existing_roll_result_locked(
        self,
        user_id: str,
        pig_id: str,
        date_str: str,
    ) -> DailyRollResult:
        snapshot = self.get_daily_roll_snapshot(user_id, date_str)
        if snapshot and snapshot.outcome_available:
            draw_state = self.get_draw_state(user_id)
            return DailyRollResult(
                pig_id=pig_id,
                created=False,
                is_new_pig=bool(snapshot.is_new_pig),
                previous_copies=int(snapshot.previous_copies or 0),
                copies=int(snapshot.copies_after_roll or 0),
                previous_expert_level=(
                    int(snapshot.previous_expert_level)
                    if snapshot.previous_expert_level is not None
                    else expert_level_from_copies(snapshot.previous_copies or 0)
                ),
                expert_level=(
                    int(snapshot.expert_level_after_roll)
                    if snapshot.expert_level_after_roll is not None
                    else expert_level_from_copies(snapshot.copies_after_roll or 0)
                ),
                previous_duplicate_streak=draw_state.duplicate_streak,
                duplicate_streak=draw_state.duplicate_streak,
                snapshot=snapshot,
            )

        draw_state = self.get_draw_state(user_id)
        copies = draw_state.copies_of(pig_id)
        expert_level = draw_state.expert_level_of(pig_id)
        return DailyRollResult(
            pig_id=pig_id,
            created=False,
            previous_copies=copies,
            copies=copies,
            previous_expert_level=expert_level,
            expert_level=expert_level,
            previous_duplicate_streak=draw_state.duplicate_streak,
            duplicate_streak=draw_state.duplicate_streak,
        )

    async def set_today_pig(self, user_id: str, pig_id: str, group_id: str = ""):
        """记录今日抽到的 pig_id，并同步将其写入图鉴（永久保留）。"""
        async with self._lock:
            today = rollpig_date_str()
            if today not in self.data["history"]:
                self.data["history"][today] = {}
            previous_pig_id = self.data["history"][today].get(user_id)
            self.data["history"][today][user_id] = pig_id
            self._record_group_roll(today, group_id, user_id, pig_id)
            if previous_pig_id != pig_id:
                result = self._apply_created_roll_progress_locked(user_id, pig_id)
                self._attach_created_snapshot_locked(
                    user_id=user_id,
                    date_str=today,
                    result=result,
                )

            await self._atomic_save()

    async def get_or_create_today_pig(
        self,
        user_id: str,
        proposed_pig_id: str,
        date_str: Optional[str] = None,
        group_id: str = "",
        *,
        makeup: bool = False,
    ) -> DailyRollResult:
        target_date = date_str or rollpig_date_str()
        async with self._lock:
            if makeup:
                if target_date != rollpig_date_str(-1):
                    raise ValueError("只能补签昨天的小猪")
                group_id = ""
            history = self.data.setdefault("history", {})
            day_history = history.setdefault(target_date, {})
            existing_pig_id = day_history.get(user_id)
            dirty = False

            if existing_pig_id:
                if group_id:
                    dirty = self._record_group_roll(target_date, group_id, user_id, existing_pig_id)
                if dirty:
                    await self._atomic_save()
                return self._build_existing_roll_result_locked(user_id, existing_pig_id, target_date)

            day_history[user_id] = proposed_pig_id
            self._record_group_roll(target_date, group_id, user_id, proposed_pig_id)
            result = self._apply_created_roll_progress_locked(user_id, proposed_pig_id)
            result = self._attach_created_snapshot_locked(
                user_id=user_id,
                date_str=target_date,
                result=result,
            )

            if makeup:
                snapshot = replace(result.snapshot, is_makeup=True)
                self.data["daily_roll_snapshots"][target_date][user_id]["is_makeup"] = True
                result = replace(result, snapshot=snapshot)
                await self._atomic_save()
                return result

            # 预约只在 DailyRoll 首次创建的同一临界区内转为 ready；重复查看不会二次激活。
            for reservation in self.data.setdefault("roast_reservations", {}).values():
                if not isinstance(reservation, dict):
                    continue
                if (
                    reservation.get("date_str") == target_date
                    and reservation.get("target_id") == user_id
                    and reservation.get("status") == "pending"
                ):
                    reservation["status"] = "ready"
                    reservation["target_pig_id"] = proposed_pig_id
                    reservation["ready_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

            await self._atomic_save()
            return result

    # ================================ 预约烤猪业务 ================================ #

    async def record_unrolled_roast_attempt(
        self, user_id: str, date_str: Optional[str] = None
    ) -> UnrolledRoastAttemptResult:
        """持久化未抽猪先烤次数；日期是天然分区，第二天自动从 1 重新开始。"""

        target_date = date_str or rollpig_date_str()
        async with self._lock:
            day_attempts = self.data.setdefault("unrolled_roast_attempts", {}).setdefault(target_date, {})
            count = max(0, _safe_int(day_attempts.get(user_id), 0)) + 1
            day_attempts[user_id] = count
            await self._atomic_save()
        return UnrolledRoastAttemptResult(target_date, user_id, count)

    # ================================ 预约通知关联与回复加入 ================================ #

    async def bind_roast_reservation_message(
        self, *, reservation_id: str, bot_id: str, group_id: str, message_id: str,
        date_str: Optional[str] = None,
    ) -> bool:
        """通知 ID 随预约落盘并清理，重启后仍可回复加入。"""
        async with self._lock:
            raw = self._find_reservation_locked(reservation_id)
            if not raw or raw.get("group_id") != group_id or raw.get("date_str") != (date_str or rollpig_date_str()):
                return False
            reference = {"bot_id": bot_id, "message_id": message_id}
            messages = raw.setdefault("messages", [])
            if reference not in messages:
                messages.append(reference)
                await self._atomic_save()
            return True

    async def join_roast_reservation_by_message(
        self, *, bot_id: str, group_id: str, message_id: str,
        attacker_id: str, attacker_name: str, date_str: Optional[str] = None,
    ) -> RoastReservationPrepareResult:
        """在同一把锁内定位、检查并加入，不创建预约、不消耗充能。"""
        target_date = date_str or rollpig_date_str()
        async with self._lock:
            reference = {"bot_id": bot_id, "message_id": message_id}
            raw = next((item for item in self.data.setdefault("roast_reservations", {}).values()
                        if isinstance(item, dict) and item.get("group_id") == group_id
                        and reference in item.get("messages", [])), None)
            if raw is None:
                return RoastReservationPrepareResult("message_not_found")
            history = self.data.setdefault("history", {}).get(target_date, {})
            if raw.get("date_str") != target_date or raw.get("status") != "pending" or history.get(raw["target_id"]):
                return RoastReservationPrepareResult("reservation_closed")
            if raw["target_id"] == attacker_id:
                return RoastReservationPrepareResult("self_target")
            pig_id = history.get(attacker_id)
            if not pig_id:
                return RoastReservationPrepareResult("attacker_unrolled")
            result = self._join_reservation_locked(raw, attacker_id, attacker_name, str(pig_id))
            if result.status == "reservation_joined":
                await self._atomic_save()
            return result

    def _join_reservation_locked(self, raw: dict, user_id: str, name: str, pig_id: str) -> RoastReservationPrepareResult:
        """两种加入入口共用人数、重复判定；调用方持锁并负责保存。"""
        participants = raw.setdefault("participants", [])
        if any(str(item.get("user_id")) == user_id for item in participants if isinstance(item, dict)):
            return RoastReservationPrepareResult("already_joined", self._reservation_from_raw(raw))
        if len(participants) >= ROAST_RESERVATION_MAX_PARTICIPANTS:
            return RoastReservationPrepareResult("reservation_full", self._reservation_from_raw(raw))
        participants.append({"user_id": user_id, "display_name": name, "pig_id": pig_id})
        # 回复加入也代表今日小猪在本群出现，必须与参与者名单一起落盘。
        self._record_group_roll(raw["date_str"], raw["group_id"], user_id, pig_id)
        return RoastReservationPrepareResult("reservation_joined", self._reservation_from_raw(raw))

    async def prepare_roast_reservation(
        self,
        *,
        attacker_id: str,
        attacker_name: str,
        attacker_pig_id: str,
        target_id: str,
        target_name: str,
        group_id: str,
        delivery_bot_id: str,
        force_mode: Optional[str] = None,
        date_str: Optional[str] = None,
        cooldown_seconds: Optional[int] = None,
        max_charges: Optional[int] = None,
    ) -> RoastReservationPrepareResult:
        """原子完成目标检查、免费加入或扣资源创建预约。"""

        target_date = date_str or rollpig_date_str()
        attacker_id = str(attacker_id)
        target_id = str(target_id)
        async with self._lock:
            target_pig_id = self.data.setdefault("history", {}).get(target_date, {}).get(target_id)
            existing = self._find_pending_reservation_locked(target_date, str(group_id), target_id)
            if existing is not None:
                result = self._join_reservation_locked(existing, attacker_id, str(attacker_name), str(attacker_pig_id))
                if result.status == "reservation_joined":
                    await self._atomic_save()
                return result

            protected_users = (
                self.data.setdefault("protected", {})
                .get(target_date, {})
                .get(str(group_id), [])
            )
            is_protected = target_id in {str(user_id) for user_id in protected_users}
            protection_broken = is_protected and force_mode in {"normal", "super"}
            if is_protected and not protection_broken:
                return RoastReservationPrepareResult("protected")
            if target_pig_id:
                return RoastReservationPrepareResult(
                    "target_ready",
                    target_pig_id=str(target_pig_id),
                    protection_broken=protection_broken,
                )

            cooldown_result: Optional[CooldownConsumeResult] = None
            if force_mode == "normal":
                if not self._consume_force_usage_locked(attacker_id, target_date):
                    return RoastReservationPrepareResult("force_denied")
            elif force_mode != "super":
                cooldown_result = self._consume_roast_usage_locked(
                    attacker_id,
                    now=time.time(),
                    cooldown_seconds=cooldown_seconds,
                    max_charges=max_charges,
                )
                if not cooldown_result.allowed:
                    return RoastReservationPrepareResult("cooldown_denied", cooldown=cooldown_result)

            reservation_id = uuid.uuid4().hex
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            raw = {
                "reservation_id": reservation_id,
                "date_str": target_date,
                "group_id": str(group_id),
                "target_id": target_id,
                "target_name": str(target_name),
                "owner_id": attacker_id,
                "owner_name": str(attacker_name),
                "owner_pig_id": str(attacker_pig_id),
                "participants": [{
                    "user_id": attacker_id,
                    "display_name": str(attacker_name),
                    "pig_id": str(attacker_pig_id),
                }],
                "delivery_bot_id": str(delivery_bot_id),
                "force_mode": force_mode,
                "status": "pending",
                "target_pig_id": "",
                "created_at": now_iso,
                "outcome_snapshot": None,
                "daily_feed_results": None,
            }
            self.data.setdefault("roast_reservations", {})[reservation_id] = raw
            self._mark_group_active_users_locked(target_date, str(group_id), [attacker_id])
            await self._atomic_save()
            return RoastReservationPrepareResult(
                "reservation_created",
                self._reservation_from_raw(raw),
                cooldown_result,
                protection_broken=protection_broken,
            )

    async def claim_roast_reservations(
        self,
        delivery_bot_id: str,
        date_str: Optional[str] = None,
        excluded_reservation_ids: Optional[set[str]] = None,
    ) -> RoastReservationClaimResult:
        """领取当前 Bot 当天可投递的预约；只回收仍可安全重试的发送前租约。"""

        target_date = date_str or rollpig_date_str()
        excluded_ids = set(excluded_reservation_ids or ())
        claimed: list[RoastReservation] = []
        async with self._lock:
            now = datetime.datetime.now(datetime.timezone.utc)
            reservations = self.data.setdefault("roast_reservations", {})
            for reservation_id, raw in reservations.items():
                if not isinstance(raw, dict):
                    continue
                claimed_at = raw.get("claimed_at")
                stale_presend = False
                # processing 负责生成结果，prepared 表示结果已固化但还在渲染消息；两者
                # 都没有调用外部发送接口，可以依赖租约恢复。sending 绝不能自动重领。
                if raw.get("status") in {"processing", "prepared"}:
                    if not isinstance(claimed_at, str):
                        stale_presend = True
                    else:
                        try:
                            claimed_time = datetime.datetime.fromisoformat(claimed_at)
                            if claimed_time.tzinfo is None:
                                claimed_time = claimed_time.replace(tzinfo=datetime.timezone.utc)
                            stale_presend = (
                                now - claimed_time
                            ).total_seconds() >= ROAST_RESERVATION_CLAIM_TIMEOUT_SECONDS
                        except ValueError:
                            stale_presend = True
                if (
                    _matches_reservation_claim_date(raw, target_date, now)
                    and raw.get("delivery_bot_id") == str(delivery_bot_id)
                    and reservation_id not in excluded_ids
                    and (raw.get("status") == "ready" or stale_presend)
                ):
                    # 固定快照仍需先渲染成最终消息，因此重领只能进入 prepared；真正
                    # 调用 OneBot 发送前由独立事务推进到 sending。
                    raw["status"] = (
                        "prepared" if isinstance(raw.get("outcome_snapshot"), dict) else "processing"
                    )
                    raw["claim_token"] = uuid.uuid4().hex
                    raw["claimed_at"] = now.isoformat()
                    claimed.append(self._reservation_from_raw(raw))
            if claimed:
                await self._atomic_save()
            has_owned = any(
                isinstance(raw, dict)
                and _matches_reservation_claim_date(raw, target_date, now)
                and raw.get("delivery_bot_id") == str(delivery_bot_id)
                and raw.get("status") in {"pending", "ready", "processing", "prepared"}
                for raw in reservations.values()
            )
        return RoastReservationClaimResult(tuple(claimed), has_owned=has_owned)

    def has_owned_roast_reservations(self, delivery_bot_id: str, date_str: Optional[str] = None) -> bool:
        target_date = date_str or rollpig_date_str()
        now = datetime.datetime.now(datetime.timezone.utc)
        return any(
            isinstance(raw, dict)
            and _matches_reservation_claim_date(raw, target_date, now)
            and raw.get("delivery_bot_id") == str(delivery_bot_id)
            and raw.get("status") in {"pending", "ready", "processing", "prepared"}
            for raw in self.data.setdefault("roast_reservations", {}).values()
        )

    async def save_roast_reservation_outcome(
        self,
        reservation_id: str,
        claim_token: str,
        outcome_snapshot: dict,
        *,
        settle_daily_feed: bool = False,
    ) -> Optional[RoastReservation]:
        """原子固化结果并进入可恢复的 prepared；此时尚未调用外部发送接口。"""

        async with self._lock:
            raw = self._find_reservation_locked(reservation_id)
            if (
                raw is None
                or raw.get("status") not in {"processing", "prepared", "sending", "completed"}
                or raw.get("claim_token") != claim_token
            ):
                return None
            changed = False
            if not isinstance(raw.get("outcome_snapshot"), dict):
                if raw.get("status") != "processing":
                    return None
                raw["outcome_snapshot"] = dict(outcome_snapshot)
                # 普通预约成功时，参与者在同一锁和同一次保存中分别尝试加餐。
                # 结果随预约固化，之后的释放、重领和重新渲染只会复用这份列表。
                if (
                    settle_daily_feed
                    and raw.get("force_mode") is None
                    and str(outcome_snapshot.get("event_type") or "") == "success"
                ):
                    raw["daily_feed_results"] = [
                        self._daily_feed_to_raw(
                            self._apply_daily_feed_locked(
                                date_str=str(raw.get("date_str") or rollpig_date_str()),
                                user_id=str(item.get("user_id") or ""),
                                source_type="reservation",
                                source_id=reservation_id,
                            )
                        )
                        for item in raw.get("participants", [])
                        if isinstance(item, dict) and item.get("user_id")
                    ]
                else:
                    raw["daily_feed_results"] = []
                changed = True
            elif raw.get("outcome_snapshot") != outcome_snapshot:
                return None
            if raw.get("status") == "processing":
                raw["status"] = "prepared"
                changed = True
            if changed:
                await self._atomic_save()
            return self._reservation_from_raw(raw)

    async def mark_roast_reservation_sending(
        self,
        reservation_id: str,
        claim_token: str,
    ) -> Optional[RoastReservation]:
        """最终消息准备完成后进入不可自动重领的 sending。"""

        async with self._lock:
            raw = self._find_reservation_locked(reservation_id)
            if (
                raw is None
                or raw.get("claim_token") != claim_token
                or raw.get("status") not in {"prepared", "sending", "completed"}
                or not isinstance(raw.get("outcome_snapshot"), dict)
            ):
                return None
            if raw.get("status") == "prepared":
                raw["status"] = "sending"
                await self._atomic_save()
            return self._reservation_from_raw(raw)

    async def complete_roast_reservation(
        self,
        reservation_id: str,
        claim_token: str,
        event: Optional[RoastEvent] = None,
    ) -> bool:
        """原子完成预约，并把对应日报事件与状态写入同一次 JSON 保存。"""

        async with self._lock:
            raw = self._find_reservation_locked(reservation_id)
            if (
                raw is None
                or raw.get("claim_token") != claim_token
                or raw.get("status") not in {"sending", "completed"}
            ):
                return False
            changed = False
            if raw.get("status") != "completed":
                raw["status"] = "completed"
                raw["completed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                changed = True
            if event is not None:
                bound_event = self._bind_reservation_event_locked(raw, event)
                changed = self._append_roast_event_locked(
                    bound_event,
                    date_str=str(raw.get("date_str") or rollpig_date_str()),
                ) or changed
            if changed:
                await self._atomic_save()
            return True

    async def release_roast_reservation(self, reservation_id: str, claim_token: str) -> bool:
        """发送开始前失败时放回 ready；已确定 outcome 会原样保留。"""

        async with self._lock:
            raw = self._find_reservation_locked(reservation_id)
            if (
                raw is None
                or raw.get("status") not in {"processing", "prepared"}
                or raw.get("claim_token") != claim_token
            ):
                return False
            raw["status"] = "ready"
            # 保留释放时间，使零点前领取、零点后失败的发送前任务仍能在短暂窗口内重领。
            raw["released_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            raw.pop("claimed_at", None)
            raw.pop("claim_token", None)
            await self._atomic_save()
            return True

    # ================================ 烤箱补货事务 ================================ #

    async def mark_group_active_users(
        self,
        group_id: str,
        user_ids: list[str],
        date_str: Optional[str] = None,
    ) -> None:
        """幂等登记群日活玩家；空群或空用户列表不会触发写盘。"""

        target_date = date_str or rollpig_date_str()
        async with self._lock:
            if self._mark_group_active_users_locked(target_date, str(group_id), user_ids):
                await self._atomic_save()

    def get_group_active_user_ids(
        self,
        group_id: str,
        date_str: Optional[str] = None,
        cutoff_at: Optional[str] = None,
    ) -> set[str]:
        target_date = date_str or rollpig_date_str()
        users = set(self._group_active_users_locked(target_date, str(group_id)))
        cutoff = _parse_utc_datetime(cutoff_at)
        if cutoff is None:
            return users
        active_at = self.data.get("group_daily_active_at", {}).get(
            target_date,
            {},
        ).get(str(group_id), {})
        return {
            user_id
            for user_id in users
            if _timestamp_not_after_cutoff(active_at.get(user_id), cutoff)
        }

    async def prepare_group_roast_refill(
        self,
        *,
        group_id: str,
        initiator_id: str,
        initiator_name: str,
        delivery_bot_id: str,
        eligible_user_ids: Optional[list[str]] = None,
        date_str: Optional[str] = None,
        now_ts: Optional[float] = None,
    ) -> GroupRoastRefillPrepareResult:
        """冻结门槛并原子创建申请；同群跨日也只允许一场未过期投票。"""

        target_date = date_str or rollpig_date_str()
        now = datetime.datetime.fromtimestamp(float(now_ts or time.time()), tz=datetime.timezone.utc)
        async with self._lock:
            changed = False
            for raw in self.data.setdefault("roast_refill_requests", {}).values():
                if not isinstance(raw, dict) or raw.get("group_id") != str(group_id):
                    continue
                changed = self._expire_refill_locked(raw, now) or changed
                if raw.get("status") == "voting":
                    if changed:
                        await self._atomic_save()
                    return GroupRoastRefillPrepareResult(
                        "existing",
                        self._refill_from_raw(raw),
                        vote_weight_policy=ROAST_REFILL_VOTE_WEIGHT_POLICY,
                    )

            active_user_set = self._group_active_users_locked(target_date, str(group_id))
            if eligible_user_ids is not None:
                # 群成员名单来自 OneBot 的实时查询；本地日活只能在这个边界内参与
                # 门槛计算，避免已退群或被错误登记的账号抬高票数。
                eligible = {str(user_id) for user_id in eligible_user_ids if user_id}
                active_user_set &= eligible
            active_user_ids = tuple(sorted(active_user_set))
            if len(active_user_ids) < 3:
                if changed:
                    await self._atomic_save()
                return GroupRoastRefillPrepareResult(
                    "insufficient_active",
                    active_user_ids=active_user_ids,
                    vote_weight_policy=ROAST_REFILL_VOTE_WEIGHT_POLICY,
                )

            success_count = sum(
                1
                for raw in self.data.setdefault("roast_refill_requests", {}).values()
                if isinstance(raw, dict)
                and raw.get("date_str") == target_date
                and raw.get("group_id") == str(group_id)
                and raw.get("status") == "succeeded"
            )
            ratio, required_votes = roast_refill_threshold(len(active_user_ids), success_count)
            request_id = uuid.uuid4().hex
            raw = {
                "request_id": request_id,
                "date_str": target_date,
                "group_id": str(group_id),
                "initiator_id": str(initiator_id),
                "initiator_name": str(initiator_name),
                "delivery_bot_id": str(delivery_bot_id),
                "message_id": "",
                "active_count_snapshot": len(active_user_ids),
                "required_ratio": ratio,
                "required_votes": required_votes,
                "success_count_before": success_count,
                "status": "voting",
                "created_at": now.isoformat(),
                "expires_at": (now + datetime.timedelta(seconds=ROAST_REFILL_TTL_SECONDS)).isoformat(),
                "completed_at": "",
                "benefited_user_ids": [],
                "failure_reason": "",
            }
            self.data["roast_refill_requests"][request_id] = raw
            await self._atomic_save()
            return GroupRoastRefillPrepareResult(
                "created",
                self._refill_from_raw(raw),
                active_user_ids,
                vote_weight_policy=ROAST_REFILL_VOTE_WEIGHT_POLICY,
            )

    async def bind_group_roast_refill_message(
        self,
        request_id: str,
        message_id: str,
        now_ts: Optional[float] = None,
    ) -> Optional[GroupRoastRefillRequest]:
        normalized_message_id = str(message_id or "")
        if not normalized_message_id:
            return None
        now_value = time.time() if now_ts is None else float(now_ts)
        now = datetime.datetime.fromtimestamp(now_value, tz=datetime.timezone.utc)
        async with self._lock:
            raw = self._find_refill_locked(request_id)
            if raw is None:
                return None
            # 发送投票消息可能跨过 TTL；过期判定与绑定必须在同一把锁内完成，
            # 否则会向群里留下一个永远不可能成功的投票入口。
            if self._expire_refill_locked(raw, now):
                await self._atomic_save()
                return None
            if raw.get("status") != "voting":
                return None
            current_message_id = str(raw.get("message_id") or "")
            if current_message_id and current_message_id != normalized_message_id:
                return None
            raw["message_id"] = normalized_message_id
            await self._atomic_save()
            return self._refill_from_raw(raw)

    async def get_group_roast_refill(
        self,
        group_id: str,
        date_str: Optional[str] = None,
        now_ts: Optional[float] = None,
    ) -> Optional[GroupRoastRefillRequest]:
        target_date = date_str or rollpig_date_str()
        now = datetime.datetime.fromtimestamp(float(now_ts or time.time()), tz=datetime.timezone.utc)
        async with self._lock:
            for raw in self.data.setdefault("roast_refill_requests", {}).values():
                if not isinstance(raw, dict) or raw.get("date_str") != target_date or raw.get("group_id") != str(group_id):
                    continue
                if self._expire_refill_locked(raw, now):
                    await self._atomic_save()
                if raw.get("status") == "voting":
                    return self._refill_from_raw(raw)
            return None

    async def fail_group_roast_refill(
        self,
        request_id: str,
        message_id: str,
        reason: str,
    ) -> bool:
        async with self._lock:
            raw = self._find_refill_locked(request_id)
            if raw is None or raw.get("status") != "voting":
                return False
            if message_id and str(raw.get("message_id") or "") not in {"", str(message_id)}:
                return False
            raw["status"] = "failed"
            raw["failure_reason"] = str(reason)
            await self._atomic_save()
            return True

    async def complete_group_roast_refill(
        self,
        *,
        request_id: str,
        message_id: str,
        voter_ids: list[str],
        excluded_user_ids: list[str],
        manager_voter_ids: Optional[list[str]] = None,
        max_charges: int = DEFAULT_ROAST_CHARGE_MAX,
        now_ts: Optional[float] = None,
    ) -> GroupRoastRefillCompleteResult:
        """在同一把锁内验票、批量满格并完成状态迁移，确保成功只发生一次。"""

        now_value = time.time() if now_ts is None else float(now_ts)
        now = datetime.datetime.fromtimestamp(now_value, tz=datetime.timezone.utc)
        _, charge_max = _normalize_charge_settings(ROAST_COOLDOWN_SECONDS, max_charges)
        async with self._lock:
            raw = self._find_refill_locked(request_id)
            if raw is None:
                return GroupRoastRefillCompleteResult(False, "missing")
            if raw.get("status") != "voting":
                return GroupRoastRefillCompleteResult(False, str(raw.get("status") or "invalid"), self._refill_from_raw(raw))
            stored_message_id = str(raw.get("message_id") or "")
            submitted_message_id = str(message_id or "")
            if (
                not stored_message_id
                or not submitted_message_id
                or stored_message_id != submitted_message_id
            ):
                return GroupRoastRefillCompleteResult(False, "message_mismatch", self._refill_from_raw(raw))
            if self._expire_refill_locked(raw, now):
                await self._atomic_save()
                return GroupRoastRefillCompleteResult(False, "expired", self._refill_from_raw(raw))

            active_user_ids = self._group_active_users_locked(str(raw.get("date_str")), str(raw.get("group_id")))
            excluded = {str(user_id) for user_id in excluded_user_ids if user_id}
            valid_voters = tuple(sorted(({str(user_id) for user_id in voter_ids if user_id} & active_user_ids) - excluded))
            valid_voter_set = set(valid_voters)
            valid_manager_voters = valid_voter_set & {
                str(user_id) for user_id in (manager_voter_ids or []) if user_id
            }
            effective_votes = len(valid_voters) + len(valid_manager_voters)
            required_votes = max(2, _safe_int(raw.get("required_votes"), 2))
            # 管理员双票只能降低票数门槛所需的人数，不能让单人独自通过集体表决。
            if (
                len(valid_voters) < ROAST_REFILL_MIN_DISTINCT_VOTERS
                or effective_votes < required_votes
            ):
                return GroupRoastRefillCompleteResult(
                    False,
                    "pending",
                    self._refill_from_raw(raw),
                    valid_voter_ids=valid_voters,
                    effective_votes=effective_votes,
                )

            benefited = tuple(sorted(active_user_ids - excluded))
            usage = self.data.setdefault("usage", {})
            for user_id in benefited:
                previous = usage.get(user_id)
                last_roast_ts = previous.get("last_roast_ts") if isinstance(previous, dict) else None
                usage[user_id] = {
                    "last_roast_ts": last_roast_ts,
                    "roast_charges": charge_max,
                    "roast_charge_updated_ts": now_value,
                }
            raw["status"] = "succeeded"
            raw["completed_at"] = now.isoformat()
            raw["benefited_user_ids"] = list(benefited)
            await self._atomic_save()
            request = self._refill_from_raw(raw)
            return GroupRoastRefillCompleteResult(
                True,
                "succeeded",
                request,
                valid_voter_ids=valid_voters,
                benefited_user_ids=benefited,
                effective_votes=effective_votes,
            )

    async def mark_group_roll_seen(
        self,
        user_id: str,
        pig_id: str,
        group_id: str,
        date_str: Optional[str] = None,
    ):
        """将已有的今日形态登记到当前群，避免群内统计漏记。"""
        if not group_id:
            return
        async with self._lock:
            target_date = date_str or rollpig_date_str()
            self._record_group_roll(target_date, group_id, user_id, pig_id)
            await self._atomic_save()

    def get_pig_by_date(self, user_id: str, date_str: str) -> Optional[str]:
        """返回指定日期的 pig_id，无记录返回 None。"""
        return self.data["history"].get(date_str, {}).get(user_id)

    def get_user_collection(self, user_id: str) -> List[str]:
        # 返回副本，避免命令层无意修改内部 list 后绕过锁与原子保存。
        return list(self.data.get("collection", {}).get(user_id, []))

    async def clean_old_history(self, days_to_keep: int = 14):
        """清理超过 days_to_keep 天的历史记录（不影响图鉴数据）。"""
        async with self._lock:
            today = rollpig_today()
            now = datetime.datetime.now(datetime.timezone.utc)
            history_dates_to_del = [
                d for d in self.data["history"]
                if _is_valid_date(d)  # 必须先过滤非法日期键，再做计算（防止 ValueError）
                and (today - datetime.date.fromisoformat(d)).days > days_to_keep
            ]
            for d in history_dates_to_del:
                del self.data["history"][d]

            daily_roll_snapshots = self.data.get("daily_roll_snapshots", {})
            snapshot_dates_to_del = [
                date_str
                for date_str in daily_roll_snapshots
                if date_str in history_dates_to_del
                or (
                    _is_valid_date(date_str)
                    and (today - datetime.date.fromisoformat(date_str)).days > days_to_keep
                )
            ]
            for date_str in snapshot_dates_to_del:
                del daily_roll_snapshots[date_str]

            daily_feeds = self.data.get("daily_feeds", {})
            feed_dates_to_del = [
                date_str
                for date_str in daily_feeds
                if _is_valid_date(date_str)
                and (today - datetime.date.fromisoformat(date_str)).days > days_to_keep
            ]
            for date_str in feed_dates_to_del:
                del daily_feeds[date_str]

            group_rolls = self.data.get("group_rolls", {})
            group_dates_to_del = [
                d for d in group_rolls
                if _is_valid_date(d)
                and (today - datetime.date.fromisoformat(d)).days > days_to_keep
            ]
            for d in group_dates_to_del:
                del group_rolls[d]

            group_roll_seen_at = self.data.get("group_roll_seen_at", {})
            for d in list(group_roll_seen_at):
                if _is_valid_date(d) and (today - datetime.date.fromisoformat(d)).days > days_to_keep:
                    del group_roll_seen_at[d]

            active_users = self.data.get("group_daily_active_users", {})
            active_dates_to_del = [
                d for d in active_users
                if _is_valid_date(d)
                and (today - datetime.date.fromisoformat(d)).days > days_to_keep
            ]
            for d in active_dates_to_del:
                del active_users[d]

            group_daily_active_at = self.data.get("group_daily_active_at", {})
            for d in list(group_daily_active_at):
                if _is_valid_date(d) and (today - datetime.date.fromisoformat(d)).days > days_to_keep:
                    del group_daily_active_at[d]

            refill_requests = self.data.get("roast_refill_requests", {})
            refill_ids_to_del = [
                request_id
                for request_id, raw in refill_requests.items()
                if isinstance(raw, dict)
                and _is_valid_date(str(raw.get("date_str") or ""))
                and (today - datetime.date.fromisoformat(str(raw["date_str"]))).days > days_to_keep
            ]
            for request_id in refill_ids_to_del:
                del refill_requests[request_id]

            # completed 已由事件记录承接，其他预约跨日后也不再有效；每日清理直接
            # 移除这些终态/过期记录，避免结果快照让 JSON 与滚动备份无限增长。
            reservations = self.data.get("roast_reservations", {})
            reservation_ids_to_del = [
                reservation_id
                for reservation_id, raw in reservations.items()
                if isinstance(raw, dict)
                and (
                    raw.get("status") == "completed"
                    or (
                        _is_valid_date(str(raw.get("date_str") or ""))
                        and datetime.date.fromisoformat(str(raw["date_str"])) < today
                        and not _matches_reservation_claim_date(raw, today.isoformat(), now)
                    )
                )
            ]
            for reservation_id in reservation_ids_to_del:
                del reservations[reservation_id]

            # 未抽猪违规次数只在当日判定中使用，跨日后直接清理，避免日期分区无限增长。
            unrolled_attempts = self.data.get("unrolled_roast_attempts", {})
            attempt_dates_to_del = [
                date_str
                for date_str in unrolled_attempts
                if _is_valid_date(date_str) and datetime.date.fromisoformat(date_str) < today
            ]
            for date_str in attempt_dates_to_del:
                del unrolled_attempts[date_str]

            if (
                history_dates_to_del
                or snapshot_dates_to_del
                or feed_dates_to_del
                or group_dates_to_del
                or active_dates_to_del
                or refill_ids_to_del
                or reservation_ids_to_del
                or attempt_dates_to_del
            ):
                await self._atomic_save()

    # ---- 烤群友 普通模式充能 ----

    def check_roast_usage(self, user_id: str) -> tuple[bool, str]:
        """
        Deprecated: 仅保留给旧调用兜底；新流程必须使用 consume_roast_usage()。
        检查与扣减分离会重新引入 TOCTOU 竞态，因此不要在新代码中调用本函数。
        返回: (是否可用, 若不可用时的提示信息)
        """
        usage = self.data.setdefault("usage", {})
        raw_state = usage.get(user_id, 0)
        now = float(time.time())
        cooldown, max_charges = _normalize_charge_settings(ROAST_COOLDOWN_SECONDS, DEFAULT_ROAST_CHARGE_MAX)

        if isinstance(raw_state, dict):
            charges = _safe_int(raw_state.get("roast_charges"), 0)
            updated_ts = float(raw_state.get("roast_charge_updated_ts") or now)
        else:
            charges, updated_ts = _legacy_roast_state(float(raw_state or 0), now, cooldown, max_charges)

        charges, updated_ts = _recover_roast_charges(charges, updated_ts, now, cooldown, max_charges)
        if charges <= 0:
            remaining = _next_charge_seconds(charges, updated_ts, now, cooldown, max_charges)
            m, s = divmod(remaining, 60)
            h, m = divmod(m, 60)
            time_str = f"{h}小时{m}分" if h > 0 else f"{m}分{s}秒"
            return False, f"烧烤充能恢复中！还需要 {time_str} 恢复 1 次。"

        return True, ""

    async def consume_roast_usage(
        self,
        user_id: str,
        now_ts: Optional[float] = None,
        cooldown_seconds: Optional[int] = None,
        max_charges: Optional[int] = None,
    ) -> CooldownConsumeResult:
        now = float(now_ts or time.time())
        cooldown, charge_max = _normalize_charge_settings(cooldown_seconds, max_charges)
        async with self._lock:
            usage = self.data.setdefault("usage", {})
            raw_state = usage.get(user_id, 0)
            if isinstance(raw_state, dict):
                charges = _safe_int(raw_state.get("roast_charges"), 0)
                updated_ts = float(raw_state.get("roast_charge_updated_ts") or now)
            else:
                charges, updated_ts = _legacy_roast_state(float(raw_state or 0), now, cooldown, charge_max)

            charges, updated_ts = _recover_roast_charges(charges, updated_ts, now, cooldown, charge_max)
            if charges <= 0:
                remaining = _next_charge_seconds(charges, updated_ts, now, cooldown, charge_max)
                usage[user_id] = {
                    "last_roast_ts": float(raw_state or 0) if not isinstance(raw_state, dict) else raw_state.get("last_roast_ts"),
                    "roast_charges": charges,
                    "roast_charge_updated_ts": updated_ts,
                }
                await self._atomic_save()
                return CooldownConsumeResult(
                    allowed=False,
                    remaining_seconds=remaining,
                    charges_left=0,
                    max_charges=charge_max,
                    next_recover_seconds=remaining,
                )

            was_full = charges >= charge_max
            charges -= 1
            if was_full:
                updated_ts = now
            usage[user_id] = {
                "last_roast_ts": now,
                "roast_charges": charges,
                "roast_charge_updated_ts": updated_ts,
            }
            await self._atomic_save()
            return CooldownConsumeResult(
                allowed=True,
                remaining_seconds=0,
                charges_left=charges,
                max_charges=charge_max,
                next_recover_seconds=_next_charge_seconds(charges, updated_ts, now, cooldown, charge_max),
            )

    async def update_roast_usage(self, user_id: str):
        """Deprecated: 仅保留给旧调用兜底；新流程必须使用 consume_roast_usage() 原子扣减。"""
        async with self._lock:
            usage = self.data.setdefault("usage", {})
            now = time.time()
            usage[user_id] = {
                "last_roast_ts": now,
                "roast_charges": max(0, DEFAULT_ROAST_CHARGE_MAX - 1),
                "roast_charge_updated_ts": now,
            }
            await self._atomic_save()

    # ---- 烤群友 后门口令 每日计数 ----

    def check_force_roast_usage(self, user_id: str) -> bool:
        """普通用户后门：每日仅 1 次，返回今日是否仍可用。"""
        today = rollpig_date_str()
        if "force_usage" not in self.data or not isinstance(self.data["force_usage"], dict):
            self.data["force_usage"] = {}
        return self.data["force_usage"].get(user_id) != today

    async def consume_force_roast_usage(self, user_id: str, date_str: Optional[str] = None) -> bool:
        target_date = date_str or rollpig_date_str()
        async with self._lock:
            usage = self.data.setdefault("force_usage", {})
            if usage.get(user_id) == target_date:
                return False
            usage[user_id] = target_date
            await self._atomic_save()
            return True

    async def update_force_roast_usage(self, user_id: str):
        async with self._lock:
            today = rollpig_date_str()
            self.data.setdefault("force_usage", {})[user_id] = today
            await self._atomic_save()

    # ================================ 每日加餐结算 ================================ #

    @staticmethod
    def _daily_feed_to_raw(result: DailyFeedResult) -> dict:
        """把加餐结果固化为稳定 JSON，供预约重领原样恢复。"""

        return {
            "status": result.status,
            "user_id": result.user_id,
            "pig_id": result.pig_id,
            "previous_level": result.previous_level,
            "new_level": result.new_level,
            "source_type": result.source_type,
            "source_id": result.source_id,
            "created_at": result.created_at,
        }

    def _apply_daily_feed_locked(
        self,
        *,
        date_str: str,
        user_id: str,
        source_type: str,
        source_id: str,
    ) -> DailyFeedResult:
        """在数据锁内尝试加餐；只有真实升级才写入日期唯一记录。"""

        normalized_user_id = str(user_id)
        pig_id = str(
            self.data.setdefault("history", {}).get(date_str, {}).get(normalized_user_id)
            or ""
        )
        if not pig_id:
            return DailyFeedResult(
                status="no_daily_pig",
                user_id=normalized_user_id,
                source_type=source_type,
                source_id=source_id,
            )

        day_feeds = self.data.setdefault("daily_feeds", {}).setdefault(date_str, {})
        existing = day_feeds.get(normalized_user_id)
        if isinstance(existing, dict):
            same_source = (
                str(existing.get("source_type") or "") == source_type
                and str(existing.get("source_id") or "") == source_id
            )
            return DailyFeedResult(
                status="fed" if same_source else "already_fed",
                user_id=normalized_user_id,
                pig_id=str(existing.get("pig_id") or pig_id),
                previous_level=max(0, min(5, _safe_int(existing.get("previous_level"), 0))),
                new_level=max(0, min(5, _safe_int(existing.get("new_level"), 0))),
                source_type=source_type,
                source_id=source_id,
                created_at=str(existing.get("created_at") or ""),
            )

        user_progress = self.data.setdefault("pig_progress", {}).setdefault(
            normalized_user_id,
            {},
        )
        if not isinstance(user_progress, dict):
            user_progress = {}
            self.data["pig_progress"][normalized_user_id] = user_progress
        raw_progress = user_progress.get(pig_id)
        if not isinstance(raw_progress, dict):
            raw_progress = {
                "copies": 1,
                "growth_bonus": 0,
                "first_obtained_at": None,
            }
            user_progress[pig_id] = raw_progress

        copies = max(1, _safe_int(raw_progress.get("copies"), 1))
        growth_bonus = max(0, _safe_int(raw_progress.get("growth_bonus"), 0))
        previous_level = expert_level_from_copies(copies, growth_bonus)
        if previous_level >= 5:
            return DailyFeedResult(
                status="max_level",
                user_id=normalized_user_id,
                pig_id=pig_id,
                previous_level=previous_level,
                new_level=previous_level,
                source_type=source_type,
                source_id=source_id,
            )

        raw_progress["growth_bonus"] = growth_bonus + 1
        new_level = expert_level_from_copies(copies, growth_bonus + 1)
        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        result = DailyFeedResult(
            status="fed",
            user_id=normalized_user_id,
            pig_id=pig_id,
            previous_level=previous_level,
            new_level=new_level,
            source_type=source_type,
            source_id=source_id,
            created_at=created_at,
        )
        day_feeds[normalized_user_id] = self._daily_feed_to_raw(result)
        return result

    # ================================ 烤群友事件记录 ================================ #
    # 预约完成和日报事件必须共用同一次原子保存；普通即时事件仍复用同一序列化入口。

    def _append_roast_event_locked(self, event: RoastEvent, *, date_str: str) -> bool:
        """在持有数据锁时追加事件；预约事件跨日期按 reservation_id 幂等。"""

        events = self.data.setdefault("daily_events", {})
        if event.reservation_id:
            for existing_events in events.values():
                if any(
                    isinstance(item, dict) and item.get("reservation_id") == event.reservation_id
                    for item in existing_events
                ):
                    return False
        day_events = events.setdefault(date_str, [])
        if event.event_id and any(
            isinstance(item, dict) and item.get("event_id") == event.event_id
            for item in day_events
        ):
            return False
        day_events.append({
            "event_id": str(event.event_id or uuid.uuid4().hex),
            "created_at": str(
                event.created_at
                or datetime.datetime.now(datetime.timezone.utc).isoformat()
            ),
            "type": event.event_type,
            "attacker": event.attacker_id,
            "target": event.target_id,
            "attacker_name": event.attacker_name,
            "target_name": event.target_name,
            "food": event.food,
            "group_id": event.group_id,
            "reservation_id": event.reservation_id,
            "participant_ids": list(event.participant_ids),
            "participant_names": list(event.participant_names),
            "participant_count": max(0, int(event.participant_count or 0)),
            "backfire_victim_id": str(event.backfire_victim_id or ""),
            "backfire_victim_name": str(event.backfire_victim_name or ""),
            "special_reason": str(event.special_reason or ""),
        })
        if event.group_id:
            active_user_ids = [event.attacker_id, *event.participant_ids]
            if event.event_type != "bot_backfire":
                active_user_ids.append(event.target_id)
            self._mark_group_active_users_locked(
                date_str,
                str(event.group_id),
                active_user_ids,
            )
        return True

    async def log_roast_event(self, event_type: str, attacker_id: str, target_id: str,
                               attacker_name: str = "", target_name: str = "",
                               food: str = "", group_id: str = "",
                               reservation_id: str = "", participant_ids: Optional[list[str]] = None,
                               participant_names: Optional[list[str]] = None, participant_count: int = 0,
                               backfire_victim_id: str = "", backfire_victim_name: str = "",
                               special_reason: str = "", event_id: str = "", created_at: str = "",
                               settle_daily_feed: bool = False) -> Optional[DailyFeedResult]:
        """
        记录一次烤群友事件。
        event_type: "success" / "escape" / "backfire" / "bot_backfire" / "self_roast"
        """
        async with self._lock:
            # 事件与加餐必须使用同一个业务日期，避免午夜切换时拆到两天。
            target_date = rollpig_date_str()
            event = RoastEvent(
                    event_type=event_type,
                    attacker_id=attacker_id,
                    target_id=target_id,
                    attacker_name=attacker_name,
                    target_name=target_name,
                    food=food,
                    group_id=group_id,
                    reservation_id=reservation_id,
                    participant_ids=tuple(participant_ids or ()),
                    participant_names=tuple(participant_names or ()),
                    participant_count=participant_count,
                    backfire_victim_id=backfire_victim_id,
                    backfire_victim_name=backfire_victim_name,
                    special_reason=special_reason,
                    event_id=str(event_id or uuid.uuid4().hex),
                    created_at=created_at,
                )
            changed = self._append_roast_event_locked(
                event,
                date_str=target_date,
            )
            feed_result = None
            if settle_daily_feed and event_type == "success" and not reservation_id:
                feed_result = self._apply_daily_feed_locked(
                    date_str=target_date,
                    user_id=attacker_id,
                    source_type="roast",
                    source_id=str(event.event_id or ""),
                )
            if changed or (feed_result is not None and feed_result.status == "fed"):
                await self._atomic_save()
            return feed_result

    def get_daily_events(
        self,
        date_str: Optional[str] = None,
        group_id: Optional[str] = None,
        user_id: Optional[str] = None,
        cutoff_at: Optional[str] = None,
    ) -> list:
        """获取指定日期（默认今天）的所有烤群友事件。"""
        if not date_str:
            date_str = rollpig_date_str()
        events = self.data.get("daily_events", {}).get(date_str, [])
        result = [dict(event) for event in events if isinstance(event, dict)]
        cutoff = _parse_utc_datetime(cutoff_at)
        if cutoff is not None:
            result = [
                event
                for event in result
                if _timestamp_not_after_cutoff(event.get("created_at"), cutoff)
            ]
        if group_id:
            result = [event for event in result if str(event.get("group_id") or "") == str(group_id)]
        if user_id:
            normalized_user_id = str(user_id)
            filtered: list[dict] = []
            for event in result:
                raw_participant_ids = event.get("participant_ids", [])
                participant_ids = raw_participant_ids if isinstance(raw_participant_ids, list) else []
                if (
                    str(event.get("attacker") or "") == normalized_user_id
                    or str(event.get("target") or "") == normalized_user_id
                    or normalized_user_id in {str(item) for item in participant_ids}
                    or str(event.get("backfire_victim_id") or "") == normalized_user_id
                ):
                    filtered.append(event)
            result = filtered
        return result

    def get_recent_rolls(self, user_id: str, days: int = 14) -> dict[str, str]:
        """返回最近若干天的抽猪记录；图鉴只读使用，不会修改 copies。"""
        today = rollpig_today()
        safe_days = max(1, min(60, int(days or 14)))
        start_date = today - datetime.timedelta(days=safe_days - 1)
        result: dict[str, str] = {}
        for date_str, rows in self.data.get("history", {}).items():
            if not _is_valid_date(date_str) or not isinstance(rows, dict):
                continue
            date_obj = datetime.date.fromisoformat(date_str)
            if start_date <= date_obj <= today:
                pig_id = rows.get(str(user_id))
                if pig_id:
                    result[date_str] = str(pig_id)
        return dict(sorted(result.items(), reverse=True))

    def count_success_roasted(self, user_id: str, days: int = 7) -> int:
        """统计用户近 N 天成功被烤次数；逃脱/反噬不算“被烤成功”。"""
        today = rollpig_today()
        safe_days = max(1, min(60, int(days or 7)))
        start_date = today - datetime.timedelta(days=safe_days - 1)
        total = 0
        for date_str, events in self.data.get("daily_events", {}).items():
            if not _is_valid_date(date_str) or not isinstance(events, list):
                continue
            date_obj = datetime.date.fromisoformat(date_str)
            if not (start_date <= date_obj <= today):
                continue
            total += sum(
                1
                for event in events
                if isinstance(event, dict)
                and event.get("type") == "success"
                and str(event.get("target") or "") == str(user_id)
            )
        return total

    def get_catalog_snapshot(self, user_id: str, days: int = 14) -> CatalogSnapshot:
        """聚合图片版图鉴需要的本地只读数据，避免命令层多处手算。"""
        return CatalogSnapshot(
            draw_state=self.get_draw_state(user_id),
            recent_rolls=self.get_recent_rolls(user_id, days=days),
            roasted_7d=self.count_success_roasted(user_id, days=7),
        )

    # ================================ 日报本地资料快照 ================================ #

    def _daily_roll_created_at_locked(self, date_str: str, user_id: str) -> str:
        """读取本地抽取时间；升级前快照回退到最早的群内显形时间。"""

        raw = self.data.get("daily_roll_snapshots", {}).get(date_str, {}).get(user_id)
        if isinstance(raw, dict) and raw.get("created_at"):
            return str(raw["created_at"])

        timestamps: list[tuple[datetime.datetime, str]] = []
        day_seen = self.data.get("group_roll_seen_at", {}).get(date_str, {})
        if isinstance(day_seen, dict):
            for group_seen in day_seen.values():
                if not isinstance(group_seen, dict):
                    continue
                value = str(group_seen.get(user_id) or "")
                parsed = _parse_utc_datetime(value)
                if parsed is not None:
                    timestamps.append((parsed, value))
        return min(timestamps, default=(None, ""), key=lambda item: item[0])[1]

    def _daily_roll_not_after_cutoff_locked(
        self,
        date_str: str,
        user_id: str,
        *,
        report_date: datetime.date,
        cutoff: datetime.datetime,
    ) -> bool:
        """判断一条用户全局抽取是否属于本期日报快照。"""

        if not _is_valid_date(date_str):
            return False
        roll_date = datetime.date.fromisoformat(date_str)
        if roll_date != report_date:
            return roll_date < report_date
        created_at = self._daily_roll_created_at_locked(date_str, user_id)
        return _timestamp_not_after_cutoff(created_at, cutoff)

    def _copies_at_cutoff_locked(
        self,
        user_id: str,
        pig_id: str,
        *,
        report_date: datetime.date,
        cutoff: datetime.datetime,
    ) -> int:
        """利用截止点后的首次 previous_copies 还原当时的累计抽取数。"""

        raw_progress = self.data.get("pig_progress", {}).get(user_id, {})
        raw_item = raw_progress.get(pig_id, {}) if isinstance(raw_progress, dict) else {}
        current_copies = _safe_int(raw_item.get("copies"), 0) if isinstance(raw_item, dict) else 0
        future_previous: list[int] = []
        for date_str, day_snapshots in self.data.get("daily_roll_snapshots", {}).items():
            if not isinstance(day_snapshots, dict):
                continue
            raw = day_snapshots.get(user_id)
            if not isinstance(raw, dict) or str(raw.get("pig_id") or "") != pig_id:
                continue
            if self._daily_roll_not_after_cutoff_locked(
                str(date_str),
                user_id,
                report_date=report_date,
                cutoff=cutoff,
            ):
                continue
            previous_copies = _optional_nonnegative_int(raw.get("previous_copies"))
            if previous_copies is not None:
                future_previous.append(previous_copies)
        return min(future_previous, default=current_copies)

    def _growth_bonus_at_cutoff_locked(
        self,
        user_id: str,
        pig_id: str,
        *,
        cutoff: datetime.datetime,
    ) -> int:
        """从当前累计加餐中扣除截止点之后的记录，恢复日报时点等级。"""

        raw_progress = self.data.get("pig_progress", {}).get(user_id, {})
        raw_item = raw_progress.get(pig_id, {}) if isinstance(raw_progress, dict) else {}
        current_bonus = (
            max(0, _safe_int(raw_item.get("growth_bonus"), 0))
            if isinstance(raw_item, dict)
            else 0
        )
        future_count = 0
        for day_feeds in self.data.get("daily_feeds", {}).values():
            if not isinstance(day_feeds, dict):
                continue
            raw_feed = day_feeds.get(user_id)
            if not isinstance(raw_feed, dict) or str(raw_feed.get("pig_id") or "") != pig_id:
                continue
            created_at = _parse_utc_datetime(raw_feed.get("created_at"))
            if created_at is not None and created_at > cutoff:
                future_count += 1
        return max(0, current_bonus - future_count)

    def _build_daily_report_profiles_locked(
        self,
        *,
        date_str: str,
        cutoff_at: str,
        user_ids: tuple[str, ...],
    ) -> tuple[DailyReportProfileSnapshot, ...]:
        """从同一份锁定数据构建整群资料，过程中不重复扫描事件账本。"""

        cutoff = _parse_utc_datetime(cutoff_at)
        if cutoff is None or not _is_valid_date(date_str):
            raise ValueError("日报日期或截止时间无效")
        report_date = datetime.date.fromisoformat(date_str)
        history = self.data.get("history", {})
        snapshots = self.data.get("daily_roll_snapshots", {})
        progress_by_user = self.data.get("pig_progress", {})
        collection_by_user = self.data.get("collection", {})
        start_date = report_date - datetime.timedelta(days=13)
        result: list[DailyReportProfileSnapshot] = []

        for user_id in dict.fromkeys(str(item) for item in user_ids if item):
            raw_progress = progress_by_user.get(user_id, {})
            raw_progress = raw_progress if isinstance(raw_progress, dict) else {}
            raw_collection = collection_by_user.get(user_id, [])
            catalog_ids = (
                {str(pig_id) for pig_id in raw_collection}
                if isinstance(raw_collection, list)
                else set()
            )
            catalog_ids.update(str(pig_id) for pig_id in raw_progress)

            visible_catalog_ids: set[str] = set()
            obtained_times: list[tuple[datetime.datetime, str]] = []
            for pig_id in catalog_ids:
                raw_item = raw_progress.get(pig_id, {})
                obtained_at = (
                    str(raw_item.get("first_obtained_at") or "")
                    if isinstance(raw_item, dict)
                    else ""
                )
                parsed_obtained_at = _parse_utc_datetime(obtained_at)
                if parsed_obtained_at is not None and parsed_obtained_at > cutoff:
                    continue
                visible_catalog_ids.add(pig_id)
                if parsed_obtained_at is not None:
                    obtained_times.append((parsed_obtained_at, obtained_at))

            day_history = history.get(date_str, {}) if isinstance(history, dict) else {}
            daily_pig_id = (
                str(day_history.get(user_id) or "")
                if isinstance(day_history, dict)
                else ""
            )
            daily_visible = bool(daily_pig_id) and self._daily_roll_not_after_cutoff_locked(
                date_str,
                user_id,
                report_date=report_date,
                cutoff=cutoff,
            )
            raw_daily = (
                snapshots.get(date_str, {}).get(user_id)
                if isinstance(snapshots, dict)
                and isinstance(snapshots.get(date_str), dict)
                else None
            )
            daily_copies = (
                _optional_nonnegative_int(raw_daily.get("copies_after_roll"))
                if daily_visible and isinstance(raw_daily, dict)
                else None
            )
            raw_daily_level = (
                _optional_nonnegative_int(raw_daily.get("expert_level_after_roll"))
                if daily_visible and isinstance(raw_daily, dict)
                else None
            )
            # ================================ 截止点等级与达成时间 ================================ #
            daily_feed = parse_applied_daily_feed(
                self.data.get("daily_feeds", {}).get(date_str, {}).get(user_id),
                pig_id=daily_pig_id,
                user_id=user_id,
            )
            if daily_feed is not None and not _timestamp_not_after_cutoff(daily_feed.created_at, cutoff):
                daily_feed = None
            daily_achieved_at = (
                self._daily_roll_created_at_locked(date_str, user_id)
                if daily_visible
                else ""
            )
            if raw_daily_level is not None:
                daily_ex_level = min(5, raw_daily_level)
                daily_base_level = daily_ex_level
            elif daily_copies is not None:
                # 旧快照没有冻结 EX；累计 bonus 已含当天加餐，先扣除再统一应用，
                # 避免重复加级，并让满级前后的达成时间判断使用同一基准。
                bonus = self._growth_bonus_at_cutoff_locked(user_id, daily_pig_id, cutoff=cutoff)
                daily_base_level = expert_level_from_copies(
                    daily_copies, max(0, bonus - int(daily_feed is not None)),
                )
                daily_ex_level = expert_level_from_copies(daily_copies, bonus)
            else:
                daily_ex_level = None
                daily_base_level = None
            if daily_feed is not None and daily_base_level is not None and daily_feed.new_level > daily_base_level:
                daily_ex_level = max(daily_ex_level, daily_feed.new_level)
                # 缺失或损坏时间继续兼容展示等级，但不能编造新的排序时间。
                if _parse_utc_datetime(daily_feed.created_at) is not None:
                    daily_achieved_at = daily_feed.created_at

            recent_pig_id = ""
            if isinstance(history, dict):
                recent_dates = sorted(
                    (str(item) for item in history if _is_valid_date(str(item))),
                    reverse=True,
                )
                for recent_date in recent_dates:
                    recent_day = datetime.date.fromisoformat(recent_date)
                    if not start_date <= recent_day <= report_date:
                        continue
                    rows = history.get(recent_date)
                    candidate = str(rows.get(user_id) or "") if isinstance(rows, dict) else ""
                    if candidate and self._daily_roll_not_after_cutoff_locked(
                        recent_date,
                        user_id,
                        report_date=report_date,
                        cutoff=cutoff,
                    ):
                        recent_pig_id = candidate
                        break

            recent_copies = (
                self._copies_at_cutoff_locked(
                    user_id,
                    recent_pig_id,
                    report_date=report_date,
                    cutoff=cutoff,
                )
                if recent_pig_id
                else 0
            )
            result.append(
                DailyReportProfileSnapshot(
                    user_id=user_id,
                    daily_pig_id=daily_pig_id if daily_visible else "",
                    daily_ex_level=daily_ex_level,
                    daily_achieved_at=daily_achieved_at,
                    catalog_count=len(visible_catalog_ids),
                    catalog_achieved_at=max(
                        obtained_times,
                        default=(None, ""),
                        key=lambda item: item[0],
                    )[1],
                    recent_pig_id=recent_pig_id,
                    recent_ex_level=(
                        expert_level_from_copies(
                            recent_copies,
                            self._growth_bonus_at_cutoff_locked(
                                user_id,
                                recent_pig_id,
                                cutoff=cutoff,
                            ),
                        )
                        if recent_pig_id
                        else None
                    ),
                )
            )
        return tuple(result)

    async def get_daily_report_profiles(
        self,
        *,
        date_str: str,
        cutoff_at: str,
        user_ids: tuple[str, ...],
    ) -> tuple[DailyReportProfileSnapshot, ...]:
        """锁定本地账本后在线程中一次性生成日报排行快照。"""

        # 所有本地写入都遵循同一把 asyncio.Lock。持锁期间工作线程只读取 data，
        # 既能获得一致快照，也不会占用 NoneBot 的事件循环执行聚合。
        async with self._lock:
            return await asyncio.to_thread(
                self._build_daily_report_profiles_locked,
                date_str=date_str,
                cutoff_at=cutoff_at,
                user_ids=user_ids,
            )

    # ================================ Local 日报投递状态 ================================ #

    async def claim_local_daily_report_deliveries(
        self,
        *,
        instance_id: str,
        delivery_bots: dict[str, str],
        date_str: str,
        cutoff_at: str,
        max_attempts: int,
    ) -> DailyReportDeliveryClaimResult:
        """持久化领取 Local 日报；返回前必须先把 claim 写入本地账本。"""

        terminal_statuses = {"sent", "skipped", "uncertain", "failed"}
        async with self._lock:
            deliveries = self.data.setdefault("daily_report_deliveries", {})
            changed = False
            for stored_date in tuple(deliveries):
                if stored_date != date_str:
                    deliveries.pop(stored_date, None)
                    changed = True
            day_map = deliveries.setdefault(date_str, {})
            now = datetime.datetime.now(datetime.timezone.utc)
            claims: list[DailyReportDeliveryClaim] = []
            next_claim_times: list[datetime.datetime] = []

            for group_id, delivery_bot_id in sorted(delivery_bots.items()):
                raw = day_map.setdefault(str(group_id), {})
                status = str(raw.get("status") or "pending")
                attempt_count = max(0, _safe_int(raw.get("attempt_count"), 0))
                if status in terminal_statuses:
                    continue
                if attempt_count >= max_attempts:
                    raw.update(
                        status="failed",
                        next_attempt_at="",
                        updated_at=now.isoformat(),
                    )
                    changed = True
                    continue
                if status in {"claimed", "sending"}:
                    continue

                next_attempt_at_raw = str(raw.get("next_attempt_at") or "")
                if next_attempt_at_raw:
                    try:
                        next_attempt_at = datetime.datetime.fromisoformat(
                            next_attempt_at_raw.replace("Z", "+00:00")
                        )
                        if next_attempt_at.tzinfo is None:
                            next_attempt_at = next_attempt_at.replace(
                                tzinfo=datetime.timezone.utc
                            )
                        next_attempt_at = next_attempt_at.astimezone(datetime.timezone.utc)
                    except ValueError:
                        next_attempt_at = None
                    if next_attempt_at is not None and next_attempt_at > now:
                        next_claim_times.append(next_attempt_at)
                        continue

                attempt_count += 1
                claim_token = (
                    f"local:{instance_id}:{date_str}:{group_id}:{attempt_count}"
                )
                raw.update(
                    status="claimed",
                    attempt_count=attempt_count,
                    delivery_bot_id=str(delivery_bot_id),
                    cutoff_at=cutoff_at,
                    claim_token=claim_token,
                    next_attempt_at="",
                    updated_at=now.isoformat(),
                )
                changed = True
                claims.append(
                    DailyReportDeliveryClaim(
                        date_str=date_str,
                        group_id=str(group_id),
                        delivery_bot_id=str(delivery_bot_id),
                        cutoff_at=cutoff_at,
                        claim_token=claim_token,
                        attempt_count=attempt_count,
                    )
                )

            if changed:
                await self._atomic_save()
            return DailyReportDeliveryClaimResult(
                claims=tuple(claims),
                next_claim_at=(
                    min(next_claim_times).isoformat()
                    if next_claim_times
                    else ""
                ),
            )

    async def transition_local_daily_report_delivery(
        self,
        claim: DailyReportDeliveryClaim,
        action: str,
        *,
        retry_seconds: int,
        max_attempts: int,
        message_id: str = "",
        error: str = "",
    ) -> DailyReportDeliveryTransitionResult:
        """原子保存 Local 日报状态；sending 必须先落盘再允许调用 QQ 发送。"""

        async with self._lock:
            raw = (
                self.data.setdefault("daily_report_deliveries", {})
                .get(claim.date_str, {})
                .get(claim.group_id)
            )
            if not isinstance(raw, dict) or raw.get("claim_token") != claim.claim_token:
                return DailyReportDeliveryTransitionResult(ok=False)

            status = str(raw.get("status") or "")
            attempt_count = max(0, _safe_int(raw.get("attempt_count"), 0))
            allowed_actions = {
                "claimed": {"sending", "release", "uncertain", "skip"},
                # retry 只表示外部接口已经明确拒绝发送；结果不明仍必须 uncertain。
                "sending": {"sent", "retry", "uncertain"},
            }.get(status, set())
            if action not in allowed_actions:
                return DailyReportDeliveryTransitionResult(
                    ok=False,
                    status=status,
                    attempt_count=attempt_count,
                )

            now = datetime.datetime.now(datetime.timezone.utc)
            next_attempt_at = ""
            if action in {"release", "retry"}:
                if attempt_count >= max_attempts:
                    next_status = "failed"
                else:
                    next_status = "pending"
                    next_attempt_at = (
                        now + datetime.timedelta(seconds=max(0, retry_seconds))
                    ).isoformat()
            else:
                next_status = "skipped" if action == "skip" else action

            raw.update(
                status=next_status,
                next_attempt_at=next_attempt_at,
                message_id=str(message_id or "")[:128],
                last_error=str(error or "")[:1000],
                updated_at=now.isoformat(),
            )
            await self._atomic_save()
            return DailyReportDeliveryTransitionResult(
                ok=True,
                status=next_status,
                attempt_count=attempt_count,
                next_attempt_at=next_attempt_at,
            )

    def get_group_rolls(
        self,
        group_id: str,
        date_str: Optional[str] = None,
        cutoff_at: Optional[str] = None,
    ) -> dict:
        """获取指定群在某天登记过的今日形态。"""
        if not date_str:
            date_str = rollpig_date_str()
        rolls = dict(self.data.get("group_rolls", {}).get(date_str, {}).get(group_id, {}))
        cutoff = _parse_utc_datetime(cutoff_at)
        if cutoff is None:
            return rolls
        seen_at = self.data.get("group_roll_seen_at", {}).get(
            date_str,
            {},
        ).get(group_id, {})
        return {
            user_id: pig_id
            for user_id, pig_id in rolls.items()
            if _timestamp_not_after_cutoff(seen_at.get(user_id), cutoff)
        }

    def get_active_group_ids(self, date_str: Optional[str] = None) -> set[str]:
        """获取指定日期内有抽猪或烧烤活动的群号集合。"""
        if not date_str:
            date_str = rollpig_date_str()

        event_groups = {
            str(e.get("group_id"))
            for e in self.get_daily_events(date_str)
            if e.get("group_id")
        }
        roll_groups = {
            str(group_id)
            for group_id in self.data.get("group_rolls", {}).get(date_str, {}).keys()
            if group_id
        }
        return event_groups | roll_groups

    # ---- 被烤最多 → 次日保护 ----

    def is_protected(self, group_id: str, user_id: str, date_str: Optional[str] = None) -> bool:
        """检查用户在当前群今日是否受保护。"""
        target_date = date_str or rollpig_date_str()
        protected_map = self.data.get("protected", {}).get(target_date, {})
        if not isinstance(protected_map, dict):
            return False
        group_users = protected_map.get(group_id, [])
        legacy_users = protected_map.get("__all__", [])
        return user_id in group_users or user_id in legacy_users

    async def replace_group_protected_users(
        self,
        group_id: str,
        user_ids: list[str],
        protect_date: Optional[str] = None,
    ):
        """按群设置某日受保护的用户列表。"""
        target_date = protect_date or rollpig_date_str(1)
        async with self._lock:
            protected = self.data.setdefault("protected", {})
            day_map = protected.setdefault(target_date, {})
            day_map[group_id] = sorted({str(user_id) for user_id in user_ids if user_id})
            await self._atomic_save()

    async def set_protected_users(self, user_ids: list):
        """兼容旧接口：写入 legacy 全局保护名单。"""
        target_date = rollpig_date_str(1)
        async with self._lock:
            protected = self.data.setdefault("protected", {})
            day_map = protected.setdefault(target_date, {})
            day_map["__all__"] = sorted({str(user_id) for user_id in user_ids if user_id})
            await self._atomic_save()

    async def clean_old_events(self, days_to_keep: int = 7):
        """清理超过 days_to_keep 天的事件记录。"""
        async with self._lock:
            today = rollpig_today()
            events = self.data.get("daily_events", {})
            dates_to_del = [
                d for d in events
                if _is_valid_date(d)
                and (today - datetime.date.fromisoformat(d)).days > days_to_keep
            ]
            for d in dates_to_del:
                del events[d]

            protected = self.data.get("protected", {})
            protection_dates_to_del = [
                d for d in list(protected.keys())
                if _is_valid_date(d)
                and (today - datetime.date.fromisoformat(d)).days > 1
            ]
            for d in protection_dates_to_del:
                del protected[d]

            if dates_to_del or protection_dates_to_del:
                await self._atomic_save()


def _safe_int(value, default: int = 0) -> int:
    """将历史 JSON 中可能出现的字符串/空值转成整数，失败时使用安全默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_utc_datetime(value: object) -> Optional[datetime.datetime]:
    """解析本地快照时间；旧数据无时间时由调用方继续按兼容记录处理。"""

    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _timestamp_not_after_cutoff(value: object, cutoff: datetime.datetime) -> bool:
    """旧记录缺少首次时间时继续保留；新记录严格排除截止点之后的活动。"""

    parsed = _parse_utc_datetime(value)
    return parsed is None or parsed <= cutoff


def _optional_nonnegative_int(value: object) -> Optional[int]:
    """读取快照中的可空非负整数；非法值返回 None，避免伪造历史结果。"""

    if isinstance(value, bool) or value is None:
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized >= 0 else None


def _is_valid_date(date_str: str) -> bool:
    try:
        datetime.date.fromisoformat(date_str)
        return True
    except ValueError:
        return False


_data_manager: PigDataManager | None = None


def get_data_manager() -> PigDataManager:
    global _data_manager
    if _data_manager is None:
        _data_manager = PigDataManager()
    return _data_manager
