from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

from ..host import logger

from .resource_manager import RollPigResourceManager, pig_resource_manager
from .runtime import rollpig_date_str
from .store.base import RollpigStore
from .store.models import DailyRollSnapshot, expert_level_from_copies
from .texts import (
    YESTERDAY_EXPERIENCE_TEXTS,
    YESTERDAY_RECAP_TEXT_VERSION,
    YESTERDAY_SUMMARY_TEXTS,
)


YesterdayScope = Literal["group", "cross_group"]


class YesterdayPigResourceMissingError(RuntimeError):
    """兼容旧调用方保留的异常类型；当前回顾构建已改为无立绘降级。"""


@dataclass(frozen=True)
class YesterdayFootprint:
    kind: str
    label: str
    count: int


@dataclass(frozen=True)
class YesterdayExperience:
    family: str
    text: str
    event_id: str


@dataclass(frozen=True)
class YesterdaySummary:
    kind: str
    text: str


@dataclass(frozen=True)
class NormalizedYesterdayEvent:
    event_id: str
    created_at: str
    source_index: int
    event_type: str
    family: str
    user_role: str
    attacker_id: str
    target_id: str
    attacker_name: str
    target_name: str
    food: str
    group_id: str
    reservation_id: str
    participant_ids: tuple[str, ...]
    participant_names: tuple[str, ...]
    participant_count: int
    backfire_victim_id: str
    backfire_victim_name: str
    special_reason: str
    fingerprint: str


@dataclass(frozen=True)
class YesterdayRecap:
    date_str: str
    scope: YesterdayScope
    group_id: str
    roll: DailyRollSnapshot
    pig_name: str
    image_path: Path | None
    fallback_image_path: Path | None
    resource_version: str
    outcome_text: str
    footprints: tuple[YesterdayFootprint, ...]
    experiences: tuple[YesterdayExperience, ...]
    summary: YesterdaySummary | None
    aftereffect_text: str
    events_available: bool
    pig_description: str = ""
    pig_analysis: str = ""


@dataclass(frozen=True)
class _ExperienceCandidate:
    family: str
    text: str
    event_id: str
    fingerprint: str
    score: int
    participant_count: int
    created_at_key: float
    event_id_key: str


# ================================ 稳定散列与事件归一化 ================================ #


def _stable_pick(pool: Sequence[str], *parts: object) -> str:
    """按业务身份稳定选句；相同数据跨进程、跨 Local/Cloud 始终得到同一句。"""

    if not pool:
        raise ValueError("昨日回顾文案池不能为空")
    seed = "\x1f".join(str(part) for part in (YESTERDAY_RECAP_TEXT_VERSION, *parts))
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return pool[int.from_bytes(digest[:8], "big") % len(pool)]


def _event_fingerprint(date_str: str, raw: dict) -> str:
    raw_participant_ids = raw.get("participant_ids", [])
    participant_ids = raw_participant_ids if isinstance(raw_participant_ids, list) else []
    payload = {
        "date": date_str,
        "type": str(raw.get("type") or ""),
        "attacker": str(raw.get("attacker") or ""),
        "target": str(raw.get("target") or ""),
        "food": str(raw.get("food") or ""),
        "group": str(raw.get("group_id") or ""),
        "reservation_id": str(raw.get("reservation_id") or ""),
        "participant_ids": sorted({str(item) for item in participant_ids if item}),
        "backfire_victim_id": str(raw.get("backfire_victim_id") or ""),
        "special_reason": str(raw.get("special_reason") or ""),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _created_at_key(value: str, source_index: int) -> float:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.timestamp()
    except (OverflowError, ValueError):
        return float(source_index)


def _event_id_key(event_id: str) -> str:
    return f"1:{int(event_id):020d}" if event_id.isdigit() else f"0:{event_id}"


def _safe_nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _normal_event_family(event_type: str, attacker_id: str, target_id: str, user_id: str) -> tuple[str, str]:
    if event_type == "self_roast" and attacker_id == user_id:
        return "self_roast", "self"
    if event_type == "success" and attacker_id == user_id:
        return "success_as_attacker", "attacker"
    if event_type == "success" and target_id == user_id:
        return "success_as_target", "target"
    if event_type == "escape" and target_id == user_id:
        return "escape_as_target", "target"
    if event_type == "escape" and attacker_id == user_id:
        return "escape_as_attacker", "attacker"
    if event_type == "backfire" and attacker_id == user_id:
        return "normal_backfire", "backfire_victim"
    if event_type == "bot_backfire" and attacker_id == user_id:
        return "bot_backfire", "backfire_victim"
    return "", ""


def _reservation_event_family(
    event_type: str,
    *,
    user_id: str,
    attacker_id: str,
    target_id: str,
    participant_ids: tuple[str, ...],
    backfire_victim_id: str,
    special_reason: str,
) -> tuple[str, str]:
    # 预约 Owner 只是事件中的 attacker；反噬受害者必须优先使用明确快照字段。
    if event_type == "backfire" and backfire_victim_id == user_id:
        return "reservation_backfire_victim", "backfire_victim"
    if target_id == user_id:
        role = "target"
    elif attacker_id == user_id:
        role = "owner"
    elif user_id in participant_ids:
        role = "participant"
    else:
        return "", ""

    if event_type == "success":
        return ("reservation_success_target" if role == "target" else "reservation_success_participant"), role
    if event_type == "escape":
        return ("reservation_escape_target" if role == "target" else "reservation_escape_participant"), role
    if event_type == "backfire":
        return "reservation_backfire_participant", role
    if event_type == "reserved_special":
        if role != "target":
            return "reserved_special_participant", role
        if special_reason in {"human", "food", "eaten", "sold"}:
            return f"reserved_special_target_{special_reason}", role
        # 旧事件没有保存特殊状态时，宁可不展示经历，也不能套用错误的本人视角。
        return "", ""
    return "", ""


def normalize_yesterday_events(
    raw_events: Sequence[dict],
    *,
    date_str: str,
    user_id: str,
) -> tuple[NormalizedYesterdayEvent, ...]:
    """把新旧 Local/Cloud 事件转为稳定领域对象，并过滤与当前用户无关的记录。"""

    normalized: list[NormalizedYesterdayEvent] = []
    seen_reservations: set[str] = set()
    for source_index, raw in enumerate(raw_events):
        if not isinstance(raw, dict):
            continue
        event_type = str(raw.get("type") or "")
        attacker_id = str(raw.get("attacker") or "")
        target_id = str(raw.get("target") or "")

        # ================================ 预约人数归一化 ================================ #
        # participant_count 的业务口径包含主厨。旧快照可能漏掉主厨或昵称，
        # 因此先按 ID 去重并补齐主厨，再与服务端保存的数量取较大值。
        raw_participant_ids = raw.get("participant_ids", [])
        raw_participant_names = raw.get("participant_names", [])
        participant_id_list: list[str] = []
        participant_name_map: dict[str, str] = {}
        if isinstance(raw_participant_ids, list):
            for index, item in enumerate(raw_participant_ids):
                participant_id = str(item or "")
                if not participant_id:
                    continue
                if participant_id not in participant_name_map:
                    participant_id_list.append(participant_id)
                participant_name = (
                    str(raw_participant_names[index] or "")
                    if isinstance(raw_participant_names, list) and index < len(raw_participant_names)
                    else ""
                )
                if participant_name or participant_id not in participant_name_map:
                    participant_name_map[participant_id] = participant_name
        reservation_id = str(raw.get("reservation_id") or "")
        backfire_victim_id = str(raw.get("backfire_victim_id") or "")
        special_reason = str(raw.get("special_reason") or "")
        if reservation_id and attacker_id and attacker_id not in participant_name_map:
            participant_id_list.append(attacker_id)
            participant_name_map[attacker_id] = str(raw.get("attacker_name") or "")
        participant_ids = tuple(participant_id_list)
        participant_names = tuple(participant_name_map[item] for item in participant_ids)

        if reservation_id:
            if reservation_id in seen_reservations:
                continue
            family, user_role = _reservation_event_family(
                event_type,
                user_id=user_id,
                attacker_id=attacker_id,
                target_id=target_id,
                participant_ids=participant_ids,
                backfire_victim_id=backfire_victim_id,
                special_reason=special_reason,
            )
        else:
            family, user_role = _normal_event_family(event_type, attacker_id, target_id, user_id)
        if not family:
            continue

        fingerprint = _event_fingerprint(date_str, raw)
        event_id = str(raw.get("event_id") or f"legacy-{source_index:08d}-{fingerprint[:12]}")
        created_at = str(raw.get("created_at") or f"{date_str}T00:00:00+00:00")
        participant_count = max(
            len(participant_ids),
            _safe_nonnegative_int(raw.get("participant_count")),
        )
        normalized.append(NormalizedYesterdayEvent(
            event_id=event_id,
            created_at=created_at,
            source_index=source_index,
            event_type=event_type,
            family=family,
            user_role=user_role,
            attacker_id=attacker_id,
            target_id=target_id,
            attacker_name=str(raw.get("attacker_name") or ""),
            target_name=str(raw.get("target_name") or ""),
            food=str(raw.get("food") or ""),
            group_id=str(raw.get("group_id") or ""),
            reservation_id=reservation_id,
            participant_ids=participant_ids,
            participant_names=participant_names,
            participant_count=participant_count,
            backfire_victim_id=backfire_victim_id,
            backfire_victim_name=str(raw.get("backfire_victim_name") or ""),
            special_reason=special_reason,
            fingerprint=fingerprint,
        ))
        if reservation_id:
            seen_reservations.add(reservation_id)
    return tuple(normalized)


# ================================ 足迹与经历选择 ================================ #


def _display_name(name: str, *, scope: YesterdayScope) -> str:
    if scope == "group":
        return name or "一名群友"
    return "一名群友"


def _special_reason_text(reason: str) -> str:
    return {
        "human": "它以人类形态出现",
        "food": "它现身时已经是熟食",
        "eaten": "它出现时只剩空盘",
        "sold": "它出现前已经售出",
    }.get(reason, "它以无法开烤的特殊形态出现")


def _experience_context(event: NormalizedYesterdayEvent, scope: YesterdayScope) -> dict[str, object]:
    participant_name_map = dict(zip(event.participant_ids, event.participant_names))
    victim_name = event.backfire_victim_name or participant_name_map.get(event.backfire_victim_id, "")
    return {
        "attacker": (
            "你" if event.user_role in {"attacker", "owner"}
            else _display_name(event.attacker_name, scope=scope)
        ),
        "target": "你" if event.user_role == "target" else _display_name(event.target_name, scope=scope),
        "victim": (
            "你" if event.user_role == "backfire_victim"
            else _display_name(victim_name, scope=scope)
        ),
        "food": event.food or "一道不明熟食",
        "participant_count": event.participant_count,
        "other_count": max(0, event.participant_count - 1),
        "special_reason": _special_reason_text(event.special_reason),
    }


def _experience_score(event: NormalizedYesterdayEvent) -> int:
    participant_bonus = min(max(event.participant_count - 1, 0), 5)
    if event.family == "bot_backfire":
        return 100
    if event.family == "reservation_backfire_victim":
        return 96
    if event.reservation_id and event.user_role == "target":
        return 93
    if event.family == "reservation_success_participant":
        return 85 + participant_bonus
    if event.family == "reserved_special_participant":
        return 81 + participant_bonus
    if event.family == "reservation_backfire_participant":
        return 79 + participant_bonus
    if event.family == "normal_backfire":
        return 84
    if event.family == "self_roast":
        return 80
    if event.family == "reservation_escape_participant":
        return 75 + participant_bonus
    return {
        "escape_as_target": 76,
        "success_as_target": 72,
        "success_as_attacker": 68,
        "escape_as_attacker": 64,
    }.get(event.family, 0)


def _experience_pool_key(event: NormalizedYesterdayEvent) -> str:
    """为预约成功与逃脱选择人数分池；经历 family 保持稳定，供排序和去重使用。"""

    count_aware_families = {
        "reservation_success_participant",
        "reservation_success_target",
        "reservation_escape_participant",
        "reservation_escape_target",
    }
    if event.family not in count_aware_families:
        return event.family
    if event.participant_count >= 2:
        suffix = "multi"
    elif event.participant_count == 1:
        suffix = "single"
    else:
        suffix = "unknown"
    return f"{event.family}_{suffix}"


def _build_experience_candidate(
    event: NormalizedYesterdayEvent,
    *,
    date_str: str,
    user_id: str,
    scope: YesterdayScope,
    group_id: str,
) -> _ExperienceCandidate:
    pool_key = _experience_pool_key(event)
    template = _stable_pick(
        YESTERDAY_EXPERIENCE_TEXTS[pool_key],
        date_str,
        user_id,
        scope,
        group_id,
        pool_key,
        event.fingerprint,
    )
    return _ExperienceCandidate(
        family=event.family,
        text=template.format(**_experience_context(event, scope)),
        event_id=event.event_id,
        fingerprint=event.fingerprint,
        score=_experience_score(event),
        participant_count=event.participant_count,
        created_at_key=_created_at_key(event.created_at, event.source_index),
        event_id_key=_event_id_key(event.event_id),
    )


def select_yesterday_experiences(
    events: Sequence[NormalizedYesterdayEvent],
    *,
    date_str: str,
    user_id: str,
    scope: YesterdayScope,
    group_id: str,
) -> tuple[YesterdayExperience, ...]:
    candidates = [
        _build_experience_candidate(
            event,
            date_str=date_str,
            user_id=user_id,
            scope=scope,
            group_id=group_id,
        )
        for event in events
    ]
    # 先按 fingerprint 升序稳定打底，再对四个主要指标做降序排序。
    candidates.sort(key=lambda candidate: candidate.fingerprint)
    candidates.sort(
        key=lambda candidate: (
            candidate.score,
            candidate.participant_count,
            candidate.created_at_key,
            candidate.event_id_key,
        ),
        reverse=True,
    )
    if not candidates:
        return ()

    selected = [candidates[0]]
    second = next(
        (candidate for candidate in candidates[1:] if candidate.family != candidates[0].family),
        None,
    )
    if second is not None:
        selected.append(second)
    return tuple(
        YesterdayExperience(
            family=candidate.family,
            text=candidate.text,
            event_id=candidate.event_id,
        )
        for candidate in selected
    )


def _event_counts(events: Sequence[NormalizedYesterdayEvent]) -> dict[str, int]:
    counts = {
        "related_event_count": len(events),
        "success_count": 0,
        "roasted_count": 0,
        "escaped_count": 0,
        "backfire_count": 0,
        "self_roast_count": 0,
        "bot_backfire_count": 0,
        "reservation_result_count": 0,
        "collaborative_reservation_count": 0,
        "targeted_count": 0,
    }
    for event in events:
        if event.reservation_id and event.user_role in {"owner", "participant", "backfire_victim"}:
            counts["reservation_result_count"] += 1
            if event.participant_count >= 2:
                counts["collaborative_reservation_count"] += 1
        if event.reservation_id and event.user_role == "target":
            counts["targeted_count"] += 1

        if event.family in {"success_as_attacker", "reservation_success_participant"}:
            counts["success_count"] += 1
        elif event.family in {"success_as_target", "reservation_success_target"}:
            counts["roasted_count"] += 1
            if not event.reservation_id:
                counts["targeted_count"] += 1
        elif event.family in {"escape_as_target", "reservation_escape_target"}:
            counts["escaped_count"] += 1
            if not event.reservation_id:
                counts["targeted_count"] += 1
        elif event.family in {"normal_backfire", "reservation_backfire_victim"}:
            counts["backfire_count"] += 1
        elif event.family == "bot_backfire":
            counts["backfire_count"] += 1
            counts["bot_backfire_count"] += 1
        elif event.family == "self_roast":
            counts["self_roast_count"] += 1
    return counts


def build_yesterday_footprints(
    events: Sequence[NormalizedYesterdayEvent],
) -> tuple[YesterdayFootprint, ...]:
    counts = _event_counts(events)
    if counts["related_event_count"] < 3:
        return ()
    definitions = (
        ("bot_backfire_count", "挑战 Bot", 0, 0),
        ("reservation_result_count", "已结算预约", 0, 1),
        ("backfire_count", "反噬落到本人", 1, 0),
        ("escaped_count", "成功逃脱", 1, 1),
        ("success_count", "烤成别人", 2, 0),
        ("roasted_count", "被成功烤", 2, 1),
        ("self_roast_count", "自烤", 3, 0),
    )
    visible = [definition for definition in definitions if counts[definition[0]] > 0]
    visible.sort(key=lambda item: (item[2], -counts[item[0]], item[3]))
    return tuple(
        YesterdayFootprint(kind=key, label=label, count=counts[key])
        for key, label, _tier, _order in visible[:3]
    )


# ================================ 多事件小结 ================================ #


def build_yesterday_summary(
    events: Sequence[NormalizedYesterdayEvent],
    *,
    date_str: str,
    user_id: str,
    scope: YesterdayScope,
    group_id: str,
) -> YesterdaySummary | None:
    counts = _event_counts(events)
    related = counts["related_event_count"]
    if related <= 0:
        text = (
            "本群昨天没有发生与你有关的烤猪事件。"
            if scope == "group"
            else "昨天没有记录到与你有关的烤猪事件。"
        )
        return YesterdaySummary(kind="none", text=text)
    if related < 3:
        return None

    passive_count = counts["roasted_count"] + counts["escaped_count"]

    # 顺序是唯一仲裁链：具体且异常的整日倾向优先于通用倾向，命中即停。
    if counts["self_roast_count"] >= 2:
        kind = "self_service"
    elif counts["backfire_count"] >= 2:
        kind = "cursed"
    elif (
        counts["escaped_count"] >= 3
        and counts["targeted_count"] > 0
        and counts["escaped_count"] / counts["targeted_count"] >= 0.6
    ):
        kind = "escape_artist"
    elif counts["roasted_count"] >= 3 and counts["roasted_count"] / related >= 0.5:
        kind = "victim"
    elif (
        counts["collaborative_reservation_count"] >= 2
        and counts["collaborative_reservation_count"] / related >= 0.5
    ):
        kind = "team_player"
    elif counts["success_count"] >= 3 and counts["success_count"] / related >= 0.5:
        kind = "chef"
    elif counts["success_count"] >= 2 and passive_count >= 2:
        kind = "chaotic"
    else:
        return None

    signature = json.dumps(counts, sort_keys=True, separators=(",", ":"))
    text = _stable_pick(
        YESTERDAY_SUMMARY_TEXTS[kind],
        date_str,
        user_id,
        scope,
        group_id,
        kind,
        signature,
    )
    return YesterdaySummary(kind=kind, text=text)


# ================================ 抽取结果与 ViewModel 组装 ================================ #


def build_yesterday_outcome_text(snapshot: DailyRollSnapshot) -> str:
    """抽取结果只保留一个事实槽位，不把 EX 成长与差分解锁拆开复读。"""

    if not snapshot.outcome_available:
        return ""
    if snapshot.is_new_pig:
        if snapshot.collection_size_after_roll:
            return f"新猪入圈 · 图鉴第 {snapshot.collection_size_after_roll} 只"
        return "新猪入圈"

    previous_level = (
        int(snapshot.previous_expert_level)
        if snapshot.previous_expert_level is not None
        else expert_level_from_copies(snapshot.previous_copies or 0)
    )
    current_level = (
        int(snapshot.expert_level_after_roll)
        if snapshot.expert_level_after_roll is not None
        else expert_level_from_copies(snapshot.copies_after_roll or 0)
    )
    if current_level <= previous_level:
        return ""
    prefix = f"EX Lv.{previous_level} → {current_level}"
    has_image = "image" in snapshot.unlocked_variant_fields
    has_text = bool(snapshot.unlocked_variant_fields & {"description", "analysis"})
    if has_image and has_text:
        return f"{prefix} · 新立绘与介绍已解锁"
    if has_image:
        return f"{prefix} · 新立绘已解锁"
    if has_text:
        return f"{prefix} · 新介绍已解锁"
    return prefix


async def build_yesterday_recap(
    user_id: str,
    *,
    group_id: str = "",
    date_str: str | None = None,
    recap_store: RollpigStore | None = None,
    resources: RollPigResourceManager = pig_resource_manager,
) -> YesterdayRecap | None:
    """组装与绘图无关的昨日回顾；没有昨日抽取记录时返回 None。"""

    if recap_store is None:
        from .store import store as configured_store

        recap_store = configured_store
    target_date = date_str or rollpig_date_str(-1)
    normalized_user_id = str(user_id)
    normalized_group_id = str(group_id or "")
    scope: YesterdayScope = "group" if normalized_group_id else "cross_group"

    roll = await recap_store.get_daily_roll_snapshot(normalized_user_id, target_date)
    if roll is None:
        return None
    # 昨日快照固定抽取与成长事实，名称和立绘则按当前资源重演。
    # 这样今天补上的 EX 差分能立即用于昨天已经达到对应等级的用户。
    pig = resources.pig_map.get(roll.pig_id)
    if pig is not None:
        if (
            roll.daily_feed_result is not None
            and roll.daily_feed_result.pig_id == roll.pig_id
        ):
            replay_ex_level = roll.daily_feed_result.new_level
        elif roll.expert_level_after_roll is not None:
            replay_ex_level = int(roll.expert_level_after_roll)
        elif roll.copies_after_roll is not None:
            replay_ex_level = expert_level_from_copies(roll.copies_after_roll)
        else:
            replay_ex_level = max(0, int(roll.resolved_variant_level or 0))
        appearance = resources.resolve_pig_appearance(pig, replay_ex_level)
        pig_name = str(appearance.pig_data.get("name") or roll.pig_id)
        image_path = appearance.image_path
        base_image_path = appearance.base_image_path
    else:
        # 当前资源包已经移除该猪时仍保留昨日事实卡；旧图片可能只是 active
        # 目录中的孤儿文件，不能绕过当前资源定义继续展示。
        logger.warning(
            "rollpig 昨日身份已不在当前资源池，已使用无立绘回顾卡: "
            f"date={target_date} user={normalized_user_id} pig_id={roll.pig_id} "
            f"snapshot_resource={roll.resource_version}"
        )
        pig_name = roll.pig_id
        image_path = None
        base_image_path = None

    if roll.is_makeup:
        # 补签展示当前资源在本次成长等级下的介绍，不虚构昨日互动。
        outcome_text = build_yesterday_outcome_text(roll)
        return YesterdayRecap(
            date_str=target_date, scope=scope, group_id=normalized_group_id,
            roll=roll, pig_name=pig_name, image_path=image_path,
            fallback_image_path=base_image_path, resource_version=roll.resource_version,
            outcome_text=outcome_text, footprints=(),
            experiences=(), summary=None, aftereffect_text="", events_available=False,
            pig_description=str(appearance.pig_data.get("description") or "") if pig else "",
            pig_analysis=str(appearance.pig_data.get("analysis") or "") if pig else "",
        )

    event_query = await recap_store.query_daily_events(
        date_str=target_date,
        group_id=normalized_group_id or None,
        user_id=normalized_user_id,
    )
    normalized_events = normalize_yesterday_events(
        list(event_query.items),
        date_str=target_date,
        user_id=normalized_user_id,
    ) if event_query.available else ()

    footprints = build_yesterday_footprints(normalized_events) if event_query.available else ()
    experiences = select_yesterday_experiences(
        normalized_events,
        date_str=target_date,
        user_id=normalized_user_id,
        scope=scope,
        group_id=normalized_group_id,
    ) if event_query.available else ()
    summary = build_yesterday_summary(
        normalized_events,
        date_str=target_date,
        user_id=normalized_user_id,
        scope=scope,
        group_id=normalized_group_id,
    ) if event_query.available else None

    aftereffect_text = ""
    if normalized_group_id:
        try:
            if await recap_store.is_protected(
                normalized_group_id,
                normalized_user_id,
                date_str=rollpig_date_str(),
            ):
                aftereffect_text = "昨天挨的烤没白挨：今天你在本群获得了一层保护，普通烤猪会被拦下。"
        except Exception as error:
            logger.warning(
                "rollpig 昨日回顾保护状态读取失败，已隐藏今日余波: "
                f"group={normalized_group_id} user={normalized_user_id} error={error}"
            )

    return YesterdayRecap(
        date_str=target_date,
        scope=scope,
        group_id=normalized_group_id,
        roll=roll,
        pig_name=pig_name,
        image_path=image_path,
        fallback_image_path=base_image_path,
        resource_version=roll.resource_version,
        outcome_text=build_yesterday_outcome_text(roll),
        footprints=footprints,
        experiences=experiences,
        summary=summary,
        aftereffect_text=aftereffect_text,
        events_available=event_query.available,
    )
