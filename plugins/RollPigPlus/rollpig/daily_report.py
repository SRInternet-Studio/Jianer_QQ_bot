from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Iterable, Literal, Mapping, Sequence


ObservationKind = Literal[
    "reservation",
    "backfire",
    "escape",
    "success",
    "human",
    "collision",
    "variety",
]
HeadlineKind = Literal[
    "normal_success",
    "normal_escape",
    "normal_backfire",
    "self_roast",
    "bot_backfire",
    "reservation_success",
    "reservation_escape",
    "reservation_backfire",
    "reservation_human",
    "reservation_food",
    "reservation_eaten",
    "reservation_sold",
]
TimelineKind = Literal[
    "mutual",
    "personal_turn",
    "reservation_followup",
    "repeat_target",
]
RankingKind = Literal["expert_level", "roast_success", "catalog"]
OverviewMetricKind = Literal[
    "roll_count",
    "ordinary_roast",
    "reservation",
    "escape",
    "backfire",
    "pig_variety",
]

KNOWN_EVENT_TYPES = frozenset(
    {"success", "escape", "backfire", "bot_backfire", "self_roast", "reserved_special"}
)
OBSERVATION_PRIORITY: tuple[ObservationKind, ...] = (
    "reservation",
    "backfire",
    "escape",
    "success",
    "human",
    "collision",
    "variety",
)
TIMELINE_PRIORITY: tuple[TimelineKind, ...] = (
    "mutual",
    "personal_turn",
    "reservation_followup",
    "repeat_target",
)


# ================================ 日报输入与输出模型 ================================ #


@dataclass(frozen=True)
class DailyUserReportProfile:
    """聚合层提供的用户排行资料；缺失字段允许旧后端降级隐藏对应排行。"""

    user_id: str
    display_name: str = ""
    daily_pig_id: str = ""
    daily_pig_name: str = ""
    daily_ex_level: int | None = None
    daily_image_name: str = ""
    daily_achieved_at: str = ""
    catalog_count: int | None = None
    catalog_achieved_at: str = ""
    recent_pig_id: str = ""
    recent_pig_name: str = ""
    recent_image_name: str = ""


@dataclass(frozen=True)
class ProtectionReportItem:
    """已经写入存储并实际生效的次日保护，不在日报层重新推导。"""

    user_id: str
    display_name: str = ""
    scope: str = "本群免烤"
    expires_at: str = ""


@dataclass(frozen=True)
class NormalizedDailyEvent:
    """跨本地与 Cloud 后端统一后的日报事件。"""

    event_id: str
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
    created_at: str = ""

    @property
    def is_reservation(self) -> bool:
        return bool(self.reservation_id)

    @property
    def result_kind(self) -> str:
        if self.event_type in {"backfire", "bot_backfire"}:
            return "backfire"
        if self.event_type == "reserved_special":
            return "special"
        return self.event_type


@dataclass(frozen=True)
class DailyOverview:
    roll_count: int = 0
    ordinary_roast_count: int = 0
    reservation_count: int = 0
    escape_count: int = 0
    backfire_count: int = 0
    pig_variety_count: int = 0
    human_count: int = 0
    ordinary_success_count: int = 0
    top_pig_id: str = ""
    top_pig_count: int = 0


@dataclass(frozen=True)
class OverviewMetric:
    kind: OverviewMetricKind
    label: str
    value: int
    unit: str


@dataclass(frozen=True)
class ObservationSelection:
    kind: ObservationKind
    total: int
    matched: int
    dominant_result: str = ""
    success_count: int = 0
    escape_count: int = 0
    backfire_count: int = 0


@dataclass(frozen=True)
class HeadlineSelection:
    kind: HeadlineKind
    score: int
    repeated_pair_events: int
    event: NormalizedDailyEvent


@dataclass(frozen=True)
class TimelineSelection:
    kind: TimelineKind
    events: tuple[NormalizedDailyEvent, ...]
    anchor_event: NormalizedDailyEvent | None = None


@dataclass(frozen=True)
class RankingEntry:
    rank: int
    user_id: str
    display_name: str
    score: int
    pig_id: str = ""
    pig_name: str = ""
    image_name: str = ""
    achieved_at: str = ""
    uses_fallback_pig_stamp: bool = False


@dataclass(frozen=True)
class DailyRanking:
    kind: RankingKind
    entries: tuple[RankingEntry, ...]


@dataclass(frozen=True)
class DailyReport:
    """日报业务层最终结果；不包含颜色、坐标、字体或图片绘制逻辑。"""

    date_str: str
    group_id: str
    has_activity: bool
    participant_ids: tuple[str, ...]
    display_names: Mapping[str, str]
    events: tuple[NormalizedDailyEvent, ...]
    overview: DailyOverview
    overview_metrics: tuple[OverviewMetric, ...]
    observation: ObservationSelection | None
    headline: HeadlineSelection | None
    timeline: TimelineSelection | None
    rankings: tuple[DailyRanking, ...]
    protections: tuple[ProtectionReportItem, ...]


@dataclass(frozen=True)
class _TimelineCandidate:
    kind: TimelineKind
    identity: str
    events: tuple[NormalizedDailyEvent, ...]
    anchor_event: NormalizedDailyEvent | None = None


# ================================ 字段归一化与稳定排序 ================================ #


def _text(value: object) -> str:
    return str(value or "").strip()


def _participant_fields(
    raw_ids: object,
    raw_names: object,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """按原始下标绑定预约成员昵称，避免空昵称导致后续成员整体错位。"""

    if not isinstance(raw_ids, (list, tuple)):
        return (), ()
    names = raw_names if isinstance(raw_names, (list, tuple)) else ()
    participant_ids: list[str] = []
    participant_names: list[str] = []
    for index, raw_user_id in enumerate(raw_ids):
        user_id = _text(raw_user_id)
        if not user_id:
            continue
        participant_ids.append(user_id)
        participant_names.append(_text(names[index]) if index < len(names) else "")
    return tuple(participant_ids), tuple(participant_names)


def _normalize_group_rolls(group_rolls: Mapping[str, str]) -> dict[str, str]:
    """隔离空用户和空猪 ID；异常记录不能进入人数、种类或排行。"""

    normalized: dict[str, str] = {}
    for raw_user_id, raw_pig_id in group_rolls.items():
        user_id = _text(raw_user_id)
        pig_id = _text(raw_pig_id)
        if user_id and pig_id:
            normalized[user_id] = pig_id
    return normalized


def _parse_timestamp(value: str) -> dt.datetime | None:
    """把跨后端 ISO 时间归一化为 UTC；无效旧值由调用方决定兼容策略。"""

    normalized = _text(value)
    if not normalized:
        return None
    try:
        parsed = dt.datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _timestamp_key(value: str) -> tuple[int, str]:
    """ISO 时间可直接按 UTC 归一化；非法旧值排到有效时间之后。"""

    parsed = _parse_timestamp(value)
    if parsed is None:
        return (1, _text(value))
    return (0, parsed.isoformat())


def _event_sort_key(event: NormalizedDailyEvent) -> tuple[tuple[int, str], str]:
    return (_timestamp_key(event.created_at), event.event_id)


def normalize_daily_events(
    raw_events: Sequence[Mapping[str, object]],
    *,
    group_id: str = "",
    cutoff_at: str = "",
) -> tuple[NormalizedDailyEvent, ...]:
    """清洗日报事件并隔离未知类型；有效时间晚于固定截止点的记录不会进入快照。"""

    normalized: list[NormalizedDailyEvent] = []
    expected_group_id = _text(group_id)
    parsed_cutoff = _parse_timestamp(cutoff_at)
    for index, raw in enumerate(raw_events):
        event_type = _text(raw.get("type"))
        if event_type not in KNOWN_EVENT_TYPES:
            continue
        event_group_id = _text(raw.get("group_id"))
        if expected_group_id and event_group_id and event_group_id != expected_group_id:
            continue
        participant_ids, participant_names = _participant_fields(
            raw.get("participant_ids"),
            raw.get("participant_names"),
        )
        try:
            raw_participant_count = max(0, int(raw.get("participant_count") or 0))
        except (TypeError, ValueError):
            raw_participant_count = 0
        event = NormalizedDailyEvent(
            event_id=_text(raw.get("event_id")) or f"legacy:{index:06d}",
            event_type=event_type,
            attacker_id=_text(raw.get("attacker")),
            target_id=_text(raw.get("target")),
            attacker_name=_text(raw.get("attacker_name")),
            target_name=_text(raw.get("target_name")),
            food=_text(raw.get("food")),
            group_id=event_group_id or expected_group_id,
            reservation_id=_text(raw.get("reservation_id")),
            participant_ids=participant_ids,
            participant_names=participant_names,
            participant_count=max(raw_participant_count, len(participant_ids)),
            backfire_victim_id=_text(raw.get("backfire_victim_id")),
            backfire_victim_name=_text(raw.get("backfire_victim_name")),
            special_reason=_text(raw.get("special_reason")),
            created_at=_text(raw.get("created_at")),
        )
        event_time = _parse_timestamp(event.created_at)
        # 无时间的旧记录继续保留；当前版本写入的有效时间必须严格服从日报截止点。
        if parsed_cutoff is not None and event_time is not None and event_time > parsed_cutoff:
            continue
        normalized.append(event)
    return tuple(sorted(normalized, key=_event_sort_key))


def _collect_display_names(
    events: Sequence[NormalizedDailyEvent],
    profiles: Mapping[str, DailyUserReportProfile],
) -> dict[str, str]:
    names = {
        user_id: profile.display_name
        for user_id, profile in profiles.items()
        if user_id and profile.display_name
    }
    # 群成员接口返回的是当前名片，优先级高于事件产生时保存的历史姓名；
    # 没有实时姓名的用户仍按事件顺序更新，最终保留当天最新一次快照。
    live_name_user_ids = frozenset(names)
    for event in events:
        for user_id, display_name in (
            (event.attacker_id, event.attacker_name),
            (event.target_id, event.target_name),
            (event.backfire_victim_id, event.backfire_victim_name),
        ):
            if user_id and display_name and user_id not in live_name_user_ids:
                names[user_id] = display_name
        for user_id, display_name in zip(event.participant_ids, event.participant_names):
            if user_id and display_name and user_id not in live_name_user_ids:
                names[user_id] = display_name
    return names


def _collect_participants(
    group_rolls: Mapping[str, str],
    events: Sequence[NormalizedDailyEvent],
    active_user_ids: Iterable[str],
    bot_user_ids: frozenset[str],
) -> tuple[str, ...]:
    users = {_text(user_id) for user_id in active_user_ids}
    users.update(_text(user_id) for user_id in group_rolls)
    for event in events:
        users.add(event.attacker_id)
        if event.event_type != "bot_backfire":
            users.add(event.target_id)
        users.update(event.participant_ids)
        users.add(event.backfire_victim_id)
    users.discard("")
    users.difference_update(bot_user_ids)
    return tuple(sorted(users))


# ================================ 今日猪圈与猪圈见闻 ================================ #


def build_daily_overview(
    group_rolls: Mapping[str, str],
    events: Sequence[NormalizedDailyEvent],
    *,
    human_pig_ids: frozenset[str] = frozenset({"human"}),
) -> DailyOverview:
    """按实施方案的固定口径生成二至三列速览所需原始统计。"""

    normalized_group_rolls = _normalize_group_rolls(group_rolls)
    # 自烤属于“今日烤猪”的独立结果，不是用户发起的普通烤群友结算。
    ordinary_events = [
        event
        for event in events
        if not event.is_reservation and event.event_type != "self_roast"
    ]
    reservation_events = [event for event in events if event.is_reservation]
    normalized_rolls = list(normalized_group_rolls.values())
    pig_counts = Counter(normalized_rolls)
    top_pig_id, top_pig_count = pig_counts.most_common(1)[0] if pig_counts else ("", 0)
    return DailyOverview(
        roll_count=len(normalized_group_rolls),
        ordinary_roast_count=len(ordinary_events),
        reservation_count=len({event.reservation_id or event.event_id for event in reservation_events}),
        escape_count=sum(event.event_type == "escape" for event in events),
        backfire_count=sum(event.event_type in {"backfire", "bot_backfire"} for event in events),
        pig_variety_count=len(set(normalized_rolls)),
        human_count=sum(pig_id in human_pig_ids for pig_id in normalized_rolls),
        ordinary_success_count=sum(
            event.event_type == "success" and not event.is_reservation
            for event in events
        ),
        top_pig_id=top_pig_id,
        top_pig_count=top_pig_count,
    )


def select_overview_metrics(overview: DailyOverview) -> tuple[OverviewMetric, ...]:
    """固定保留两项基础统计，并按预约、逃脱、反噬、种类顺序选择第三项。"""

    metrics = [
        OverviewMetric("roll_count", "小猪数量", overview.roll_count, "头"),
        OverviewMetric("ordinary_roast", "普通烤猪", overview.ordinary_roast_count, "次"),
    ]
    if overview.reservation_count > 0:
        metrics.append(OverviewMetric("reservation", "预约烤猪", overview.reservation_count, "场"))
    elif overview.escape_count > 0:
        metrics.append(OverviewMetric("escape", "成功逃脱", overview.escape_count, "次"))
    elif overview.backfire_count > 0:
        metrics.append(OverviewMetric("backfire", "意外翻车", overview.backfire_count, "次"))
    elif overview.pig_variety_count > 0:
        metrics.append(OverviewMetric("pig_variety", "小猪种类", overview.pig_variety_count, "种"))
    return tuple(metrics)


def select_observation(
    overview: DailyOverview,
    group_rolls: Mapping[str, str],
    events: Sequence[NormalizedDailyEvent],
) -> ObservationSelection | None:
    """按固定优先级最多选出一条观察；未过阈值时返回 None。"""

    normalized_group_rolls = _normalize_group_rolls(group_rolls)
    total_roasts = overview.ordinary_roast_count + overview.reservation_count
    reservation_results = Counter(
        event.result_kind
        for event in events
        if event.is_reservation and event.result_kind in {"success", "escape", "backfire"}
    )
    all_results = Counter(
        event.result_kind
        for event in events
        if event.result_kind in {"success", "escape", "backfire"}
    )
    ordinary_results = Counter(
        event.result_kind
        for event in events
        if not event.is_reservation
        and event.event_type != "self_roast"
        and event.result_kind in {"success", "escape", "backfire"}
    )
    dominant_result = ""
    if reservation_results:
        # 同数时保持“成功→逃脱→反噬”，避免 Counter 首次出现顺序影响结果。
        dominant_result = max(
            ("success", "escape", "backfire"),
            key=lambda kind: (reservation_results[kind], -("success", "escape", "backfire").index(kind)),
        )

    candidates: dict[ObservationKind, ObservationSelection] = {}
    # 保留原有大样本门槛；三四次互动的小群日必须至少两次同结果，
    # 避免一次偶发结果就被写成全天趋势。
    if overview.reservation_count >= 3 or (
        overview.reservation_count >= 2
        and total_roasts > 0
        and overview.reservation_count / total_roasts >= 0.30
    ):
        candidates["reservation"] = ObservationSelection(
            "reservation",
            total_roasts,
            overview.reservation_count,
            dominant_result,
            reservation_results["success"],
            reservation_results["escape"],
            reservation_results["backfire"],
        )
    if (total_roasts >= 4 and overview.backfire_count / total_roasts >= 0.30) or (
        total_roasts >= 3
        and overview.backfire_count >= 2
        and overview.backfire_count / total_roasts >= 0.50
    ):
        candidates["backfire"] = ObservationSelection(
            "backfire",
            total_roasts,
            overview.backfire_count,
            success_count=all_results["success"],
            escape_count=all_results["escape"],
            backfire_count=all_results["backfire"],
        )
    if (total_roasts >= 5 and overview.escape_count / total_roasts >= 0.40) or (
        total_roasts >= 3
        and overview.escape_count >= 2
        and overview.escape_count / total_roasts >= 0.50
    ):
        candidates["escape"] = ObservationSelection(
            "escape",
            total_roasts,
            overview.escape_count,
            success_count=all_results["success"],
            escape_count=all_results["escape"],
            backfire_count=all_results["backfire"],
        )
    if (
        overview.ordinary_roast_count >= 5
        and overview.ordinary_success_count / overview.ordinary_roast_count >= 0.60
    ) or (
        overview.ordinary_roast_count >= 3
        and overview.ordinary_success_count >= 3
        and overview.ordinary_success_count / overview.ordinary_roast_count >= 0.75
    ):
        candidates["success"] = ObservationSelection(
            "success",
            overview.ordinary_roast_count,
            overview.ordinary_success_count,
            success_count=ordinary_results["success"],
            escape_count=ordinary_results["escape"],
            backfire_count=ordinary_results["backfire"],
        )
    if overview.human_count >= 2 or (
        overview.roll_count >= 8
        and overview.human_count / overview.roll_count >= 0.15
    ):
        candidates["human"] = ObservationSelection(
            "human", overview.roll_count, overview.human_count
        )

    pig_counts = Counter(normalized_group_rolls.values())
    duplicate_count = max(0, overview.roll_count - overview.pig_variety_count)
    if overview.roll_count > 0 and (
        max(pig_counts.values(), default=0) >= 3
        or duplicate_count / overview.roll_count >= 0.25
    ):
        candidates["collision"] = ObservationSelection(
            "collision", overview.roll_count, duplicate_count
        )
    if (
        overview.roll_count >= 8
        and overview.pig_variety_count / overview.roll_count >= 0.90
        and max(pig_counts.values(), default=0) <= 2
    ):
        candidates["variety"] = ObservationSelection(
            "variety", overview.roll_count, overview.pig_variety_count
        )
    return next((candidates[kind] for kind in OBSERVATION_PRIORITY if kind in candidates), None)


# ================================ 今日头条评分 ================================ #


_HEADLINE_BASE_SCORE: dict[HeadlineKind, int] = {
    "normal_success": 35,
    "normal_escape": 40,
    "normal_backfire": 45,
    "self_roast": 35,
    "bot_backfire": 60,
    "reservation_success": 35,
    "reservation_escape": 40,
    "reservation_backfire": 50,
    "reservation_human": 50,
    "reservation_food": 50,
    "reservation_eaten": 50,
    "reservation_sold": 50,
}


def _headline_kind(event: NormalizedDailyEvent) -> HeadlineKind | None:
    if event.is_reservation:
        if event.event_type == "success":
            return "reservation_success"
        if event.event_type == "escape":
            return "reservation_escape"
        if event.event_type == "backfire":
            return "reservation_backfire"
        if event.event_type == "reserved_special" and event.special_reason in {
            "human", "food", "eaten", "sold"
        }:
            return f"reservation_{event.special_reason}"  # type: ignore[return-value]
        return None
    return {
        "success": "normal_success",
        "escape": "normal_escape",
        "backfire": "normal_backfire",
        "self_roast": "self_roast",
        "bot_backfire": "bot_backfire",
    }.get(event.event_type)  # type: ignore[return-value]


def _pair_key(event: NormalizedDailyEvent) -> tuple[str, str] | None:
    if not event.attacker_id or not event.target_id:
        return None
    # 自烤保留 (user, user)，以便沿用同一人多次自烤的既有候选条件。
    return tuple(sorted((event.attacker_id, event.target_id)))


def reservation_party_size(event: NormalizedDailyEvent) -> int:
    """统一预约总人数口径；旧事件若未把主厨写入名单，仍补算主厨。"""

    participant_ids = set(event.participant_ids)
    return max(
        1,
        event.participant_count,
        len(participant_ids) + (0 if event.attacker_id in participant_ids else 1),
    )


def select_headline(events: Sequence[NormalizedDailyEvent]) -> HeadlineSelection | None:
    """先按事件规模与当天互动选候选，再稳定比较事件分数。"""

    pair_counts = Counter(pair for event in events if (pair := _pair_key(event)) is not None)
    interaction_events = tuple(event for event in events if event.event_type != "self_roast")
    active_day = len(interaction_events) >= 3
    mixed_results = len({event.result_kind for event in interaction_events}) >= 2
    candidates: list[HeadlineSelection] = []
    for event in events:
        kind = _headline_kind(event)
        if kind is None:
            continue
        repeated_pair_events = max(0, pair_counts.get(_pair_key(event), 0) - 1)
        participant_count = reservation_party_size(event) if event.is_reservation else 0

        # 单人预约不因“预约”身份压过真正的群事件；两人预约普通结果
        # 只有在当天另有交锋或结局反转时才进入候选。
        if event.is_reservation:
            if participant_count == 1:
                continue
            if participant_count == 2 and kind in {"reservation_success", "reservation_escape"}:
                if not active_day or not (mixed_results or repeated_pair_events):
                    continue
        elif kind in {"normal_success", "normal_escape", "normal_backfire"}:
            if not active_day or not (mixed_results or repeated_pair_events):
                continue
        elif kind == "self_roast" and repeated_pair_events < 4:
            continue

        # 六人后协作价值递减；十二人上限不该把普通预约堆到双倍于 Bot 反噬。
        size_bonus = 15 if participant_count >= 10 else 10 if participant_count >= 6 else 0
        score = (
            _HEADLINE_BASE_SCORE[kind]
            + min(max(0, participant_count - 1), 5) * 5
            + size_bonus
            + min(repeated_pair_events, 3) * 5
        )
        candidates.append(HeadlineSelection(kind, score, repeated_pair_events, event))
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda item: (
            -item.score,
            -reservation_party_size(item.event) if item.event.is_reservation else 0,
            -_HEADLINE_BASE_SCORE[item.kind],
            _timestamp_key(item.event.created_at),
            item.event.event_id,
        ),
    )


# ================================ 事件追踪与头条去重 ================================ #


def _unique_events(
    events: Iterable[NormalizedDailyEvent],
) -> tuple[NormalizedDailyEvent, ...]:
    unique = {event.event_id: event for event in events}
    return tuple(sorted(unique.values(), key=_event_sort_key))


def _build_timeline_candidates(
    events: Sequence[NormalizedDailyEvent],
) -> list[_TimelineCandidate]:
    candidates: list[_TimelineCandidate] = []
    directed_pairs: dict[tuple[str, str], list[NormalizedDailyEvent]] = defaultdict(list)
    initiated_by_user: dict[str, list[NormalizedDailyEvent]] = defaultdict(list)
    for event in events:
        if event.attacker_id:
            initiated_by_user[event.attacker_id].append(event)
        if event.attacker_id and event.target_id and event.attacker_id != event.target_id:
            directed_pairs[(event.attacker_id, event.target_id)].append(event)

    seen_mutual: set[tuple[str, str]] = set()
    for attacker_id, target_id in directed_pairs:
        pair = tuple(sorted((attacker_id, target_id)))
        if pair in seen_mutual or (target_id, attacker_id) not in directed_pairs:
            continue
        seen_mutual.add(pair)
        candidates.append(
            _TimelineCandidate(
                "mutual",
                ":".join(pair),
                _unique_events(
                    (*directed_pairs[(attacker_id, target_id)], *directed_pairs[(target_id, attacker_id)])
                ),
            )
        )

    for user_id, user_events in initiated_by_user.items():
        ordered = _unique_events(user_events)
        if len(ordered) < 3:
            continue
        results = {event.result_kind for event in ordered}
        mixes_reservation = any(event.is_reservation for event in ordered) and any(
            not event.is_reservation for event in ordered
        )
        if len(results) > 1 or mixes_reservation:
            candidates.append(_TimelineCandidate("personal_turn", user_id, ordered))

    for reservation_event in (event for event in events if event.is_reservation):
        owner_id = reservation_event.attacker_id
        target_id = reservation_event.target_id
        if not owner_id or not target_id or owner_id == target_id:
            continue
        reservation_pair = {owner_id, target_id}
        related = [reservation_event]
        related.extend(
            event
            for event in events
            if not event.is_reservation
            and {event.attacker_id, event.target_id} == reservation_pair
            and _event_sort_key(event) > _event_sort_key(reservation_event)
        )
        ordered = _unique_events(related)
        if len(ordered) >= 2:
            candidates.append(
                _TimelineCandidate(
                    "reservation_followup",
                    reservation_event.reservation_id,
                    ordered,
                    reservation_event,
                )
            )

    for (attacker_id, target_id), pair_events in directed_pairs.items():
        ordered = _unique_events(pair_events)
        if len(ordered) >= 2:
            candidates.append(
                _TimelineCandidate(
                    "repeat_target",
                    f"{attacker_id}:{target_id}",
                    ordered,
                )
            )
    return candidates


def select_timeline(
    events: Sequence[NormalizedDailyEvent],
    headline: HeadlineSelection | None,
) -> TimelineSelection | None:
    """选择唯一事件链；先移除头条原事件，不足两段的候选直接作废。"""

    headline_event_id = headline.event.event_id if headline else ""
    candidates: list[_TimelineCandidate] = []
    for candidate in _build_timeline_candidates(events):
        remaining = tuple(
            event for event in candidate.events if event.event_id != headline_event_id
        )
        if len(remaining) >= 2:
            candidates.append(
                _TimelineCandidate(
                    candidate.kind,
                    candidate.identity,
                    remaining,
                    candidate.anchor_event,
                )
            )
    if not candidates:
        return None

    def candidate_key(candidate: _TimelineCandidate) -> tuple[object, ...]:
        result_count = len({event.result_kind for event in candidate.events})
        contains_special = any(
            event.is_reservation or event.result_kind == "backfire"
            for event in candidate.events
        )
        return (
            TIMELINE_PRIORITY.index(candidate.kind),
            -len(candidate.events),
            -result_count,
            -int(contains_special),
            _event_sort_key(candidate.events[0]),
            candidate.identity,
        )

    selected = min(candidates, key=candidate_key)
    return TimelineSelection(
        selected.kind,
        selected.events[:3],
        selected.anchor_event,
    )


# ================================ 今日排行 ================================ #


def _rank_entries(
    kind: RankingKind,
    rows: Sequence[tuple[str, str, int, str, str, str, str, bool]],
) -> DailyRanking | None:
    """按分数、首次达成时间和用户 ID 排序，先计算并列名次再截取三人。"""

    if not rows:
        return None
    ordered = sorted(rows, key=lambda row: (-row[2], _timestamp_key(row[6]), row[0]))
    entries: list[RankingEntry] = []
    previous_score: int | None = None
    current_rank = 0
    for position, row in enumerate(ordered, start=1):
        user_id, display_name, score, pig_id, pig_name, image_name, achieved_at, fallback = row
        if score != previous_score:
            current_rank = position
            previous_score = score
        entries.append(
            RankingEntry(
                rank=current_rank,
                user_id=user_id,
                display_name=display_name,
                score=score,
                pig_id=pig_id,
                pig_name=pig_name,
                image_name=image_name,
                achieved_at=achieved_at,
                uses_fallback_pig_stamp=fallback,
            )
        )
    return DailyRanking(kind, tuple(entries[:3]))


def build_rankings(
    participant_ids: Sequence[str],
    group_rolls: Mapping[str, str],
    events: Sequence[NormalizedDailyEvent],
    profiles: Mapping[str, DailyUserReportProfile],
    display_names: Mapping[str, str],
) -> tuple[DailyRanking, ...]:
    """生成 EX、普通烤猪成功和图鉴收录排行；缺少聚合字段时只隐藏对应榜。"""

    normalized_group_rolls = _normalize_group_rolls(group_rolls)
    candidates = set(participant_ids)
    success_events: dict[str, list[NormalizedDailyEvent]] = defaultdict(list)
    for event in events:
        if event.event_type == "success" and not event.is_reservation and event.attacker_id in candidates:
            success_events[event.attacker_id].append(event)

    ex_rows: list[tuple[str, str, int, str, str, str, str, bool]] = []
    success_rows: list[tuple[str, str, int, str, str, str, str, bool]] = []
    catalog_rows: list[tuple[str, str, int, str, str, str, str, bool]] = []
    for user_id in sorted(candidates):
        profile = profiles.get(user_id, DailyUserReportProfile(user_id=user_id))
        display_name = profile.display_name or display_names.get(user_id, user_id)
        daily_pig_id = normalized_group_rolls.get(user_id, "")
        if daily_pig_id and profile.daily_ex_level is not None:
            ex_rows.append(
                (
                    user_id,
                    display_name,
                    max(0, int(profile.daily_ex_level)),
                    daily_pig_id,
                    profile.daily_pig_name,
                    profile.daily_image_name,
                    profile.daily_achieved_at,
                    False,
                )
            )

        user_successes = sorted(success_events.get(user_id, ()), key=_event_sort_key)
        if user_successes and daily_pig_id:
            success_rows.append(
                (
                    user_id,
                    display_name,
                    len(user_successes),
                    daily_pig_id,
                    profile.daily_pig_name,
                    profile.daily_image_name,
                    user_successes[-1].created_at,
                    False,
                )
            )

        if profile.catalog_count is not None and profile.catalog_count > 0:
            catalog_pig_id = daily_pig_id or profile.recent_pig_id
            catalog_rows.append(
                (
                    user_id,
                    display_name,
                    int(profile.catalog_count),
                    catalog_pig_id,
                    profile.daily_pig_name if daily_pig_id else profile.recent_pig_name,
                    profile.daily_image_name if daily_pig_id else profile.recent_image_name,
                    profile.catalog_achieved_at,
                    not bool(catalog_pig_id),
                )
            )

    rankings = (
        _rank_entries("expert_level", ex_rows),
        _rank_entries("roast_success", success_rows),
        _rank_entries("catalog", catalog_rows),
    )
    return tuple(ranking for ranking in rankings if ranking is not None)


# ================================ 日报总装配 ================================ #


def build_daily_report(
    *,
    date_str: str,
    group_id: str,
    group_rolls: Mapping[str, str],
    raw_events: Sequence[Mapping[str, object]],
    active_user_ids: Iterable[str] = (),
    bot_user_ids: Iterable[str] = (),
    user_profiles: Mapping[str, DailyUserReportProfile] | None = None,
    protections: Sequence[ProtectionReportItem] = (),
    human_pig_ids: Iterable[str] = ("human",),
    cutoff_at: str = "",
) -> DailyReport:
    """构建一份不依赖 NoneBot、Pillow 或具体存储实现的日报业务快照。"""

    profiles = user_profiles or {}
    events = normalize_daily_events(
        raw_events,
        group_id=group_id,
        cutoff_at=cutoff_at,
    )
    normalized_bot_ids = frozenset(_text(user_id) for user_id in bot_user_ids if _text(user_id))
    normalized_group_rolls = {
        user_id: pig_id
        for user_id, pig_id in _normalize_group_rolls(group_rolls).items()
        if user_id not in normalized_bot_ids
    }
    participant_ids = _collect_participants(
        normalized_group_rolls,
        events,
        active_user_ids,
        normalized_bot_ids,
    )
    display_names = _collect_display_names(events, profiles)
    overview = build_daily_overview(
        normalized_group_rolls,
        events,
        human_pig_ids=frozenset(_text(pig_id) for pig_id in human_pig_ids if _text(pig_id)),
    )
    overview_metrics = select_overview_metrics(overview)
    observation = select_observation(overview, normalized_group_rolls, events)
    headline = select_headline(events)
    timeline = select_timeline(events, headline)
    rankings = build_rankings(
        participant_ids,
        normalized_group_rolls,
        events,
        profiles,
        display_names,
    )
    return DailyReport(
        date_str=_text(date_str),
        group_id=_text(group_id),
        has_activity=bool(normalized_group_rolls or events or participant_ids),
        participant_ids=participant_ids,
        display_names=display_names,
        events=events,
        overview=overview,
        overview_metrics=overview_metrics,
        observation=observation,
        headline=headline,
        timeline=timeline,
        rankings=rankings,
        protections=tuple(protections),
    )
