from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

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
    RoastEvent,
    RoastReservation,
    RoastReservationClaimResult,
    RoastReservationPrepareResult,
    UnrolledRoastAttemptResult,
)


class RollpigStore(ABC):
    async def close(self) -> None:
        """释放后端持有的连接或句柄；本地 JSON 后端没有常驻资源，默认无需处理。"""
        return None

    @abstractmethod
    async def get_daily_roll(self, user_id: str, date_str: Optional[str] = None) -> Optional[str]:
        raise NotImplementedError

    @abstractmethod
    async def get_daily_rolls(self, date_str: Optional[str] = None) -> dict[str, str]:
        raise NotImplementedError

    @abstractmethod
    async def get_daily_roll_snapshot(
        self,
        user_id: str,
        date_str: str,
    ) -> Optional[DailyRollSnapshot]:
        raise NotImplementedError

    @abstractmethod
    async def complete_daily_roll_snapshot(
        self,
        user_id: str,
        snapshot: DailyRollSnapshot,
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def get_or_create_daily_roll(
        self,
        user_id: str,
        proposed_pig_id: str,
        date_str: Optional[str] = None,
        group_id: str = "",
        *,
        makeup: bool = False,
    ) -> DailyRollResult:
        raise NotImplementedError

    @abstractmethod
    async def get_draw_state(self, user_id: str) -> DrawState:
        raise NotImplementedError

    @abstractmethod
    async def mark_group_roll_seen(
        self,
        user_id: str,
        pig_id: str,
        group_id: str,
        date_str: Optional[str] = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_group_rolls(
        self,
        group_id: str,
        date_str: Optional[str] = None,
        cutoff_at: Optional[str] = None,
    ) -> dict[str, str]:
        raise NotImplementedError

    @abstractmethod
    async def get_user_collection(self, user_id: str) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    async def get_pig_by_date(self, user_id: str, date_str: str) -> Optional[str]:
        raise NotImplementedError

    @abstractmethod
    async def consume_roast_cooldown(
        self,
        user_id: str,
        now_ts: Optional[float] = None,
        cooldown_seconds: Optional[int] = None,
        max_charges: Optional[int] = None,
    ) -> CooldownConsumeResult:
        raise NotImplementedError

    @abstractmethod
    async def get_catalog_snapshot(self, user_id: str, days: int = 14) -> CatalogSnapshot:
        raise NotImplementedError

    @abstractmethod
    async def consume_force_usage(self, user_id: str, date_str: Optional[str] = None) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def append_roast_event(
        self,
        event: RoastEvent,
        *,
        settle_daily_feed: bool = False,
    ) -> Optional[DailyFeedResult]:
        raise NotImplementedError

    @abstractmethod
    async def query_daily_events(
        self,
        date_str: Optional[str] = None,
        group_id: Optional[str] = None,
        user_id: Optional[str] = None,
        cutoff_at: Optional[str] = None,
    ) -> DailyEventQueryResult:
        raise NotImplementedError

    async def list_daily_events(
        self,
        date_str: Optional[str] = None,
        group_id: Optional[str] = None,
        user_id: Optional[str] = None,
        cutoff_at: Optional[str] = None,
    ) -> list[dict]:
        """兼容原日报调用；需保留失败语义的后端应覆盖此方法，回顾业务读取 available。"""

        result = await self.query_daily_events(
            date_str=date_str,
            group_id=group_id,
            user_id=user_id,
            cutoff_at=cutoff_at,
        )
        return [dict(item) for item in result.items]

    @abstractmethod
    async def get_active_group_ids(self, date_str: Optional[str] = None) -> set[str]:
        raise NotImplementedError

    @abstractmethod
    async def replace_group_protections(
        self,
        group_id: str,
        user_ids: list[str],
        protect_date: Optional[str] = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def is_protected(self, group_id: str, user_id: str, date_str: Optional[str] = None) -> bool:
        raise NotImplementedError

    # ================================ 猪圈日报投递 ================================ #

    async def get_daily_report_profiles(
        self,
        *,
        group_id: str,
        date_str: str,
        cutoff_at: str,
        user_ids: tuple[str, ...],
    ) -> tuple[DailyReportProfileSnapshot, ...]:
        """返回日报排行资料；不提供批量能力的后端可返回空结果并隐藏增强榜单。"""

        return ()

    async def claim_daily_report_deliveries(
        self,
        *,
        instance_id: str,
        delivery_bots: dict[str, str],
        date_str: str,
        cutoff_at: str,
    ) -> DailyReportDeliveryClaimResult:
        """领取待投递日报；旧后端默认不参与投递，保持升级兼容。"""

        return DailyReportDeliveryClaimResult()

    async def transition_daily_report_delivery(
        self,
        claim: DailyReportDeliveryClaim,
        action: str,
        *,
        message_id: str = "",
        error: str = "",
    ) -> DailyReportDeliveryTransitionResult:
        """迁移日报投递状态；未实现日报能力的旧后端始终拒绝迁移。"""

        return DailyReportDeliveryTransitionResult(ok=False)

    @abstractmethod
    async def prune_history(self, days_to_keep: int = 14) -> None:
        raise NotImplementedError

    @abstractmethod
    async def prune_events(self, days_to_keep: int = 7) -> None:
        raise NotImplementedError

    @abstractmethod
    async def record_unrolled_roast_attempt(
        self, user_id: str, date_str: Optional[str] = None
    ) -> UnrolledRoastAttemptResult:
        raise NotImplementedError

    @abstractmethod
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
        raise NotImplementedError

    async def bind_roast_reservation_message(
        self, *, reservation_id: str, bot_id: str, group_id: str, message_id: str,
        date_str: Optional[str] = None,
    ) -> bool:
        raise NotImplementedError

    async def join_roast_reservation_by_message(
        self, *, bot_id: str, group_id: str, message_id: str,
        attacker_id: str, attacker_name: str, date_str: Optional[str] = None,
    ) -> RoastReservationPrepareResult:
        raise NotImplementedError

    @abstractmethod
    async def claim_roast_reservations(
        self,
        delivery_bot_id: str,
        date_str: Optional[str] = None,
        excluded_reservation_ids: Optional[set[str]] = None,
    ) -> RoastReservationClaimResult:
        raise NotImplementedError

    @abstractmethod
    async def has_owned_roast_reservations(
        self, delivery_bot_id: str, date_str: Optional[str] = None
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def save_roast_reservation_outcome(
        self,
        reservation: RoastReservation,
        outcome_snapshot: dict,
        *,
        settle_daily_feed: bool = False,
    ) -> Optional[RoastReservation]:
        raise NotImplementedError

    @abstractmethod
    async def mark_roast_reservation_sending(
        self,
        reservation: RoastReservation,
    ) -> Optional[RoastReservation]:
        raise NotImplementedError

    @abstractmethod
    async def complete_roast_reservation(
        self,
        reservation: RoastReservation,
        event: RoastEvent | None = None,
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def release_roast_reservation(self, reservation: RoastReservation) -> bool:
        raise NotImplementedError

    # ================================ 烤箱补货 ================================ #

    @abstractmethod
    async def mark_group_active_users(
        self,
        group_id: str,
        user_ids: list[str],
        date_str: Optional[str] = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_group_active_user_ids(
        self,
        group_id: str,
        date_str: Optional[str] = None,
        cutoff_at: Optional[str] = None,
    ) -> set[str]:
        raise NotImplementedError

    @abstractmethod
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
        raise NotImplementedError

    @abstractmethod
    async def bind_group_roast_refill_message(
        self,
        request_id: str,
        message_id: str,
    ) -> Optional[GroupRoastRefillRequest]:
        raise NotImplementedError

    @abstractmethod
    async def get_group_roast_refill(
        self,
        group_id: str,
        date_str: Optional[str] = None,
        now_ts: Optional[float] = None,
    ) -> Optional[GroupRoastRefillRequest]:
        raise NotImplementedError

    @abstractmethod
    async def fail_group_roast_refill(
        self,
        request_id: str,
        message_id: str,
        reason: str,
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
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
        raise NotImplementedError
