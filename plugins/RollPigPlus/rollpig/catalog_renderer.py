from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import math
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import AsyncIterator

from ..host import logger

from .config import plugin_config
from .catalog_pillow_renderer import (
    CatalogCard,
    CatalogData,
    CatalogFavorite,
    CatalogStats,
    CatalogRenderer,
    clear_catalog_pillow_caches,
)
from .resource_manager import pig_resource_manager
from .helpers import log_perf
from .runtime import ROLLPIG_TIMEZONE, rollpig_today
from .store.models import MAX_EXPERT_LEVEL, CatalogSnapshot, DrawState, PigProgress


RESOURCE_DIR = Path(__file__).parent / "resource"


# ================================ 图鉴渲染总预算 ================================ #
# Pillow 在线程中执行；并发预算用于限制同时存在的超采样 RGBA 画布数量，
# 避免多人同时请求图鉴时出现明显的瞬时内存峰值。

_catalog_render_semaphore: asyncio.Semaphore | None = None
_catalog_render_limit: int | None = None
_catalog_render_lock = asyncio.Lock()


def _resolve_catalog_render_limit() -> int:
    try:
        raw_limit = plugin_config.rollpig_catalog_render_concurrency
    except Exception as error:
        logger.warning(f"rollpig_catalog_render_concurrency 配置读取失败，已回退到 2: {error}")
        raw_limit = 2

    try:
        return max(1, min(6, int(raw_limit or 2)))
    except (TypeError, ValueError):
        logger.warning(f"rollpig_catalog_render_concurrency 配置非法，已回退到 2: {raw_limit}")
        return 2


async def _get_catalog_render_semaphore() -> asyncio.Semaphore:
    global _catalog_render_limit, _catalog_render_semaphore
    limit = _resolve_catalog_render_limit()
    async with _catalog_render_lock:
        if _catalog_render_semaphore is None or _catalog_render_limit != limit:
            _catalog_render_semaphore = asyncio.Semaphore(limit)
            _catalog_render_limit = limit
    return _catalog_render_semaphore


@asynccontextmanager
async def catalog_render_budget(_label: str) -> AsyncIterator[None]:
    """进入图鉴共享渲染预算；异常和超时都必须释放 semaphore。"""

    semaphore = await _get_catalog_render_semaphore()
    await semaphore.acquire()
    try:
        yield
    finally:
        semaphore.release()


CATALOG_BASE_IMAGE = RESOURCE_DIR / "catalog_base.jpg"
CATALOG_FONT = RESOURCE_DIR / "fonts" / "SourceHanSansSC-Medium.otf"
CATALOG_PAGE_SIZE = 38
CATALOG_CACHE_MAX_ENTRIES = 64
CATALOG_CACHE_MAX_BYTES = 64 * 1024 * 1024
NEW_BADGE_DAYS = 7


@dataclass
class _CachedCatalogImage:
    created_at: float
    payload: bytes


# ================================ 图鉴缓存状态 ================================ #

_catalog_cache: dict[str, _CachedCatalogImage] = {}
_catalog_cache_lock = asyncio.Lock()
_catalog_render_tasks: dict[str, asyncio.Task[bytes]] = {}


async def shutdown_catalog_renderer() -> None:
    """释放 Pillow LRU，避免 Bot 重载后残留图鉴常驻资源。"""

    _get_catalog_renderer.cache_clear()
    clear_catalog_pillow_caches()


# ================================ 图鉴结果缓存 ================================ #

def _catalog_cache_bytes() -> int:
    return sum(len(cached.payload) for cached in _catalog_cache.values())


def _prune_catalog_cache(*, ttl: int, now: float) -> None:
    """清理图片结果缓存；TTL 控制新鲜度，硬上限防止多用户短时生成导致内存膨胀。"""
    if not _catalog_cache:
        return
    if ttl <= 0:
        _catalog_cache.clear()
        return

    expired_keys = [key for key, cached in _catalog_cache.items() if now - cached.created_at > ttl]
    for key in expired_keys:
        _catalog_cache.pop(key, None)

    overflow = len(_catalog_cache) - CATALOG_CACHE_MAX_ENTRIES
    if overflow > 0:
        oldest_keys = sorted(_catalog_cache, key=lambda key: _catalog_cache[key].created_at)[:overflow]
        for key in oldest_keys:
            _catalog_cache.pop(key, None)

    # PNG 图鉴单张可达数 MB，仅限制条数不足以约束内存；总字节超限时继续按最老优先淘汰。
    cache_bytes = _catalog_cache_bytes()
    for key in sorted(_catalog_cache, key=lambda key: _catalog_cache[key].created_at):
        if cache_bytes <= CATALOG_CACHE_MAX_BYTES:
            break
        cached = _catalog_cache.pop(key, None)
        if cached is not None:
            cache_bytes -= len(cached.payload)


def _get_catalog_cache_locked(cache_key: str, *, ttl: int, now: float) -> bytes | None:
    """读取图鉴缓存；调用方必须持有 _catalog_cache_lock，避免并发淘汰时字典变化。"""
    _prune_catalog_cache(ttl=ttl, now=now)
    cached = _catalog_cache.get(cache_key)
    if cached and ttl > 0 and now - cached.created_at <= ttl:
        return cached.payload
    return None


def _store_catalog_cache_locked(cache_key: str, payload: bytes, *, ttl: int, now: float) -> None:
    """写入图鉴缓存；调用方必须持有 _catalog_cache_lock，并在写入后立即按上限淘汰。"""
    if ttl <= 0:
        return
    _catalog_cache[cache_key] = _CachedCatalogImage(created_at=now, payload=payload)
    _prune_catalog_cache(ttl=ttl, now=now)


def _parse_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    normalized = str(value).replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(ROLLPIG_TIMEZONE).replace(tzinfo=None)
    return parsed


def _is_recent_new(first_obtained_at: str | None, *, today: dt.date) -> bool:
    obtained_at = _parse_datetime(first_obtained_at)
    if obtained_at is None:
        return False
    return 0 <= (today - obtained_at.date()).days < NEW_BADGE_DAYS


def _calculate_checkin_streak(recent_rolls: dict[str, str], *, today: dt.date) -> int:
    """从今天往前数连续抽猪天数；图鉴只展示已有记录，不补写任何状态。"""
    streak = 0
    for offset in range(60):
        date_str = (today - dt.timedelta(days=offset)).isoformat()
        if date_str not in recent_rolls:
            break
        streak += 1
    return streak


def _next_milestone(unlocked: int, total: int) -> int:
    if total <= 0:
        return 0
    if unlocked >= total:
        return total
    return min(total, max(10, ((unlocked // 10) + 1) * 10))


def _sort_progress_items(draw_state: DrawState) -> list[tuple[str, PigProgress]]:
    resource_order = {str(pig.get("id")): index for index, pig in enumerate(pig_resource_manager.pig_list)}
    return sorted(
        draw_state.progress.items(),
        key=lambda item: (
            -item[1].expert_level,
            -int(item[1].copies or 0),
            item[1].first_obtained_at or "",
            resource_order.get(item[0], 10**9),
            item[0],
        ),
    )


def _favorite_progress_item(draw_state: DrawState) -> tuple[str, PigProgress] | None:
    """本命猪只按真实抽取次数排序；加餐成长不能伪造玩家最常抽到的猪。"""

    if not draw_state.progress:
        return None
    return min(
        draw_state.progress.items(),
        key=lambda item: (
            -int(item[1].copies or 0),
            item[1].first_obtained_at or "",
            item[0],
        ),
    )


def _build_catalog_data(
    *,
    user_name: str,
    snapshot: CatalogSnapshot,
    page: int,
) -> CatalogData:
    """从只读业务快照构造当前页图鉴数据，不写回抽猪状态。"""

    today = rollpig_today()
    page_size = CATALOG_PAGE_SIZE
    progress_items = _sort_progress_items(snapshot.draw_state)
    total_pigs = len(pig_resource_manager.pig_list)
    unlocked = len(snapshot.draw_state.pig_ids)
    pages = max(1, math.ceil(max(1, unlocked) / page_size))
    page = max(1, min(page, pages))
    start = (page - 1) * page_size
    page_items = progress_items[start : start + page_size]

    cards: list[CatalogCard] = []
    for pig_id, progress in page_items:
        pig = pig_resource_manager.pig_map.get(pig_id) or {"id": pig_id}
        level = progress.expert_level
        appearance = pig_resource_manager.resolve_pig_appearance(pig, level)
        is_max = level >= MAX_EXPERT_LEVEL
        is_new = (not is_max) and _is_recent_new(progress.first_obtained_at, today=today)
        badge = "MAX" if is_max else ("NEW" if is_new else "")
        cards.append(
            CatalogCard(
                pig_id=pig_id,
                name=str(pig.get("name") or pig_id),
                image_path=appearance.image_path,
                fallback_image_path=appearance.base_image_path,
                level=level,
                badge=badge,
            )
        )

    favorite_item = _favorite_progress_item(snapshot.draw_state)
    if favorite_item is not None:
        favorite_id, favorite_progress = favorite_item
        favorite_pig = pig_resource_manager.pig_map.get(favorite_id) or {"id": favorite_id}
        favorite_appearance = pig_resource_manager.resolve_pig_appearance(
            favorite_pig,
            favorite_progress.expert_level,
        )
        favorite = CatalogFavorite(
            name=str(favorite_pig.get("name") or favorite_id),
            image_path=favorite_appearance.image_path,
            fallback_image_path=favorite_appearance.base_image_path,
            level=favorite_progress.expert_level,
            copies=int(favorite_progress.copies or 0),
        )
    else:
        favorite = CatalogFavorite()

    levels = [progress.expert_level for _, progress in progress_items]
    recent_new_count = sum(
        1
        for _, progress in progress_items
        if _is_recent_new(progress.first_obtained_at, today=today)
    )
    progress_percent = round((unlocked / total_pigs) * 100, 1) if total_pigs > 0 else 0.0
    stats = CatalogStats(
        unlocked=unlocked,
        total=total_pigs,
        progress_percent=progress_percent,
        max_level=max(levels) if levels else 0,
        maxed_count=sum(1 for level in levels if level >= MAX_EXPERT_LEVEL),
        recent_new_count=recent_new_count,
        checkin_streak=_calculate_checkin_streak(snapshot.recent_rolls, today=today),
        roasted_7d=int(snapshot.roasted_7d or 0),
        next_milestone=_next_milestone(unlocked, total_pigs),
        page=page,
        pages=pages,
    )
    return CatalogData(
        user_name=user_name,
        stats=stats,
        cards=tuple(cards),
        favorite=favorite,
    )


def _build_cache_key(data: CatalogData, snapshot: CatalogSnapshot) -> str:
    """缓存指纹只使用状态摘要，不把图片二进制塞进 key，避免内存膨胀。"""

    stats = data.stats
    favorite = data.favorite
    key_payload = {
        "resource_version": pig_resource_manager.resource_version,
        # 页码必须使用业务层钳制后的实际页，避免 999/1000 等请求重复缓存同一张末页图片。
        "page": stats.page,
        "page_size": CATALOG_PAGE_SIZE,
        "user_name": data.user_name,
        "stats": (
            stats.unlocked,
            stats.total,
            stats.progress_percent,
            stats.max_level,
            stats.maxed_count,
            stats.recent_new_count,
            stats.checkin_streak,
            stats.roasted_7d,
            stats.next_milestone,
            stats.page,
            stats.pages,
        ),
        "cards": [
            (
                card.pig_id,
                card.level,
                card.badge,
                str(card.image_path or ""),
                str(card.fallback_image_path or ""),
            )
            for card in data.cards
        ],
        "favorite": (
            favorite.name,
            str(favorite.image_path or ""),
            str(favorite.fallback_image_path or ""),
            favorite.level,
            favorite.copies,
        ),
        "recent_rolls": snapshot.recent_rolls,
        "roasted_7d": snapshot.roasted_7d,
    }
    raw = json.dumps(key_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ================================ Pillow 图鉴渲染 ================================ #


def _resolve_catalog_scale_factor() -> float:
    try:
        raw_scale = float(plugin_config.rollpig_catalog_scale_factor or 2.0)
    except (TypeError, ValueError):
        logger.warning("rollpig_catalog_scale_factor 配置非法，已回退到 2")
        return 2.0
    return max(1.0, min(3.0, raw_scale))


@lru_cache(maxsize=6)
def _get_catalog_renderer(scale_factor: float) -> CatalogRenderer:
    """按超采样倍率复用无状态渲染器；底图、字体和贴片缓存由模块级 LRU 管理。"""

    return CatalogRenderer(
        CATALOG_BASE_IMAGE,
        CATALOG_FONT,
        scale_factor=scale_factor,
    )


def _render_catalog_sync(
    data: CatalogData,
    *,
    output_format: str,
    scale_factor: float,
) -> bytes:
    """在线程工作函数中执行纯 Pillow 渲染；不得读取或修改 NoneBot 运行状态。"""

    renderer = _get_catalog_renderer(scale_factor)
    return renderer.render(data, output_format=output_format)


def _normalize_output_format(raw_format: str) -> str:
    """图鉴默认坚持 PNG；允许显式切到 JPEG 时统一收敛到 Pillow 可识别格式。"""
    value = str(raw_format or "png").strip().lower()
    if value in {"jpg", "jpeg"}:
        return "JPEG"
    return "PNG"


async def _render_catalog(
    data: CatalogData,
    *,
    output_format: str,
    scale_factor: float,
) -> bytes:
    """在共享预算内把 CPU 绘图移到工作线程，避免阻塞 NoneBot 事件循环。"""

    wait_started_at = time.perf_counter()
    async with catalog_render_budget("catalog-pillow"):
        acquired_at = time.perf_counter()
        result = await asyncio.to_thread(
            _render_catalog_sync,
            data,
            output_format=output_format,
            scale_factor=scale_factor,
        )
    finished_at = time.perf_counter()
    log_perf(
        "rollpig catalog pillow stage: "
        f"wait={acquired_at - wait_started_at:.2f}s "
        f"render={finished_at - acquired_at:.2f}s bytes={len(result)}"
    )
    return result


async def _render_catalog_image_uncached(
    *,
    user_name: str,
    page: int,
    data: CatalogData,
    cache_key: str,
    ttl: int,
    output_format: str,
    scale_factor: float,
    started_at: float,
    data_ready_at: float,
) -> bytes:
    """执行一次真实渲染，并在成功后写入有界结果缓存。"""

    result = await _render_catalog(
        data,
        output_format=output_format,
        scale_factor=scale_factor,
    )

    finished_at = time.perf_counter()
    log_perf(
        f"rollpig catalog rendered: user={user_name} page={page} "
        f"data={data_ready_at - started_at:.2f}s "
        f"total={finished_at - started_at:.2f}s bytes={len(result)}"
    )
    async with _catalog_cache_lock:
        _store_catalog_cache_locked(cache_key, result, ttl=ttl, now=time.time())
    return result


async def render_catalog_image(
    *,
    user_name: str,
    snapshot: CatalogSnapshot,
    page: int = 1,
) -> bytes:
    """渲染图片版小猪图鉴；只读取快照，不修改抽猪状态或 copies。"""
    started_at = time.perf_counter()
    data = _build_catalog_data(user_name=user_name, snapshot=snapshot, page=page)
    resolved_page = data.stats.page
    data_ready_at = time.perf_counter()
    output_format = _normalize_output_format(plugin_config.rollpig_catalog_output_format)
    scale_factor = _resolve_catalog_scale_factor()
    cache_key = (
        f"pillow:{scale_factor}:{output_format}:"
        f"{_build_cache_key(data, snapshot)}"
    )
    ttl = max(0, int(plugin_config.rollpig_catalog_cache_seconds or 0))

    # ================================ 图鉴同键合流 ================================ #
    # 多群同时请求同一页时，只让第一个请求真正渲染；其余请求等待同一个 task。
    render_owner = False
    async with _catalog_cache_lock:
        cached_payload = _get_catalog_cache_locked(cache_key, ttl=ttl, now=time.time())
        if cached_payload is not None:
            log_perf(
                f"rollpig catalog cache hit: user={user_name} page={resolved_page} "
                f"data={data_ready_at - started_at:.2f}s bytes={len(cached_payload)}"
            )
            return cached_payload

        render_task = _catalog_render_tasks.get(cache_key)
        if render_task is None or render_task.done():
            render_task = asyncio.create_task(
                _render_catalog_image_uncached(
                    user_name=user_name,
                    page=resolved_page,
                    data=data,
                    cache_key=cache_key,
                    ttl=ttl,
                    output_format=output_format,
                    scale_factor=scale_factor,
                    started_at=started_at,
                    data_ready_at=data_ready_at,
                )
            )
            _catalog_render_tasks[cache_key] = render_task
            render_owner = True

    if not render_owner:
        log_perf(
            f"rollpig catalog render coalesced: user={user_name} page={resolved_page} "
            f"data={data_ready_at - started_at:.2f}s"
        )

    try:
        return await render_task
    finally:
        if render_owner:
            async with _catalog_cache_lock:
                if _catalog_render_tasks.get(cache_key) is render_task:
                    _catalog_render_tasks.pop(cache_key, None)
