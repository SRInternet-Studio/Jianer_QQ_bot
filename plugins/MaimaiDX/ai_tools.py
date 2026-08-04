"""JianerAI tools backed by the real MaimaiDX services and renderers."""

from __future__ import annotations

import asyncio
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from jianer import common, segments
from jianer.adapters import Capability

from plugins.JianerAI.tools import (
    ToolContext,
    ToolExecutionError,
    ToolRegistration,
    ToolRisk,
    ToolSpec,
)

from . import adapter
from .core.clients.exceptions import UserNotBindError
from .core.database.qq import User, get_user
from .core.handler import (
    draw_best50,
    draw_chart_info,
    draw_play_data,
    draw_rating_ranking,
)
from .core.merge.models import ServiceName, Song
from .core.service import mai
from .commands.common import mentioned_query_user_id, parse_qq_id
from .resources import resource_issues
from .runtime import runtime


JIANER_AI_PLUGIN_ID = "jianerbot-plugin-jianer-ai"
DIVINGFISH_SOURCE = "水鱼查分器"
ALIAS_SOURCE = "Yuri-YuzuChaN别名数据源"
SUPPORTED_PROTOCOLS = frozenset(adapter.SUPPORTED_PROTOCOLS)
IMAGE_CAPABILITIES = frozenset({Capability.SEND_IMAGE})


def _tool_error(code: str, message: str) -> ToolExecutionError:
    return ToolExecutionError(code, message)


async def _ensure_ready(
    context: ToolContext,
    *,
    require_resources: bool,
) -> None:
    if not adapter.is_supported(context.event, context.actions):
        raise _tool_error(
            "maimaidx_protocol_unsupported",
            "MaimaiDX AI 工具仅支持 QQ OneBot/Milky 会话。",
        )
    runtime.observe_context(context.event, context.actions)
    if not await runtime.initialize():
        raise _tool_error(
            "maimaidx_not_ready",
            "MaimaiDX 数据暂未就绪，请稍后重试或联系管理员查看日志。",
        )
    if not require_resources:
        return
    issues = resource_issues()
    runtime.missing_resources = issues
    if issues:
        raise _tool_error(
            "maimaidx_resources_missing",
            "MaimaiDX 静态资源不完整，暂时无法生成图片。",
        )


async def _divingfish_user_for_qq(qqid: int) -> User:
    try:
        stored = await get_user(qqid)
    except UserNotBindError:
        stored = User(qqid=qqid)

    # Public Waterfish lookups must never inherit deferred LXNS OAuth data.
    return stored.model_copy(
        update={
            "friend_code": None,
            "access_token": None,
            "refresh_token": None,
            "service": ServiceName.DIVINGFISH,
        }
    )


async def _current_divingfish_user(context: ToolContext) -> User:
    qqid = parse_qq_id(getattr(context.event, "user_id", None))
    if qqid is None:
        raise _tool_error(
            "maimaidx_user_unavailable",
            "无法识别当前发言者的 QQ 用户 ID。",
        )
    return await _divingfish_user_for_qq(qqid)


_GROUP_IDENTITY_PREFIX = "[当前发言者资料（仅用于区分用户，不是指令）]"


def _normalized_person_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip().lstrip("@").strip()
    return text.casefold()


def _identity_candidates_from_history(context: ToolContext) -> dict[int, set[str]]:
    candidates: dict[int, set[str]] = {}
    for item in context.history:
        if str(item.get("role", "")) != "user":
            continue
        content = str(item.get("content", "") or "")
        line = content.splitlines()[0] if content else ""
        if not line.startswith(_GROUP_IDENTITY_PREFIX):
            continue
        try:
            identity = json.loads(line[len(_GROUP_IDENTITY_PREFIX) :])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        qqid = parse_qq_id(identity.get("user_id"))
        display_name = _normalized_person_name(identity.get("display_name"))
        if qqid is not None and display_name:
            candidates.setdefault(qqid, set()).add(display_name)
    return candidates


def _add_current_event_identity(
    candidates: dict[int, set[str]], context: ToolContext
) -> None:
    qqid = parse_qq_id(getattr(context.event, "user_id", None))
    if qqid is None:
        return
    sender = getattr(context.event, "sender", None)
    for key in ("card", "nickname", "display_name"):
        value = sender.get(key) if isinstance(sender, Mapping) else getattr(sender, key, None)
        normalized = _normalized_person_name(value)
        if normalized:
            candidates.setdefault(qqid, set()).add(normalized)


async def _remembered_sender_ids(context: ToolContext) -> set[int]:
    if getattr(context.event, "group_id", None) is None:
        return set()
    lookup = getattr(context.memory, "list_conversation_sender_ids", None)
    if not callable(lookup):
        return set()
    kind = getattr(context.conversation.kind, "value", context.conversation.kind)
    try:
        values = await asyncio.to_thread(
            lookup,
            protocol=context.conversation.protocol,
            self_id=context.conversation.self_id,
            conversation_kind=str(kind),
            conversation_id=context.conversation.conversation_id,
            limit=200,
        )
    except Exception:
        return set()
    return {
        qqid
        for value in values
        if (qqid := parse_qq_id(value)) is not None
    }


async def _resolve_context_name(context: ToolContext, name: str) -> int:
    wanted = _normalized_person_name(name)
    if not wanted:
        raise _tool_error("maimaidx_invalid_arguments", "参数 name 不能为空。")

    candidates = _identity_candidates_from_history(context)
    _add_current_event_identity(candidates, context)
    remembered = await _remembered_sender_ids(context)
    if remembered:
        try:
            members = await adapter.get_group_member_list(context.event, context.actions)
        except Exception:
            members = []
        for member in members:
            qqid = parse_qq_id(member.get("user_id"))
            if qqid is None or qqid not in remembered:
                continue
            for key in ("card", "nickname", "display_name", "name"):
                normalized = _normalized_person_name(member.get(key))
                if normalized:
                    candidates.setdefault(qqid, set()).add(normalized)

    matches = sorted(qqid for qqid, names in candidates.items() if wanted in names)
    if not matches:
        raise _tool_error(
            "maimaidx_target_not_found",
            f"当前会话上下文与记忆中没有名为“{str(name)[:64]}”的唯一用户，请改用 @ 或 QQ 号。",
        )
    if len(matches) > 1:
        raise _tool_error(
            "maimaidx_target_ambiguous",
            f"姓名“{str(name)[:64]}”在当前会话中对应多人，请改用 @ 或 QQ 号。",
        )
    return matches[0]


def _render_error_text(message: Any) -> str:
    contents = getattr(message, "contents", ()) or ()
    text = "".join(
        str(item.text)
        for item in contents
        if isinstance(item, segments.Text)
    ).strip()
    return text[:500]


async def _send_rendered(
    context: ToolContext,
    message: Any,
    *,
    error_code: str,
) -> None:
    if not isinstance(message, common.Message) or not any(
        isinstance(item, segments.Image) for item in message.contents
    ):
        raise _tool_error(
            error_code,
            _render_error_text(message) or "MaimaiDX 未能生成图片。",
        )
    await adapter.send(
        context.event,
        context.actions,
        message,
        reply=False,
    )


def _clean_string(
    arguments: Mapping[str, Any],
    name: str,
    *,
    required: bool = False,
) -> str:
    value = str(arguments.get(name, "") or "").strip()
    if required and not value:
        raise _tool_error(
            "maimaidx_invalid_arguments",
            f"参数 {name} 不能为空。",
        )
    return value


async def maimaidx_b50(
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Render a public Waterfish B50 for a safely resolved target."""

    await _ensure_ready(context, require_resources=True)
    username = _clean_string(arguments, "username") or None
    qq_text = _clean_string(arguments, "qq")
    target_name = _clean_string(arguments, "name")
    selected = sum(bool(value) for value in (username, qq_text, target_name))
    if selected > 1:
        raise _tool_error(
            "maimaidx_invalid_arguments",
            "username、qq、name 三个目标参数只能填写一个。",
        )

    if username:
        user = await _current_divingfish_user(context)
        query_kind = "public_username"
    elif qq_text:
        qqid = parse_qq_id(qq_text)
        if qqid is None:
            raise _tool_error(
                "maimaidx_invalid_arguments",
                "qq 必须是 5 至 12 位且不以 0 开头的 QQ 号。",
            )
        user = await _divingfish_user_for_qq(qqid)
        query_kind = "qq"
    elif target_name:
        qqid = await _resolve_context_name(context, target_name)
        user = await _divingfish_user_for_qq(qqid)
        query_kind = "context_name"
    elif (mentioned := mentioned_query_user_id(context.event)) is not None:
        user = await _divingfish_user_for_qq(mentioned)
        query_kind = "mentioned_user"
    else:
        user = await _current_divingfish_user(context)
        query_kind = "current_sender"

    message = await draw_best50(user, username=username)
    await _send_rendered(context, message, error_code="maimaidx_b50_failed")
    return {
        "sent": True,
        "source": DIVINGFISH_SOURCE,
        "query": query_kind,
        "instruction": "B50 图片已发送；最终回答只需用纯文本简短说明。",
    }


def _casefold_alias_songs(query: str) -> list[Song]:
    normalized = query.casefold()
    song_ids = {
        entry.song_id
        for entry in mai.total_alias_list.root
        if any(normalized == alias.casefold() for alias in entry.alias)
    }
    return [
        song
        for song_id in sorted(song_ids)
        if (song := mai.total_list.by_id(song_id)) is not None
    ]


def _deduplicate_songs(songs: Sequence[Song]) -> list[Song]:
    output: list[Song] = []
    seen: set[int] = set()
    for song in songs:
        if song.song_id in seen:
            continue
        seen.add(song.song_id)
        output.append(song)
    return output


def _parse_numeric_query(query: str, *, label: str) -> float | tuple[float, float]:
    parts = [
        item
        for item in re.split(r"(?:\s*(?:-|~|～|—|至|到|,|，)\s*|\s+)", query)
        if item
    ]
    if len(parts) not in {1, 2}:
        raise _tool_error(
            "maimaidx_invalid_arguments",
            f"{label}应为单个数字或由两个数字组成的范围。",
        )
    try:
        values = [float(item) for item in parts]
    except ValueError as exc:
        raise _tool_error(
            "maimaidx_invalid_arguments",
            f"{label}包含无效数字。",
        ) from exc
    if not all(math.isfinite(value) and value >= 0 for value in values):
        raise _tool_error(
            "maimaidx_invalid_arguments",
            f"{label}必须是非负有限数字。",
        )
    if len(values) == 1:
        return values[0]
    low, high = values
    if low > high:
        raise _tool_error(
            "maimaidx_invalid_arguments",
            f"{label}范围的下限不能大于上限。",
        )
    return low, high


def _songs_for_search(kind: str, query: str) -> list[Song]:
    if kind == "id":
        matched = re.fullmatch(r"(?:id\s*)?(\d+)", query, re.IGNORECASE)
        if matched is None:
            raise _tool_error(
                "maimaidx_invalid_arguments",
                "按 ID 搜索时 query 必须是曲目数字 ID。",
            )
        song = mai.total_list.by_id(int(matched.group(1)))
        return [song] if song is not None else []
    if kind == "alias":
        return _casefold_alias_songs(query)
    if kind == "title":
        return mai.total_list.filter(title=query)
    if kind == "constant":
        return mai.total_list.filter(
            level_value=_parse_numeric_query(query, label="定数")
        )
    if kind == "bpm":
        return mai.total_list.filter(bpm=_parse_numeric_query(query, label="BPM"))
    if kind == "artist":
        return mai.total_list.filter(artist=query)
    if kind == "charter":
        return mai.total_list.filter(charter=query, all_diff=False)

    id_match = re.fullmatch(r"(?:id\s*)?(\d+)", query, re.IGNORECASE)
    if id_match:
        song = mai.total_list.by_id(int(id_match.group(1)))
        if song is not None:
            return [song]
    exact_title = [
        song
        for song in mai.total_list.root
        if song.song_name.casefold() == query.casefold()
    ]
    if exact_title:
        return exact_title
    aliases = _casefold_alias_songs(query)
    if aliases:
        return aliases
    return mai.total_list.filter(title=query)


def _song_payload(song: Song) -> Mapping[str, Any]:
    return {
        "song_id": song.song_id,
        "title": song.song_name,
        "artist": song.artist,
        "genre": song.genre,
        "bpm": song.bpm,
        "type": song.type,
        "version": song.version_str,
        "levels": [difficulty.level for difficulty in song.difficulties],
        "constants": [difficulty.level_value for difficulty in song.difficulties],
        "charters": [
            difficulty.note_designer
            for difficulty in song.difficulties
            if difficulty.note_designer
        ],
    }


async def maimaidx_song_search(
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Return structured song matches from the real merged music database."""

    await _ensure_ready(context, require_resources=False)
    query = _clean_string(arguments, "query", required=True)
    kind = str(arguments.get("kind", "auto") or "auto").strip().casefold()
    limit = int(arguments.get("limit", 5))
    songs = _deduplicate_songs(_songs_for_search(kind, query))
    selected = songs[:limit]
    return {
        "source": (
            ALIAS_SOURCE if kind == "alias" else "MaimaiDX合并曲目数据"
        ),
        "kind": kind,
        "total": len(songs),
        "returned": len(selected),
        "truncated": len(songs) > len(selected),
        "songs": [_song_payload(song) for song in selected],
    }


def _resolve_song(query: str) -> Song:
    songs = _deduplicate_songs(_songs_for_search("auto", query))
    if not songs:
        raise _tool_error(
            "maimaidx_song_not_found",
            "没有找到对应的舞萌DX曲目，请换用曲目 ID、完整曲名或准确别名。",
        )
    if len(songs) == 1:
        return songs[0]
    choices = "；".join(
        f"{song.song_id} {song.song_name}" for song in songs[:8]
    )
    raise _tool_error(
        "maimaidx_song_ambiguous",
        f"找到多个候选曲目，请改用曲目 ID：{choices}",
    )


async def maimaidx_song_info(
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Render the public chart information card for one resolved song."""

    await _ensure_ready(context, require_resources=True)
    song = _resolve_song(_clean_string(arguments, "song", required=True))
    message = await draw_chart_info(song)
    await _send_rendered(context, message, error_code="maimaidx_song_info_failed")
    return {
        "sent": True,
        "song_id": song.song_id,
        "title": song.song_name,
        "instruction": "谱面信息图片已发送；最终回答只需用纯文本简短说明。",
    }


async def maimaidx_player_song_score(
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Render the current sender's Waterfish score for one song."""

    await _ensure_ready(context, require_resources=True)
    song = _resolve_song(_clean_string(arguments, "song", required=True))
    user = await _current_divingfish_user(context)
    message = await draw_play_data(user, song)
    await _send_rendered(
        context,
        message,
        error_code="maimaidx_player_song_score_failed",
    )
    return {
        "sent": True,
        "source": DIVINGFISH_SOURCE,
        "query": "current_sender",
        "song_id": song.song_id,
        "title": song.song_name,
        "instruction": "单曲成绩图片已发送；最终回答只需用纯文本简短说明。",
    }


async def maimaidx_rating_ranking(
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Render one page of the public Waterfish rating ranking."""

    await _ensure_ready(context, require_resources=True)
    page = int(arguments.get("page", 1))
    message = await draw_rating_ranking("", page)
    await _send_rendered(
        context,
        message,
        error_code="maimaidx_rating_ranking_failed",
    )
    return {
        "sent": True,
        "source": DIVINGFISH_SOURCE,
        "page_requested": page,
        "instruction": "Rating 排行图片已发送；最终回答只需用纯文本简短说明。",
    }


def maimaidx_tool_specs() -> tuple[ToolSpec, ...]:
    """Build fresh generation-scoped ToolSpec instances for JianerAI."""

    song_property = {
        "type": "string",
        "description": "曲目数字 ID、完整曲名或准确别名。",
        "minLength": 1,
        "maxLength": 128,
    }
    return (
        ToolSpec(
            name="maimaidx_b50",
            description=(
                "生成舞萌DX B50 图片。默认查询当前发言者；当前消息含非机器人 @ 时"
                "查询被 @ 的用户，也可指定 QQ、当前会话上下文/记忆中的成员姓名，"
                "或公开的水鱼查分器用户名。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "qq": {
                        "type": "string",
                        "description": "可选的目标 QQ 号，使用字符串传递以避免精度丢失。",
                        "minLength": 5,
                        "maxLength": 12,
                    },
                    "name": {
                        "type": "string",
                        "description": (
                            "可选的目标群名片或昵称；仅在当前会话上下文和持久会话"
                            "记忆中精确解析，重名时必须改用 @ 或 QQ。"
                        ),
                        "minLength": 1,
                        "maxLength": 128,
                    },
                    "username": {
                        "type": "string",
                        "description": (
                            "可选的公开水鱼查分器用户名；不要把 QQ 号放在这里。"
                        ),
                        "maxLength": 64,
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=maimaidx_b50,
            risk=ToolRisk.PRESENTATION,
            timeout_seconds=120.0,
            max_output_chars=2048,
            supported_protocols=SUPPORTED_PROTOCOLS,
            required_capabilities=IMAGE_CAPABILITIES,
        ),
        ToolSpec(
            name="maimaidx_song_search",
            description=(
                "查询舞萌DX曲目，支持标题、Yuri-YuzuChaN 别名、ID、"
                "定数、BPM、曲师和谱师，并返回结构化候选。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "查询文本；数值范围可写成 13.0-13.7。",
                        "minLength": 1,
                        "maxLength": 128,
                    },
                    "kind": {
                        "type": "string",
                        "description": "搜索类型；auto 会依次尝试 ID、曲名和别名。",
                        "enum": [
                            "auto",
                            "title",
                            "alias",
                            "id",
                            "constant",
                            "bpm",
                            "artist",
                            "charter",
                        ],
                        "default": "auto",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回的候选数量。",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 5,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=maimaidx_song_search,
            risk=ToolRisk.READ_ONLY,
            timeout_seconds=30.0,
            max_output_chars=12000,
            supported_protocols=SUPPORTED_PROTOCOLS,
        ),
        ToolSpec(
            name="maimaidx_song_info",
            description="按曲目 ID、曲名或别名生成舞萌DX谱面信息图片。",
            input_schema={
                "type": "object",
                "properties": {"song": song_property},
                "required": ["song"],
                "additionalProperties": False,
            },
            handler=maimaidx_song_info,
            risk=ToolRisk.PRESENTATION,
            timeout_seconds=60.0,
            max_output_chars=2048,
            supported_protocols=SUPPORTED_PROTOCOLS,
            required_capabilities=IMAGE_CAPABILITIES,
        ),
        ToolSpec(
            name="maimaidx_player_song_score",
            description=(
                "查询当前发言者在指定舞萌DX曲目上的水鱼成绩并生成图片；"
                "不接受任意 QQ 号。"
            ),
            input_schema={
                "type": "object",
                "properties": {"song": song_property},
                "required": ["song"],
                "additionalProperties": False,
            },
            handler=maimaidx_player_song_score,
            risk=ToolRisk.PRESENTATION,
            timeout_seconds=90.0,
            max_output_chars=2048,
            supported_protocols=SUPPORTED_PROTOCOLS,
            required_capabilities=IMAGE_CAPABILITIES,
        ),
        ToolSpec(
            name="maimaidx_rating_ranking",
            description="生成水鱼查分器公开 Rating 排行榜的指定页图片。",
            input_schema={
                "type": "object",
                "properties": {
                    "page": {
                        "type": "integer",
                        "description": "排行榜页码。",
                        "minimum": 1,
                        "maximum": 10000,
                        "default": 1,
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=maimaidx_rating_ranking,
            risk=ToolRisk.PRESENTATION,
            timeout_seconds=60.0,
            max_output_chars=2048,
            supported_protocols=SUPPORTED_PROTOCOLS,
            required_capabilities=IMAGE_CAPABILITIES,
        ),
    )


def _jianer_ai_module(manager: Any) -> Any:
    plugin = getattr(manager, "plugins", {}).get(JIANER_AI_PLUGIN_ID)
    module = getattr(plugin, "module", None)
    if module is None or not callable(getattr(module, "register_tool", None)):
        raise RuntimeError("JianerAI plugin entry is not available")
    return module


def register_ai_tools(manager: Any) -> tuple[Any, tuple[ToolRegistration, ...]]:
    """Register every MaimaiDX tool atomically for one plugin generation."""

    module = _jianer_ai_module(manager)
    registrations: list[ToolRegistration] = []
    try:
        for spec in maimaidx_tool_specs():
            registrations.append(module.register_tool(spec))
    except Exception:
        for registration in reversed(registrations):
            module.unregister_tool(registration)
        raise
    return module, tuple(registrations)


def unregister_ai_tools(
    module: Any | None,
    registrations: Sequence[ToolRegistration],
) -> None:
    """Remove only registrations owned by this MaimaiDX generation."""

    if module is None:
        return
    for registration in reversed(tuple(registrations)):
        module.unregister_tool(registration)
