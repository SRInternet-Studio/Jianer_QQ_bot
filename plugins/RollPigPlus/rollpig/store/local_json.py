from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional

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
    RoastEvent,
    RoastReservation,
    RoastReservationClaimResult,
    RoastReservationPrepareResult,
    UnrolledRoastAttemptResult,
)

if TYPE_CHECKING:
    from ..data_manager import PigDataManager


LOCAL_DAILY_REPORT_RETRY_SECONDS = 30
LOCAL_DAILY_REPORT_MAX_ATTEMPTS = 4


class LocalJsonStore(RollpigStore):
    def __init__(self, manager_factory: Callable[[], "PigDataManager"]):
        self._manager_factory = manager_factory

    @property
    def manager(self) -> "PigDataManager":
        return self._manager_factory()

    async def get_daily_roll(self, user_id: str, date_str: Optional[str] = None) -> Optional[str]:
        return self.manager.get_today_pig(user_id, date_str=date_str)

    async def get_daily_rolls(self, date_str: Optional[str] = None) -> dict[str, str]:
        return self.manager.get_daily_rolls(date_str)

    async def get_daily_roll_snapshot(
        self,
        user_id: str,
        date_str: str,
    ) -> Optional[DailyRollSnapshot]:
        return self.manager.get_daily_roll_snapshot(user_id, date_str)

    async def complete_daily_roll_snapshot(
        self,
        user_id: str,
        snapshot: DailyRollSnapshot,
    ) -> bool:
        return await self.manager.complete_daily_roll_snapshot(user_id, snapshot)

    async def get_or_create_daily_roll(
        self,
        user_id: str,
        proposed_pig_id: str,
        date_str: Optional[str] = None,
        group_id: str = "",
        *,
        makeup: bool = False,
    ) -> DailyRollResult:
        return await self.manager.get_or_create_today_pig(
            user_id=user_id,
            proposed_pig_id=proposed_pig_id,
            date_str=date_str,
            group_id=group_id,
            makeup=makeup,
        )

    async def get_draw_state(self, user_id: str) -> DrawState:
        return self.manager.get_draw_state(user_id)

    async def mark_group_roll_seen(
        self,
        user_id: str,
        pig_id: str,
        group_id: str,
        date_str: Optional[str] = None,
    ) -> None:
        await self.manager.mark_group_roll_seen(user_id, pig_id, group_id, date_str=date_str)

    async def get_group_rolls(
        self,
        group_id: str,
        date_str: Optional[str] = None,
        cutoff_at: Optional[str] = None,
    ) -> dict[str, str]:
        return self.manager.get_group_rolls(group_id, date_str, cutoff_at=cutoff_at)

    async def get_user_collection(self, user_id: str) -> list[str]:
        return self.manager.get_user_collection(user_id)

    async def get_pig_by_date(self, user_id: str, date_str: str) -> Optional[str]:
        return self.manager.get_pig_by_date(user_id, date_str)

    async def consume_roast_cooldown(
        self,
        user_id: str,
        now_ts: Optional[float] = None,
        cooldown_seconds: Optional[int] = None,
        max_charges: Optional[int] = None,
    ) -> CooldownConsumeResult:
        return await self.manager.consume_roast_usage(
            user_id,
            now_ts=now_ts,
            cooldown_seconds=cooldown_seconds,
            max_charges=max_charges,
        )

    async def get_catalog_snapshot(self, user_id: str, days: int = 14) -> CatalogSnapshot:
        return self.manager.get_catalog_snapshot(user_id, days=days)

    async def consume_force_usage(self, user_id: str, date_str: Optional[str] = None) -> bool:
        return await self.manager.consume_force_roast_usage(user_id, date_str=date_str)

    async def append_roast_event(
        self,
        event: RoastEvent,
        *,
        settle_daily_feed: bool = False,
    ) -> Optional[DailyFeedResult]:
        return await self.manager.log_roast_event(
            event.event_type,
            event.attacker_id,
            event.target_id,
            attacker_name=event.attacker_name,
            target_name=event.target_name,
            food=event.food,
            group_id=event.group_id,
            reservation_id=event.reservation_id,
            participant_ids=list(event.participant_ids),
            participant_names=list(event.participant_names),
            participant_count=event.participant_count,
            backfire_victim_id=event.backfire_victim_id,
            backfire_victim_name=event.backfire_victim_name,
            special_reason=event.special_reason,
            event_id=event.event_id,
            created_at=event.created_at,
            settle_daily_feed=settle_daily_feed,
        )

    async def query_daily_events(
        self,
        date_str: Optional[str] = None,
        group_id: Optional[str] = None,
        user_id: Optional[str] = None,
        cutoff_at: Optional[str] = None,
    ) -> DailyEventQueryResult:
        return DailyEventQueryResult(
            items=tuple(self.manager.get_daily_events(
                date_str=date_str,
                group_id=group_id,
                user_id=user_id,
                cutoff_at=cutoff_at,
            )),
            available=True,
        )

    async def get_active_group_ids(self, date_str: Optional[str] = None) -> set[str]:
        return self.manager.get_active_group_ids(date_str=date_str)

    async def replace_group_protections(
        self,
        group_id: str,
        user_ids: list[str],
        protect_date: Optional[str] = None,
    ) -> None:
        await self.manager.replace_group_protected_users(group_id, user_ids, protect_date=protect_date)

    async def is_protected(self, group_id: str, user_id: str, date_str: Optional[str] = None) -> bool:
        return self.manager.is_protected(group_id, user_id, date_str=date_str)

    # ================================ 猪圈日报投递 ================================ #

    async def get_daily_report_profiles(
        self,
        *,
        group_id: str,
        date_str: str,
        cutoff_at: str,
        user_ids: tuple[str, ...],
    ) -> tuple[DailyReportProfileSnapshot, ...]:
        # 本地排行资料来自用户全局成长；group_id 只决定参赛范围，由 jobs 传入 user_ids。
        return await self.manager.get_daily_report_profiles(
            date_str=date_str,
            cutoff_at=cutoff_at,
            user_ids=user_ids,
        )

    async def claim_daily_report_deliveries(
        self,
        *,
        instance_id: str,
        delivery_bots: dict[str, str],
        date_str: str,
        cutoff_at: str,
    ) -> DailyReportDeliveryClaimResult:
        # Local 仍只协调单进程，但领取状态必须先落盘，才能在安全窗口内
        # 从进程重启中恢复，并避免已经进入 sending 的日报重复发送。
        return await self.manager.claim_local_daily_report_deliveries(
            instance_id=instance_id,
            delivery_bots=delivery_bots,
            date_str=date_str,
            cutoff_at=cutoff_at,
            max_attempts=LOCAL_DAILY_REPORT_MAX_ATTEMPTS,
        )

    async def transition_daily_report_delivery(
        self,
        claim: DailyReportDeliveryClaim,
        action: str,
        *,
        message_id: str = "",
        error: str = "",
    ) -> DailyReportDeliveryTransitionResult:
        return await self.manager.transition_local_daily_report_delivery(
            claim,
            action,
            retry_seconds=LOCAL_DAILY_REPORT_RETRY_SECONDS,
            max_attempts=LOCAL_DAILY_REPORT_MAX_ATTEMPTS,
            message_id=message_id,
            error=error,
        )

    async def prune_history(self, days_to_keep: int = 14) -> None:
        await self.manager.clean_old_history(days_to_keep=days_to_keep)

    async def prune_events(self, days_to_keep: int = 7) -> None:
        await self.manager.clean_old_events(days_to_keep=days_to_keep)

    async def record_unrolled_roast_attempt(self, user_id: str, date_str: Optional[str] = None) -> UnrolledRoastAttemptResult:
        return await self.manager.record_unrolled_roast_attempt(user_id, date_str=date_str)

    async def prepare_roast_reservation(self, **kwargs) -> RoastReservationPrepareResult:
        return await self.manager.prepare_roast_reservation(**kwargs)

    async def bind_roast_reservation_message(self, **kwargs) -> bool:
        return await self.manager.bind_roast_reservation_message(**kwargs)

    async def join_roast_reservation_by_message(self, **kwargs) -> RoastReservationPrepareResult:
        return await self.manager.join_roast_reservation_by_message(**kwargs)

    async def claim_roast_reservations(
        self,
        delivery_bot_id: str,
        date_str: Optional[str] = None,
        excluded_reservation_ids: Optional[set[str]] = None,
    ) -> RoastReservationClaimResult:
        return await self.manager.claim_roast_reservations(
            delivery_bot_id,
            date_str=date_str,
            excluded_reservation_ids=excluded_reservation_ids,
        )

    async def has_owned_roast_reservations(self, delivery_bot_id: str, date_str: Optional[str] = None) -> bool:
        return self.manager.has_owned_roast_reservations(delivery_bot_id, date_str=date_str)

    async def save_roast_reservation_outcome(
        self,
        reservation: RoastReservation,
        outcome_snapshot: dict,
        *,
        settle_daily_feed: bool = False,
    ) -> Optional[RoastReservation]:
        return await self.manager.save_roast_reservation_outcome(
            reservation.reservation_id,
            reservation.claim_token,
            outcome_snapshot,
            settle_daily_feed=settle_daily_feed,
        )

    async def mark_roast_reservation_sending(
        self,
        reservation: RoastReservation,
    ) -> Optional[RoastReservation]:
        return await self.manager.mark_roast_reservation_sending(
            reservation.reservation_id,
            reservation.claim_token,
        )

    async def complete_roast_reservation(
        self,
        reservation: RoastReservation,
        event: RoastEvent | None = None,
    ) -> bool:
        return await self.manager.complete_roast_reservation(
            reservation.reservation_id,
            reservation.claim_token,
            event,
        )

    async def release_roast_reservation(self, reservation: RoastReservation) -> bool:
        return await self.manager.release_roast_reservation(
            reservation.reservation_id,
            reservation.claim_token,
        )

    # ================================ 烤箱补货 ================================ #

    async def mark_group_active_users(
        self,
        group_id: str,
        user_ids: list[str],
        date_str: Optional[str] = None,
    ) -> None:
        await self.manager.mark_group_active_users(group_id, user_ids, date_str=date_str)

    async def get_group_active_user_ids(
        self,
        group_id: str,
        date_str: Optional[str] = None,
        cutoff_at: Optional[str] = None,
    ) -> set[str]:
        return self.manager.get_group_active_user_ids(
            group_id,
            date_str=date_str,
            cutoff_at=cutoff_at,
        )

    async def prepare_group_roast_refill(self, **kwargs) -> GroupRoastRefillPrepareResult:
        return await self.manager.prepare_group_roast_refill(**kwargs)

    async def bind_group_roast_refill_message(
        self,
        request_id: str,
        message_id: str,
    ) -> Optional[GroupRoastRefillRequest]:
        return await self.manager.bind_group_roast_refill_message(request_id, message_id)

    async def get_group_roast_refill(
        self,
        group_id: str,
        date_str: Optional[str] = None,
        now_ts: Optional[float] = None,
    ) -> Optional[GroupRoastRefillRequest]:
        return await self.manager.get_group_roast_refill(group_id, date_str=date_str, now_ts=now_ts)

    async def fail_group_roast_refill(
        self,
        request_id: str,
        message_id: str,
        reason: str,
    ) -> bool:
        return await self.manager.fail_group_roast_refill(request_id, message_id, reason)

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
        return await self.manager.complete_group_roast_refill(
            request_id=request_id,
            message_id=message_id,
            voter_ids=voter_ids,
            excluded_user_ids=excluded_user_ids,
            manager_voter_ids=manager_voter_ids,
            max_charges=max_charges,
            now_ts=now_ts,
        )
