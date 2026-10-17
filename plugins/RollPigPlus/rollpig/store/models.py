from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


MAX_EXPERT_LEVEL = 5
ROAST_REFILL_THRESHOLD_POLICY = "capped-v1"
ROAST_REFILL_VOTE_WEIGHT_POLICY = "group-manager-double-v1"
ROAST_REFILL_THRESHOLD_STEPS = ((25, 8), (35, 12), (45, 16), (55, 20))
ROAST_REFILL_MIN_DISTINCT_VOTERS = 2
LEGACY_ROAST_REFILL_RATIOS = (25, 35, 45, 55, 65)


def expert_level_from_copies(copies: int, growth_bonus: int = 0) -> int:
    """根据真实抽取与加餐成长计算 EX Lv.；异常或旧数据统一钳制到 0～5。"""

    return min(
        max(int(copies or 0) - 1 + int(growth_bonus or 0), 0),
        MAX_EXPERT_LEVEL,
    )


def roast_refill_threshold(active_count: int, success_count: int) -> tuple[int, int]:
    """返回本轮比例与票数；第四次起固定为 55%，且每档设有人数上限。"""

    normalized_active = max(0, int(active_count or 0))
    normalized_success = max(0, int(success_count or 0))
    ratio, vote_cap = ROAST_REFILL_THRESHOLD_STEPS[
        min(normalized_success, len(ROAST_REFILL_THRESHOLD_STEPS) - 1)
    ]
    proportional_votes = (normalized_active * ratio + 99) // 100
    required_votes = max(2, min(proportional_votes, vote_cap))
    return ratio, required_votes


def legacy_roast_refill_threshold(active_count: int, success_count: int) -> tuple[int, int]:
    """计算旧 Cloud 的无人数上限门槛，供滚动升级期间校验响应。"""

    normalized_active = max(0, int(active_count or 0))
    normalized_success = max(0, int(success_count or 0))
    ratio = LEGACY_ROAST_REFILL_RATIOS[
        min(normalized_success, len(LEGACY_ROAST_REFILL_RATIOS) - 1)
    ]
    return ratio, max(2, (normalized_active * ratio + 99) // 100)


@dataclass(frozen=True)
class PigProgress:
    copies: int = 0
    growth_bonus: int = 0
    first_obtained_at: Optional[str] = None

    @property
    def expert_level(self) -> int:
        """返回当前小猪的只读 EX Lv.，避免展示层重复等级公式。"""

        return expert_level_from_copies(self.copies, self.growth_bonus)


@dataclass(frozen=True)
class DrawState:
    pig_ids: list[str]
    progress: dict[str, PigProgress]
    duplicate_streak: int = 0

    def copies_of(self, pig_id: str) -> int:
        item = self.progress.get(pig_id)
        return int(item.copies) if item else 0

    def expert_level_of(self, pig_id: str) -> int:
        """返回指定小猪的 EX Lv.；未拥有或记录缺失时为 EX0。"""

        item = self.progress.get(pig_id)
        return item.expert_level if item else 0


@dataclass(frozen=True)
class DailyRollSnapshot:
    """某日抽取真正发生时的最小历史结果；旧记录允许只保留日期与 pig_id。"""

    date_str: str
    pig_id: str
    is_new_pig: Optional[bool] = None
    previous_copies: Optional[int] = None
    copies_after_roll: Optional[int] = None
    previous_expert_level: Optional[int] = None
    expert_level_after_roll: Optional[int] = None
    daily_feed_result: Optional["DailyFeedResult"] = None
    collection_size_after_roll: Optional[int] = None
    resource_version: str = ""
    resolved_variant_level: Optional[int] = None
    resolved_image_name: str = ""
    unlocked_variant_levels: tuple[int, ...] = ()
    unlocked_variant_fields: frozenset[str] = frozenset()
    is_makeup: bool = False

    @property
    def outcome_available(self) -> bool:
        """是否保存了可用于昨日成长展示的抽取时结果。"""

        return all(
            value is not None
            for value in (
                self.is_new_pig,
                self.previous_copies,
                self.copies_after_roll,
                self.collection_size_after_roll,
            )
        )


@dataclass(frozen=True)
class DailyRollResult:
    pig_id: str
    created: bool
    is_new_pig: bool = False
    previous_copies: int = 0
    copies: int = 0
    previous_expert_level: Optional[int] = None
    expert_level: Optional[int] = None
    previous_duplicate_streak: int = 0
    duplicate_streak: int = 0
    snapshot: Optional[DailyRollSnapshot] = None

    def __iter__(self):
        # 兼容旧代码的 `pig_id, created = await get_or_create_daily_roll(...)` 写法。
        yield self.pig_id
        yield self.created


@dataclass(frozen=True)
class CooldownConsumeResult:
    allowed: bool
    remaining_seconds: int = 0
    charges_left: int = 0
    max_charges: int = 1
    next_recover_seconds: int = 0


@dataclass(frozen=True)
class CatalogSnapshot:
    draw_state: DrawState
    recent_rolls: dict[str, str]
    roasted_7d: int = 0


@dataclass(frozen=True)
class RoastEvent:
    event_type: str
    attacker_id: str
    target_id: str
    attacker_name: str = ""
    target_name: str = ""
    food: str = ""
    group_id: str = ""
    reservation_id: str = ""
    participant_ids: tuple[str, ...] = ()
    participant_names: tuple[str, ...] = ()
    participant_count: int = 0
    backfire_victim_id: str = ""
    backfire_victim_name: str = ""
    special_reason: str = ""
    event_id: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class DailyFeedResult:
    """一次用户级加餐判定；只有 status=fed 代表实际发生成长。"""

    status: str
    user_id: str
    pig_id: str = ""
    previous_level: int = 0
    new_level: int = 0
    source_type: str = "roast"
    source_id: str = ""
    created_at: str = ""


# ================================ 历史加餐事实校验 ================================ #


def parse_applied_daily_feed(
    raw: object, *, pig_id: str, user_id: Optional[str] = None,
) -> Optional[DailyFeedResult]:
    """读取独立的已生效加餐；不把失败判定或损坏等级归一化成历史事实。"""

    if not isinstance(raw, dict) or raw.get("status") != "fed":
        return None
    if not raw.get("user_id") or str(raw.get("pig_id") or "") != pig_id:
        return None
    if user_id is not None and str(raw["user_id"]) != user_id:
        return None
    previous, current = raw.get("previous_level"), raw.get("new_level")
    # JSON 整数才可信；拒绝 bool、浮点截断和越界钳制，避免凭空制造升级。
    if type(previous) is not int or type(current) is not int:
        return None
    if not 0 <= previous < current <= MAX_EXPERT_LEVEL:
        return None
    return DailyFeedResult(
        status="fed",
        user_id=str(raw["user_id"]),
        pig_id=pig_id,
        previous_level=previous,
        new_level=current,
        source_type=str(raw.get("source_type") or "roast"),
        source_id=str(raw.get("source_id") or ""),
        created_at=str(raw.get("created_at") or ""),
    )


@dataclass(frozen=True)
class DailyEventQueryResult:
    """事件查询结果；available=False 与真实的零事件严格区分。"""

    items: tuple[dict[str, Any], ...] = ()
    available: bool = True


@dataclass(frozen=True)
class DailyReportDeliveryClaim:
    """某实例从 Store 领取的一份群日报；claim_token 绑定全部状态迁移。"""

    date_str: str
    group_id: str
    delivery_bot_id: str
    cutoff_at: str
    claim_token: str
    status: str = "claimed"
    attempt_count: int = 1


@dataclass(frozen=True)
class DailyReportDeliveryClaimResult:
    """一次群日报领取响应；next_claim_at 用于等待其他实例租约或服务端退避。"""

    claims: tuple[DailyReportDeliveryClaim, ...] = ()
    next_claim_at: str = ""


@dataclass(frozen=True)
class DailyReportDeliveryTransitionResult:
    """日报状态迁移结果；失败释放时携带服务端决定的下次领取时间。"""

    ok: bool
    status: str = ""
    attempt_count: int = 0
    next_attempt_at: str = ""

    def __bool__(self) -> bool:
        """兼容既有布尔判断，同时让调用方可以读取完整重试信息。"""

        return self.ok


@dataclass(frozen=True)
class DailyReportProfileSnapshot:
    """Store 在日报固定截止点批量返回的用户排行资料。"""

    user_id: str
    daily_pig_id: str = ""
    daily_ex_level: Optional[int] = None
    daily_achieved_at: str = ""
    catalog_count: Optional[int] = None
    catalog_achieved_at: str = ""
    recent_pig_id: str = ""
    recent_ex_level: Optional[int] = None


@dataclass(frozen=True)
class RoastReservationParticipant:
    user_id: str
    display_name: str = ""
    pig_id: str = ""


@dataclass(frozen=True)
class RoastReservation:
    reservation_id: str
    date_str: str
    group_id: str
    target_id: str
    target_name: str
    owner_id: str
    owner_name: str
    owner_pig_id: str
    participants: tuple[RoastReservationParticipant, ...] = ()
    delivery_bot_id: str = ""
    force_mode: Optional[str] = None
    status: str = "pending"
    target_pig_id: str = ""
    outcome_snapshot: Optional[dict[str, Any]] = None
    daily_feed_results: tuple[DailyFeedResult, ...] = ()
    claim_token: str = ""

    @property
    def participant_count(self) -> int:
        return len(self.participants)


@dataclass(frozen=True)
class UnrolledRoastAttemptResult:
    date_str: str
    user_id: str
    count: int


@dataclass(frozen=True)
class RoastReservationPrepareResult:
    status: str
    reservation: Optional[RoastReservation] = None
    cooldown: Optional[CooldownConsumeResult] = None
    target_pig_id: str = ""
    protection_broken: bool = False


@dataclass(frozen=True)
class RoastReservationClaimResult:
    reservations: tuple[RoastReservation, ...] = ()
    has_owned: bool = False


@dataclass(frozen=True)
class GroupRoastRefillRequest:
    request_id: str
    date_str: str
    group_id: str
    initiator_id: str
    initiator_name: str
    delivery_bot_id: str
    message_id: str = ""
    active_count_snapshot: int = 0
    required_ratio: int = 25
    required_votes: int = 2
    success_count_before: int = 0
    status: str = "voting"
    created_at: str = ""
    expires_at: str = ""
    completed_at: str = ""
    benefited_user_ids: tuple[str, ...] = ()
    failure_reason: str = ""


@dataclass(frozen=True)
class GroupRoastRefillPrepareResult:
    status: str
    request: Optional[GroupRoastRefillRequest] = None
    active_user_ids: tuple[str, ...] = ()
    vote_weight_policy: str = ""


@dataclass(frozen=True)
class GroupRoastRefillCompleteResult:
    completed: bool
    status: str
    request: Optional[GroupRoastRefillRequest] = None
    valid_voter_ids: tuple[str, ...] = ()
    benefited_user_ids: tuple[str, ...] = ()
    effective_votes: int = 0
