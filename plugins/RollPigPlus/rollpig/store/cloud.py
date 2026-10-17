from __future__ import annotations

import datetime as dt
from typing import Optional

import httpx
from ...host import logger

from ..config import plugin_config
from ..runtime import rollpig_date_str
from .base import RollpigStore
from .models import (
    CatalogSnapshot,
    CooldownConsumeResult,
    DailyFeedResult,
    DailyEventQueryResult,
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
    ROAST_REFILL_THRESHOLD_POLICY,
    ROAST_REFILL_VOTE_WEIGHT_POLICY,
    UnrolledRoastAttemptResult,
    parse_applied_daily_feed,
)


class CloudStoreError(RuntimeError):
    pass


class CloudMakeupUnsupportedError(CloudStoreError):
    pass


class CloudReservationUnsupportedError(CloudStoreError):
    """旧版 Cloud 没有预约接口时抛出，handler 可只降级预约场景。"""


class CloudRoastRefillUnsupportedError(CloudStoreError):
    """旧版 Cloud 没有补货接口时抛出，不影响其他 RollPig 功能。"""


class CloudDailyReportUnsupportedError(CloudStoreError):
    """旧版 Cloud 缺少日报扩展接口时抛出，由调用场景决定隐藏榜单或停止投递。"""


DAILY_REPORT_PROFILE_BATCH_SIZE = 2048
DAILY_REPORT_CLAIM_BATCH_SIZE = 256
DAILY_REPORT_BATCH_RETRY_SECONDS = 30
DAILY_REPORT_ERROR_MAX_LENGTH = 512


class CloudStore(RollpigStore):
    def __init__(self):
        if not plugin_config.rollpig_cloud_api_url:
            raise ValueError("启用 cloud 存储时必须配置 rollpig_cloud_api_url")
        if not plugin_config.rollpig_cloud_token:
            raise ValueError("启用 cloud 存储时必须配置 rollpig_cloud_token")

        self.base_url = plugin_config.rollpig_cloud_api_url.rstrip("/")
        # CloudStore 位于命令主链路，等待过久会直接拖住用户响应；有效范围固定为 0.5～60 秒。
        self.timeout = min(60.0, max(0.5, float(plugin_config.rollpig_cloud_timeout or 5.0)))
        self.strict_mode = bool(plugin_config.rollpig_cloud_strict_mode)
        self.headers = {
            "Authorization": f"Bearer {plugin_config.rollpig_cloud_token}",
            "Content-Type": "application/json",
        }
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self.headers,
            timeout=self.timeout,
            # CloudStore 是高频路径：复用连接池能减少 TCP/TLS 开销，同时用上限避免异常并发撑爆连接。
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        self._daily_roll_snapshot_supported: Optional[bool] = None

    async def close(self) -> None:
        """NoneBot 关闭时释放长期 HTTP client，避免 reload/退出时留下连接资源。"""
        if not self._client.is_closed:
            await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        fallback=None,
    ):
        url = f"{self.base_url}{path}"
        normalized_params = {key: value for key, value in (params or {}).items() if value is not None}
        normalized_json = {key: value for key, value in (json_body or {}).items() if value is not None}
        try:
            response = await self._client.request(
                method,
                path,
                params=normalized_params,
                json=normalized_json,
            )
            response.raise_for_status()
            if response.content:
                return response.json()
            return None
        except Exception as error:
            logger.error(f"rollpig cloud 请求失败: {method} {url} error={error}")
            if not self.strict_mode and fallback is not None:
                return fallback
            raise CloudStoreError(str(error)) from error

    async def _reservation_request(self, method: str, path: str, **kwargs):
        """预约接口使用独立兼容错误，避免旧 Cloud 拖垮正常烧烤。"""

        try:
            return await self._request(method, path, **kwargs)
        except CloudStoreError as error:
            cause = error.__cause__
            if isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code in {404, 405}:
                raise CloudReservationUnsupportedError(str(error)) from error
            raise

    async def _refill_request(self, method: str, path: str, **kwargs):
        """补货接口独立兼容降级，避免新插件连接旧 Cloud 时扩大故障面。"""

        try:
            return await self._request(method, path, **kwargs)
        except CloudStoreError as error:
            cause = error.__cause__
            if isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code in {404, 405}:
                raise CloudRoastRefillUnsupportedError(str(error)) from error
            raise

    async def _daily_report_request(self, method: str, path: str, **kwargs):
        """日报投递不允许旧 Cloud 静默降级，否则多个实例会重复发送同一个群。"""

        try:
            return await self._request(method, path, **kwargs)
        except CloudStoreError as error:
            cause = error.__cause__
            if isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code in {404, 405}:
                raise CloudDailyReportUnsupportedError(str(error)) from error
            raise

    @staticmethod
    def _parse_daily_feed(payload: dict | None) -> Optional[DailyFeedResult]:
        if not isinstance(payload, dict) or not payload.get("status") or not payload.get("user_id"):
            return None
        return DailyFeedResult(
            status=str(payload["status"]),
            user_id=str(payload["user_id"]),
            pig_id=str(payload.get("pig_id") or ""),
            previous_level=max(0, min(5, int(payload.get("previous_level") or 0))),
            new_level=max(0, min(5, int(payload.get("new_level") or 0))),
            source_type=str(payload.get("source_type") or "roast"),
            source_id=str(payload.get("source_id") or ""),
            created_at=str(payload.get("created_at") or ""),
        )

    @staticmethod
    def _parse_reservation(payload: dict | None) -> Optional[RoastReservation]:
        if not isinstance(payload, dict) or not payload.get("reservation_id"):
            return None
        participants = tuple(
            RoastReservationParticipant(
                user_id=str(item.get("user_id") or ""),
                display_name=str(item.get("display_name") or ""),
                pig_id=str(item.get("pig_id") or ""),
            )
            for item in payload.get("participants", [])
            if isinstance(item, dict) and item.get("user_id")
        )
        return RoastReservation(
            reservation_id=str(payload["reservation_id"]),
            date_str=str(payload.get("date_str") or ""),
            group_id=str(payload.get("group_id") or ""),
            target_id=str(payload.get("target_id") or ""),
            target_name=str(payload.get("target_name") or ""),
            owner_id=str(payload.get("owner_id") or ""),
            owner_name=str(payload.get("owner_name") or ""),
            owner_pig_id=str(payload.get("owner_pig_id") or ""),
            participants=participants,
            delivery_bot_id=str(payload.get("delivery_bot_id") or ""),
            force_mode=payload.get("force_mode"),
            status=str(payload.get("status") or "pending"),
            target_pig_id=str(payload.get("target_pig_id") or ""),
            outcome_snapshot=(dict(payload["outcome_snapshot"]) if isinstance(payload.get("outcome_snapshot"), dict) else None),
            daily_feed_results=tuple(
                result
                for item in (payload.get("daily_feed_results") or [])
                if (result := CloudStore._parse_daily_feed(item)) is not None
            ),
            claim_token=str(payload.get("claim_token") or ""),
        )

    @staticmethod
    def _parse_refill(payload: dict | None) -> Optional[GroupRoastRefillRequest]:
        if not isinstance(payload, dict) or not payload.get("request_id"):
            return None
        return GroupRoastRefillRequest(
            request_id=str(payload["request_id"]),
            date_str=str(payload.get("date_str") or ""),
            group_id=str(payload.get("group_id") or ""),
            initiator_id=str(payload.get("initiator_id") or ""),
            initiator_name=str(payload.get("initiator_name") or ""),
            delivery_bot_id=str(payload.get("delivery_bot_id") or ""),
            message_id=str(payload.get("message_id") or ""),
            active_count_snapshot=int(payload.get("active_count_snapshot") or 0),
            required_ratio=int(payload.get("required_ratio") or 25),
            required_votes=int(payload.get("required_votes") or 2),
            success_count_before=int(payload.get("success_count_before") or 0),
            status=str(payload.get("status") or "voting"),
            created_at=str(payload.get("created_at") or ""),
            expires_at=str(payload.get("expires_at") or ""),
            completed_at=str(payload.get("completed_at") or ""),
            benefited_user_ids=tuple(sorted({
                str(user_id) for user_id in payload.get("benefited_user_ids", []) if user_id
            })),
            failure_reason=str(payload.get("failure_reason") or ""),
        )

    @staticmethod
    def _parse_daily_roll_snapshot(
        payload: dict | None,
        *,
        date_str: str,
        user_id: Optional[str] = None,
        allow_pending_completion: bool = False,
    ) -> Optional[DailyRollSnapshot]:
        """解析 Cloud 快照；只有当前抽取流程可以读取待补全的成长结果。"""

        if not isinstance(payload, dict) or not payload.get("pig_id"):
            return None
        pig_id = str(payload["pig_id"])
        # 加餐有独立结算记录，不依赖可选的外观快照补全成功。
        daily_feed = parse_applied_daily_feed(
            payload.get("daily_feed_result"), pig_id=pig_id, user_id=user_id,
        )
        identity_snapshot = DailyRollSnapshot(
            date_str=date_str, pig_id=pig_id, daily_feed_result=daily_feed,
            is_makeup=payload.get("is_makeup") is True,
        )
        outcome = payload.get("outcome_snapshot")
        if not isinstance(outcome, dict):
            return identity_snapshot

        snapshot_available = bool(outcome.get("snapshot_available"))
        if not snapshot_available:
            # Cloud 会先冻结成长结果，再由抽取客户端补写资源与外观。历史读取
            # 不能使用半成品；只有 get-or-create 可拿成长数据继续完成 PUT。
            pending_values = (
                payload.get("is_new_pig"),
                payload.get("previous_copies"),
                payload.get("copies"),
                outcome.get("collection_size_after_roll"),
            )
            if not allow_pending_completion or any(value is None for value in pending_values):
                return identity_snapshot

        raw_levels = outcome.get("unlocked_variant_levels", [])
        levels = tuple(sorted({
            int(level)
            for level in raw_levels
            if not isinstance(level, bool) and isinstance(level, int) and 1 <= level <= 5
        })) if isinstance(raw_levels, list) else ()
        raw_fields = outcome.get("unlocked_variant_fields", [])
        fields = frozenset(
            str(field)
            for field in raw_fields
            if str(field) in {"image", "description", "analysis"}
        ) if isinstance(raw_fields, list) else frozenset()
        return DailyRollSnapshot(
            date_str=date_str,
            pig_id=pig_id,
            is_makeup=payload.get("is_makeup") is True,
            is_new_pig=bool(payload.get("is_new_pig")),
            previous_copies=max(0, int(payload.get("previous_copies") or 0)),
            copies_after_roll=max(0, int(payload.get("copies") or 0)),
            previous_expert_level=(
                max(0, min(5, int(payload["previous_expert_level"])))
                if payload.get("previous_expert_level") is not None
                else None
            ),
            expert_level_after_roll=(
                max(0, min(5, int(payload["expert_level"])))
                if payload.get("expert_level") is not None
                else None
            ),
            daily_feed_result=daily_feed,
            collection_size_after_roll=max(0, int(outcome.get("collection_size_after_roll") or 0)),
            resource_version=(str(outcome.get("resource_version") or "") if snapshot_available else ""),
            resolved_variant_level=(
                max(0, min(5, int(outcome.get("resolved_variant_level") or 0)))
                if snapshot_available
                else None
            ),
            resolved_image_name=(
                str(outcome.get("resolved_image_name") or "")
                if snapshot_available
                else ""
            ),
            unlocked_variant_levels=levels if snapshot_available else (),
            unlocked_variant_fields=fields if snapshot_available else frozenset(),
        )

    async def get_daily_roll(self, user_id: str, date_str: Optional[str] = None) -> Optional[str]:
        payload = await self._request(
            "GET",
            "/v1/daily-rolls/by-date",
            params={"user_id": user_id, "date_str": date_str or rollpig_date_str()},
            fallback={"pig_id": None},
        )
        return payload.get("pig_id") if payload else None

    async def get_daily_rolls(self, date_str: Optional[str] = None) -> dict[str, str]:
        payload = await self._request(
            "GET",
            "/v1/daily-rolls/all",
            params={"date_str": date_str or rollpig_date_str()},
            fallback={"items": []},
        )
        items = payload.get("items", []) if payload else []
        return {
            str(item.get("user_id")): str(item.get("pig_id"))
            for item in items
            if item.get("user_id") and item.get("pig_id")
        }

    async def get_daily_roll_snapshot(
        self,
        user_id: str,
        date_str: str,
    ) -> Optional[DailyRollSnapshot]:
        payload = await self._request(
            "GET",
            "/v1/daily-rolls/by-date",
            params={"user_id": user_id, "date_str": date_str},
            fallback={"pig_id": None},
        )
        return self._parse_daily_roll_snapshot(payload, date_str=date_str, user_id=user_id)

    async def complete_daily_roll_snapshot(
        self,
        user_id: str,
        snapshot: DailyRollSnapshot,
    ) -> bool:
        if getattr(self, "_daily_roll_snapshot_supported", None) is False:
            return False
        try:
            await self._request(
                "PUT",
                "/v1/daily-rolls/snapshot",
                json_body={
                    "user_id": user_id,
                    "date_str": snapshot.date_str,
                    "pig_id": snapshot.pig_id,
                    "resource_version": snapshot.resource_version,
                    "resolved_variant_level": snapshot.resolved_variant_level or 0,
                    "resolved_image_name": snapshot.resolved_image_name,
                    "unlocked_variant_levels": list(snapshot.unlocked_variant_levels),
                    "unlocked_variant_fields": sorted(snapshot.unlocked_variant_fields),
                },
            )
        except CloudStoreError as error:
            cause = error.__cause__
            if isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code in {404, 405}:
                self._daily_roll_snapshot_supported = False
                logger.info("当前 rollpig cloud 不支持每日抽取快照补全，已按旧版兼容模式运行。")
                return False
            raise
        self._daily_roll_snapshot_supported = True
        return True

    async def get_or_create_daily_roll(
        self,
        user_id: str,
        proposed_pig_id: str,
        date_str: Optional[str] = None,
        group_id: str = "",
        *,
        makeup: bool = False,
    ) -> DailyRollResult:
        target_date = date_str or rollpig_date_str()
        try:
            payload = await self._request(
                "POST",
                "/v1/daily-rolls/makeup" if makeup else "/v1/daily-rolls/get-or-create",
                json_body={
                    "user_id": user_id,
                    "proposed_pig_id": proposed_pig_id,
                    "date_str": target_date,
                    "group_id": "" if makeup else group_id,
                },
            )
        except CloudStoreError as error:
            cause = error.__cause__
            if makeup and isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code in {404, 405}:
                raise CloudMakeupUnsupportedError("当前 Cloud 尚未支持补签，请联系维护者升级。") from error
            raise
        return DailyRollResult(
            pig_id=str(payload["pig_id"]),
            created=bool(payload.get("created")),
            is_new_pig=bool(payload.get("is_new_pig")),
            previous_copies=int(payload.get("previous_copies") or 0),
            copies=int(payload.get("copies") or 0),
            previous_expert_level=(
                max(0, min(5, int(payload["previous_expert_level"])))
                if payload.get("previous_expert_level") is not None
                else None
            ),
            expert_level=(
                max(0, min(5, int(payload["expert_level"])))
                if payload.get("expert_level") is not None
                else None
            ),
            previous_duplicate_streak=int(payload.get("previous_duplicate_streak") or 0),
            duplicate_streak=int(payload.get("duplicate_streak") or 0),
            snapshot=self._parse_daily_roll_snapshot(
                payload,
                date_str=target_date,
                user_id=user_id,
                allow_pending_completion=True,
            ),
        )

    async def get_draw_state(self, user_id: str) -> DrawState:
        payload = await self._request(
            "GET",
            "/v1/draw-state",
            params={"user_id": user_id},
            fallback={"pig_ids": [], "progress": {}, "duplicate_streak": 0},
        )
        progress_payload = payload.get("progress", {}) if payload else {}
        progress: dict[str, PigProgress] = {}
        if isinstance(progress_payload, dict):
            for pig_id, item in progress_payload.items():
                if not isinstance(item, dict):
                    continue
                progress[str(pig_id)] = PigProgress(
                    copies=int(item.get("copies") or 0),
                    growth_bonus=max(0, int(item.get("growth_bonus") or 0)),
                    first_obtained_at=item.get("first_obtained_at"),
                )
        pig_ids = [str(item) for item in payload.get("pig_ids", [])] if payload else []
        return DrawState(
            pig_ids=pig_ids,
            progress=progress,
            duplicate_streak=int(payload.get("duplicate_streak") or 0) if payload else 0,
        )

    async def mark_group_roll_seen(
        self,
        user_id: str,
        pig_id: str,
        group_id: str,
        date_str: Optional[str] = None,
    ) -> None:
        await self._request(
            "POST",
            "/v1/group-rolls/mark-seen",
            json_body={
                "group_id": group_id,
                "user_id": user_id,
                "pig_id": pig_id,
                "date_str": date_str or rollpig_date_str(),
            },
        )

    async def get_group_rolls(
        self,
        group_id: str,
        date_str: Optional[str] = None,
        cutoff_at: Optional[str] = None,
    ) -> dict[str, str]:
        payload = await self._request(
            "GET",
            "/v1/group-rolls",
            params={
                "group_id": group_id,
                "date_str": date_str or rollpig_date_str(),
                "cutoff_at": cutoff_at,
            },
            fallback={"items": []},
        )
        items = payload.get("items", []) if payload else []
        return {
            str(item.get("user_id")): str(item.get("pig_id"))
            for item in items
            if item.get("user_id") and item.get("pig_id")
        }

    async def get_user_collection(self, user_id: str) -> list[str]:
        payload = await self._request(
            "GET",
            "/v1/collections",
            params={"user_id": user_id},
            fallback={"pig_ids": []},
        )
        return [str(item) for item in payload.get("pig_ids", [])] if payload else []

    async def get_pig_by_date(self, user_id: str, date_str: str) -> Optional[str]:
        payload = await self._request(
            "GET",
            "/v1/daily-rolls/by-date",
            params={"user_id": user_id, "date_str": date_str},
            fallback={"pig_id": None},
        )
        return payload.get("pig_id") if payload else None

    async def consume_roast_cooldown(
        self,
        user_id: str,
        now_ts: Optional[float] = None,
        cooldown_seconds: Optional[int] = None,
        max_charges: Optional[int] = None,
    ) -> CooldownConsumeResult:
        payload = await self._request(
            "POST",
            "/v1/cooldowns/consume-roast",
            json_body={
                "user_id": user_id,
                "now_ts": now_ts,
                "cooldown_seconds": cooldown_seconds,
                "max_charges": max_charges,
            },
        )
        return CooldownConsumeResult(
            allowed=bool(payload.get("allowed")),
            remaining_seconds=int(payload.get("remaining_seconds", 0)),
            charges_left=int(payload.get("charges_left", 0)),
            max_charges=int(payload.get("max_charges", max_charges or 1)),
            next_recover_seconds=int(payload.get("next_recover_seconds", payload.get("remaining_seconds", 0))),
        )

    async def get_catalog_snapshot(self, user_id: str, days: int = 14) -> CatalogSnapshot:
        payload = await self._request(
            "GET",
            "/v1/catalog-snapshot",
            params={"user_id": user_id, "days": days},
            fallback={"pig_ids": [], "progress": {}, "duplicate_streak": 0, "recent_rolls": [], "roasted_7d": 0},
        )
        progress_payload = payload.get("progress", {})
        progress: dict[str, PigProgress] = {}
        if isinstance(progress_payload, dict):
            for pig_id, item in progress_payload.items():
                if not isinstance(item, dict):
                    continue
                progress[str(pig_id)] = PigProgress(
                    copies=int(item.get("copies") or 0),
                    growth_bonus=max(0, int(item.get("growth_bonus") or 0)),
                    first_obtained_at=item.get("first_obtained_at"),
                )

        recent_rolls: dict[str, str] = {}
        for item in payload.get("recent_rolls", []) or []:
            if not isinstance(item, dict):
                continue
            date_str = str(item.get("date_str") or "")
            pig_id = str(item.get("pig_id") or "")
            if date_str and pig_id:
                recent_rolls[date_str] = pig_id

        return CatalogSnapshot(
            draw_state=DrawState(
                pig_ids=[str(item) for item in payload.get("pig_ids", []) or []],
                progress=progress,
                duplicate_streak=int(payload.get("duplicate_streak") or 0),
            ),
            recent_rolls=recent_rolls,
            roasted_7d=int(payload.get("roasted_7d") or payload.get("roast_events_7d") or 0),
        )

    async def consume_force_usage(self, user_id: str, date_str: Optional[str] = None) -> bool:
        payload = await self._request(
            "POST",
            "/v1/cooldowns/consume-force",
            json_body={"user_id": user_id, "date_str": date_str or rollpig_date_str()},
        )
        return bool(payload.get("allowed"))

    # ================================ 事件与预约完成 ================================ #

    @staticmethod
    def _roast_event_payload(event: RoastEvent, *, date_str: str) -> dict:
        """生成 Cloud 事件负载；预约完成可显式沿用预约所属业务日期。"""

        return {
            "event_type": event.event_type,
            "attacker_id": event.attacker_id,
            "target_id": event.target_id,
            "attacker_name": event.attacker_name,
            "target_name": event.target_name,
            "food": event.food,
            "group_id": event.group_id,
            "date_str": date_str,
            "reservation_id": event.reservation_id,
            "participant_ids": list(event.participant_ids),
            "participant_names": list(event.participant_names),
            "participant_count": event.participant_count,
            "backfire_victim_id": event.backfire_victim_id,
            "backfire_victim_name": event.backfire_victim_name,
            "special_reason": event.special_reason,
        }

    async def _append_roast_event(
        self,
        event: RoastEvent,
        *,
        date_str: str,
        settle_daily_feed: bool = False,
    ) -> Optional[DailyFeedResult]:
        payload = await self._request(
            "POST",
            "/v1/events",
            json_body={
                **self._roast_event_payload(event, date_str=date_str),
                "settle_daily_feed": settle_daily_feed,
                "source_id": event.event_id,
            },
        )
        return self._parse_daily_feed(
            payload.get("daily_feed_result") if isinstance(payload, dict) else None
        )

    async def append_roast_event(
        self,
        event: RoastEvent,
        *,
        settle_daily_feed: bool = False,
    ) -> Optional[DailyFeedResult]:
        return await self._append_roast_event(
            event,
            date_str=rollpig_date_str(),
            settle_daily_feed=settle_daily_feed,
        )

    async def query_daily_events(
        self,
        date_str: Optional[str] = None,
        group_id: Optional[str] = None,
        user_id: Optional[str] = None,
        cutoff_at: Optional[str] = None,
    ) -> DailyEventQueryResult:
        try:
            payload = await self._request(
                "GET",
                "/v1/events",
                params={
                    "date_str": date_str or rollpig_date_str(),
                    "group_id": group_id,
                    "user_id": user_id,
                    "cutoff_at": cutoff_at,
                },
            )
        except CloudStoreError:
            return DailyEventQueryResult(available=False)

        raw_items = payload.get("items", []) if isinstance(payload, dict) else []
        items = tuple(dict(item) for item in raw_items if isinstance(item, dict))
        return DailyEventQueryResult(items=items, available=True)

    async def list_daily_events(
        self,
        date_str: Optional[str] = None,
        group_id: Optional[str] = None,
        user_id: Optional[str] = None,
        cutoff_at: Optional[str] = None,
    ) -> list[dict]:
        """保留日报旧语义：严格模式查询失败必须抛错，避免误写空保护名单。"""

        payload = await self._request(
            "GET",
            "/v1/events",
            params={
                "date_str": date_str or rollpig_date_str(),
                "group_id": group_id,
                "user_id": user_id,
                "cutoff_at": cutoff_at,
            },
            # 非严格模式继续沿用旧版安全读降级；严格模式会忽略 fallback 并抛错，
            # 由日报任务跳过当前群，不能把查询失败当成真实的零事件。
            fallback={"items": []},
        )
        raw_items = payload.get("items", []) if isinstance(payload, dict) else []
        return [dict(item) for item in raw_items if isinstance(item, dict)]

    async def get_active_group_ids(self, date_str: Optional[str] = None) -> set[str]:
        payload = await self._request(
            "GET",
            "/v1/groups/active",
            params={"date_str": date_str or rollpig_date_str()},
            fallback={"group_ids": []},
        )
        return {str(group_id) for group_id in payload.get("group_ids", [])} if payload else set()

    async def replace_group_protections(
        self,
        group_id: str,
        user_ids: list[str],
        protect_date: Optional[str] = None,
    ) -> None:
        await self._request(
            "POST",
            "/v1/protections/replace-group",
            json_body={
                "group_id": group_id,
                "user_ids": user_ids,
                "protect_date": protect_date or rollpig_date_str(1),
            },
        )

    async def is_protected(self, group_id: str, user_id: str, date_str: Optional[str] = None) -> bool:
        payload = await self._request(
            "GET",
            "/v1/protections/check",
            params={
                "group_id": group_id,
                "user_id": user_id,
                "protect_date": date_str or rollpig_date_str(),
            },
            fallback={"protected": False},
        )
        return bool(payload.get("protected")) if payload else False

    # ================================ 猪圈日报投递 ================================ #

    async def get_daily_report_profiles(
        self,
        *,
        group_id: str,
        date_str: str,
        cutoff_at: str,
        user_ids: tuple[str, ...],
    ) -> tuple[DailyReportProfileSnapshot, ...]:
        """批量读取整群候选用户的 EX 与图鉴资料，禁止退化为逐用户请求。"""

        items: list[dict] = []
        for index in range(0, len(user_ids), DAILY_REPORT_PROFILE_BATCH_SIZE):
            payload = await self._daily_report_request(
                "POST",
                "/v1/daily-reports/profiles",
                json_body={
                    "group_id": group_id,
                    "date_str": date_str,
                    "cutoff_at": cutoff_at,
                    "user_ids": list(
                        user_ids[index : index + DAILY_REPORT_PROFILE_BATCH_SIZE]
                    ),
                },
            )
            items.extend(
                item
                for item in (payload or {}).get("items", [])
                if isinstance(item, dict)
            )
        return tuple(
            DailyReportProfileSnapshot(
                user_id=str(item.get("user_id") or ""),
                daily_pig_id=str(item.get("daily_pig_id") or ""),
                daily_ex_level=(
                    int(item["daily_ex_level"])
                    if item.get("daily_ex_level") is not None
                    else None
                ),
                daily_achieved_at=str(item.get("daily_achieved_at") or ""),
                catalog_count=(
                    int(item["catalog_count"])
                    if item.get("catalog_count") is not None
                    else None
                ),
                catalog_achieved_at=str(item.get("catalog_achieved_at") or ""),
                recent_pig_id=str(item.get("recent_pig_id") or ""),
                recent_ex_level=(
                    int(item["recent_ex_level"])
                    if item.get("recent_ex_level") is not None
                    else None
                ),
            )
            for item in items
            if item.get("user_id")
        )

    async def claim_daily_report_deliveries(
        self,
        *,
        instance_id: str,
        delivery_bots: dict[str, str],
        date_str: str,
        cutoff_at: str,
    ) -> DailyReportDeliveryClaimResult:
        candidate_items = sorted(delivery_bots.items())
        payload_items: list[dict] = []
        next_claim_times: list[str] = []
        completed_candidates = 0
        for index in range(0, len(candidate_items), DAILY_REPORT_CLAIM_BATCH_SIZE):
            batch_items = candidate_items[index : index + DAILY_REPORT_CLAIM_BATCH_SIZE]
            try:
                payload = await self._daily_report_request(
                    "POST",
                    "/v1/daily-reports/claim",
                    json_body={
                        "date_str": date_str,
                        "cutoff_at": cutoff_at,
                        "instance_id": instance_id,
                        "candidates": [
                            {
                                "group_id": group_id,
                                "delivery_bot_id": delivery_bot_id,
                            }
                            for group_id, delivery_bot_id in batch_items
                        ],
                    },
                )
            except CloudStoreError as error:
                if completed_candidates == 0:
                    raise
                # 前序批次已经在 Cloud 建立租约，必须先交给 jobs 完成投递；同时
                # 返回短重试时间，让尚未请求的群仍能在当晚窗口内继续领取。
                next_claim_times.append(
                    (
                        dt.datetime.now(dt.timezone.utc)
                        + dt.timedelta(seconds=DAILY_REPORT_BATCH_RETRY_SECONDS)
                    ).isoformat()
                )
                logger.warning(
                    "rollpig cloud 日报分批领取中断，已保留前序领取结果并等待重试: "
                    f"completed_candidates={completed_candidates} error={error}"
                )
                break
            completed_candidates += len(batch_items)
            payload_items.extend(
                item
                for item in (payload or {}).get("items", [])
                if isinstance(item, dict)
            )
            next_claim_at = str((payload or {}).get("next_claim_at") or "")
            if next_claim_at:
                next_claim_times.append(next_claim_at)
        return DailyReportDeliveryClaimResult(
            claims=tuple(
                DailyReportDeliveryClaim(
                    date_str=str(item.get("date_str") or date_str),
                    group_id=str(item.get("group_id") or ""),
                    delivery_bot_id=str(item.get("delivery_bot_id") or ""),
                    cutoff_at=str(item.get("cutoff_at") or cutoff_at),
                    claim_token=str(item.get("claim_token") or ""),
                    status=str(item.get("status") or "claimed"),
                    attempt_count=max(1, int(item.get("attempt_count") or 1)),
                )
                for item in payload_items
                if item.get("group_id")
                and item.get("claim_token")
            ),
            # 同一 Cloud 的时间格式一致，ISO 8601 字符串可直接按时间排序。
            next_claim_at=min(next_claim_times, default=""),
        )

    async def transition_daily_report_delivery(
        self,
        claim: DailyReportDeliveryClaim,
        action: str,
        *,
        message_id: str = "",
        error: str = "",
    ) -> DailyReportDeliveryTransitionResult:
        payload = await self._daily_report_request(
            "POST",
            "/v1/daily-reports/transition",
            json_body={
                "date_str": claim.date_str,
                "group_id": claim.group_id,
                "claim_token": claim.claim_token,
                "action": action,
                "message_id": message_id,
                # Cloud 的持久化字段上限为 512；在客户端边界先截断，避免状态迁移
                # 因诊断文本过长被请求校验拒绝。
                "error": str(error or "")[:DAILY_REPORT_ERROR_MAX_LENGTH],
            },
        )
        return DailyReportDeliveryTransitionResult(
            ok=bool((payload or {}).get("ok")),
            status=str((payload or {}).get("status") or ""),
            attempt_count=max(0, int((payload or {}).get("attempt_count") or 0)),
            next_attempt_at=str((payload or {}).get("next_attempt_at") or ""),
        )

    async def prune_history(self, days_to_keep: int = 14) -> None:
        return None

    async def prune_events(self, days_to_keep: int = 7) -> None:
        return None

    async def record_unrolled_roast_attempt(
        self, user_id: str, date_str: Optional[str] = None
    ) -> UnrolledRoastAttemptResult:
        payload = await self._reservation_request(
            "POST",
            "/v1/roast-reservations/unrolled-attempt",
            json_body={"user_id": user_id, "date_str": date_str or rollpig_date_str()},
        )
        return UnrolledRoastAttemptResult(str(payload["date_str"]), str(payload["user_id"]), int(payload["count"]))

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
        payload = await self._reservation_request(
            "POST",
            "/v1/roast-reservations/prepare",
            json_body={
                "attacker_id": attacker_id,
                "attacker_name": attacker_name,
                "attacker_pig_id": attacker_pig_id,
                "target_id": target_id,
                "target_name": target_name,
                "group_id": group_id,
                "delivery_bot_id": delivery_bot_id,
                "force_mode": force_mode,
                "date_str": date_str or rollpig_date_str(),
                "cooldown_seconds": cooldown_seconds,
                "max_charges": max_charges,
            },
        )
        cooldown_payload = payload.get("cooldown") if isinstance(payload, dict) else None
        cooldown = None
        if isinstance(cooldown_payload, dict):
            cooldown = CooldownConsumeResult(
                allowed=bool(cooldown_payload.get("allowed")),
                remaining_seconds=int(cooldown_payload.get("remaining_seconds") or 0),
                charges_left=int(cooldown_payload.get("charges_left") or 0),
                max_charges=int(cooldown_payload.get("max_charges") or 1),
                next_recover_seconds=int(cooldown_payload.get("next_recover_seconds") or 0),
            )
        return RoastReservationPrepareResult(
            status=str(payload.get("status") or "error"),
            reservation=self._parse_reservation(payload.get("reservation")),
            cooldown=cooldown,
            target_pig_id=str(payload.get("target_pig_id") or ""),
            protection_broken=bool(payload.get("protection_broken")),
        )

    async def bind_roast_reservation_message(self, **kwargs) -> bool:
        payload = await self._reservation_request(
            "POST", "/v1/roast-reservations/bind-message",
            json_body={**kwargs, "date_str": kwargs.get("date_str") or rollpig_date_str()},
        )
        return bool(payload.get("ok"))

    async def join_roast_reservation_by_message(self, **kwargs) -> RoastReservationPrepareResult:
        payload = await self._reservation_request(
            "POST", "/v1/roast-reservations/join-by-message",
            json_body={**kwargs, "date_str": kwargs.get("date_str") or rollpig_date_str()},
        )
        return RoastReservationPrepareResult(
            status=str(payload.get("status") or "error"),
            reservation=self._parse_reservation(payload.get("reservation")),
        )

    async def claim_roast_reservations(
        self,
        delivery_bot_id: str,
        date_str: Optional[str] = None,
        excluded_reservation_ids: Optional[set[str]] = None,
    ) -> RoastReservationClaimResult:
        payload = await self._reservation_request(
            "POST",
            "/v1/roast-reservations/claim",
            json_body={
                "delivery_bot_id": delivery_bot_id,
                "date_str": date_str or rollpig_date_str(),
                "supports_prepared": True,
                "excluded_reservation_ids": sorted(excluded_reservation_ids or ()),
            },
        )
        return RoastReservationClaimResult(
            tuple(
                reservation
                for item in payload.get("items", [])
                if (reservation := self._parse_reservation(item)) is not None
            ),
            # 旧 Cloud 没有 has_owned；若已经返回项目，至少保留本轮 Owner 状态，
            # 避免安全降级期间过早停止后续恢复。
            has_owned=bool(payload.get("has_owned", payload.get("items"))),
        )

    async def has_owned_roast_reservations(self, delivery_bot_id: str, date_str: Optional[str] = None) -> bool:
        payload = await self._reservation_request(
            "GET",
            "/v1/roast-reservations/owned",
            params={"delivery_bot_id": delivery_bot_id, "date_str": date_str or rollpig_date_str()},
        )
        return bool(payload.get("has_owned"))

    async def save_roast_reservation_outcome(
        self,
        reservation: RoastReservation,
        outcome_snapshot: dict,
        *,
        settle_daily_feed: bool = False,
    ) -> Optional[RoastReservation]:
        payload = await self._reservation_request(
            "POST",
            "/v1/roast-reservations/outcome/prepare",
            json_body={
                "reservation_id": reservation.reservation_id,
                "claim_token": reservation.claim_token,
                "outcome_snapshot": outcome_snapshot,
                "settle_daily_feed": settle_daily_feed,
            },
        )
        updated = self._parse_reservation(payload.get("reservation"))
        if updated and reservation.claim_token:
            return RoastReservation(**{**updated.__dict__, "claim_token": reservation.claim_token})
        return updated

    async def mark_roast_reservation_sending(
        self,
        reservation: RoastReservation,
    ) -> Optional[RoastReservation]:
        payload = await self._reservation_request(
            "POST",
            "/v1/roast-reservations/sending",
            json_body={
                "reservation_id": reservation.reservation_id,
                "claim_token": reservation.claim_token,
            },
        )
        updated = self._parse_reservation(payload.get("reservation"))
        if updated and reservation.claim_token:
            return RoastReservation(**{**updated.__dict__, "claim_token": reservation.claim_token})
        return updated

    async def complete_roast_reservation(
        self,
        reservation: RoastReservation,
        event: RoastEvent | None = None,
    ) -> bool:
        request_body = {
            "reservation_id": reservation.reservation_id,
            "claim_token": reservation.claim_token,
        }
        if event is not None:
            request_body["event"] = self._roast_event_payload(
                event,
                date_str=reservation.date_str,
            )
        payload = await self._reservation_request(
            "POST",
            "/v1/roast-reservations/complete",
            json_body=request_body,
        )
        if not payload.get("ok"):
            return False
        if event is not None and not payload.get("event_recorded"):
            # 旧 Cloud 会忽略新增 event 字段；保持兼容时退回旧的独立事件接口。
            await self._append_roast_event(event, date_str=reservation.date_str)
        return True

    async def release_roast_reservation(self, reservation: RoastReservation) -> bool:
        payload = await self._reservation_request(
            "POST",
            "/v1/roast-reservations/release",
            json_body={"reservation_id": reservation.reservation_id, "claim_token": reservation.claim_token},
        )
        return bool(payload.get("ok"))

    # ================================ 烤箱补货 ================================ #

    async def mark_group_active_users(
        self,
        group_id: str,
        user_ids: list[str],
        date_str: Optional[str] = None,
    ) -> None:
        await self._refill_request(
            "POST",
            "/v1/group-roast-refills/active-users/mark",
            json_body={
                "group_id": group_id,
                "user_ids": user_ids,
                "date_str": date_str or rollpig_date_str(),
            },
        )

    async def get_group_active_user_ids(
        self,
        group_id: str,
        date_str: Optional[str] = None,
        cutoff_at: Optional[str] = None,
    ) -> set[str]:
        payload = await self._refill_request(
            "GET",
            "/v1/group-roast-refills/active-users",
            params={
                "group_id": group_id,
                "date_str": date_str or rollpig_date_str(),
                "cutoff_at": cutoff_at,
            },
        )
        return {str(user_id) for user_id in payload.get("user_ids", []) if user_id}

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
        json_body = {
            "group_id": group_id,
            "initiator_id": initiator_id,
            "initiator_name": initiator_name,
            "delivery_bot_id": delivery_bot_id,
            "date_str": date_str or rollpig_date_str(),
            "now_ts": now_ts,
            # 新 Cloud 按该能力标识启用封顶门槛；旧 Cloud 会安全忽略未知字段。
            "threshold_policy": ROAST_REFILL_THRESHOLD_POLICY,
            # 新 Cloud 回显该能力后，创建文案才会声明群管理双票。
            "vote_weight_policy": ROAST_REFILL_VOTE_WEIGHT_POLICY,
        }
        if eligible_user_ids is not None:
            # 新 Cloud 会据此冻结群成员交集；旧 Cloud 默认忽略未知字段，调用方
            # 仍会校验响应快照，绝不静默接受未过滤门槛。
            json_body["eligible_user_ids"] = eligible_user_ids
        payload = await self._refill_request(
            "POST",
            "/v1/group-roast-refills/prepare",
            json_body=json_body,
        )
        return GroupRoastRefillPrepareResult(
            status=str(payload.get("status") or "error"),
            request=self._parse_refill(payload.get("request")),
            active_user_ids=tuple(sorted({
                str(user_id) for user_id in payload.get("active_user_ids", []) if user_id
            })),
            vote_weight_policy=str(payload.get("vote_weight_policy") or ""),
        )

    async def bind_group_roast_refill_message(
        self,
        request_id: str,
        message_id: str,
    ) -> Optional[GroupRoastRefillRequest]:
        payload = await self._refill_request(
            "POST",
            "/v1/group-roast-refills/bind-message",
            json_body={"request_id": request_id, "message_id": message_id},
        )
        return self._parse_refill(payload.get("request"))

    async def get_group_roast_refill(
        self,
        group_id: str,
        date_str: Optional[str] = None,
        now_ts: Optional[float] = None,
    ) -> Optional[GroupRoastRefillRequest]:
        payload = await self._refill_request(
            "GET",
            "/v1/group-roast-refills/active",
            params={
                "group_id": group_id,
                "date_str": date_str or rollpig_date_str(),
                "now_ts": now_ts,
            },
        )
        return self._parse_refill(payload.get("request"))

    async def fail_group_roast_refill(
        self,
        request_id: str,
        message_id: str,
        reason: str,
    ) -> bool:
        payload = await self._refill_request(
            "POST",
            "/v1/group-roast-refills/fail",
            json_body={"request_id": request_id, "message_id": message_id, "reason": reason},
        )
        return bool(payload.get("allowed"))

    async def complete_group_roast_refill(
        self,
        *,
        request_id: str,
        message_id: str,
        voter_ids: list[str],
        excluded_user_ids: list[str],
        manager_voter_ids: Optional[list[str]] = None,
        max_charges: int = 2,
        now_ts: Optional[float] = None,
    ) -> GroupRoastRefillCompleteResult:
        payload = await self._refill_request(
            "POST",
            "/v1/group-roast-refills/complete",
            json_body={
                "request_id": request_id,
                "message_id": message_id,
                "voter_ids": voter_ids,
                "excluded_user_ids": excluded_user_ids,
                "manager_voter_ids": manager_voter_ids or [],
                "vote_weight_policy": ROAST_REFILL_VOTE_WEIGHT_POLICY,
                "max_charges": max_charges,
                "now_ts": now_ts,
            },
        )
        valid_voter_ids = tuple(sorted({
            str(user_id) for user_id in payload.get("valid_voter_ids", []) if user_id
        }))
        return GroupRoastRefillCompleteResult(
            completed=bool(payload.get("completed")),
            status=str(payload.get("status") or "error"),
            request=self._parse_refill(payload.get("request")),
            valid_voter_ids=valid_voter_ids,
            benefited_user_ids=tuple(sorted({
                str(user_id) for user_id in payload.get("benefited_user_ids", []) if user_id
            })),
            # 旧 Cloud 不返回加权票数时保持一人一票，不影响既有补货流程。
            effective_votes=max(
                0,
                int(payload.get("effective_votes", len(valid_voter_ids)) or 0),
            ),
        )
