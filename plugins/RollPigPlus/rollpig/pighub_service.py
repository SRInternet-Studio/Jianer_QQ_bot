from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx
from ..host import logger
from ..host import localstore


PIGHUB_IMAGE_BASE_URL = "https://pighub.top/data/"
PIGHUB_ORIGIN = "https://pighub.top/"
PIGHUB_API_URLS = (
    "https://pighub.top/api/images?sort=2&limit=200",
    "https://pighub.top/api/images?sort=2",
    "https://pighub.top/api/all-images",
)
PIGHUB_CACHE_TTL_SECONDS = 12 * 3600
PIGHUB_REFRESH_RETRY_SECONDS = 10 * 60
PIGHUB_REFRESH_INTERVAL_HOURS = 12
PIGHUB_STARTUP_REFRESH_DELAY_SECONDS = (60, 300)
PIGHUB_HTTP_TIMEOUT_SECONDS = 10.0
PIGHUB_USER_AGENT = "RollPig-Plus/0.8 (+https://github.com/Felis2026/nonebot-plugin-rollpig-plus)"
PIGHUB_CACHE_FILE = localstore.get_plugin_cache_dir() / "pighub_images.json"


class PigHubService:
    """PigHub 图片索引服务；只缓存列表元数据，不下载图片本体。"""

    def __init__(self, cache_file: Path = PIGHUB_CACHE_FILE) -> None:
        self.cache_file = cache_file
        self.images: list[dict[str, Any]] = []
        self.last_loaded: float = 0.0
        self.last_refresh_attempt: float = 0.0
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[bool] | None = None
        self._startup_task: asyncio.Task[None] | None = None
        self._load_cache_sync()

    # ================================ 缓存读写 ================================ #
    # PigHub 是外部站点，缓存的是 API 返回的图片索引，避免每次命令都打接口。
    def _load_cache_sync(self) -> None:
        if not self.cache_file.exists():
            return
        try:
            payload = json.loads(self.cache_file.read_text(encoding="utf-8-sig"))
            raw_images = payload.get("images")
            cached_at = float(payload.get("cached_at") or 0)
            if not isinstance(raw_images, list):
                raise ValueError("PigHub 缓存缺少 images 列表")
            images = [item for item in (normalize_pighub_image_item(item) for item in raw_images) if item]
            if not images:
                raise ValueError("PigHub 缓存为空")
            self.images = images
            self.last_loaded = cached_at or time.time()
            logger.info(f"PigHub 本地索引缓存已加载: images={len(images)}")
        except Exception as error:
            logger.warning(f"PigHub 本地索引缓存读取失败，稍后将尝试重新刷新: {error}")

    def _save_cache_sync(self) -> None:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cached_at": int(self.last_loaded),
            "images": self.images,
        }
        tmp = self.cache_file.with_suffix(f"{self.cache_file.suffix}.{id(self)}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(self.cache_file)

    # ================================ 刷新调度 ================================ #
    def is_fresh(self, now: float | None = None) -> bool:
        now = now or time.time()
        return bool(self.images and (now - self.last_loaded) < PIGHUB_CACHE_TTL_SECONDS)

    def is_retry_cooling_down(self, now: float | None = None) -> bool:
        now = now or time.time()
        return (now - self.last_refresh_attempt) < PIGHUB_REFRESH_RETRY_SECONDS

    async def ensure_ready(self) -> bool:
        """
        确保命令侧有可用索引。

        有旧缓存时直接返回，并在过期时后台刷新；完全没有缓存时才等待一次真实刷新。
        """

        if self.is_fresh():
            return True

        async with self._lock:
            if self.is_fresh():
                return True
            if self.images:
                self._ensure_refresh_task("stale-cache")
                return True
            if self.is_retry_cooling_down():
                return False
            task = self._ensure_refresh_task("first-load")

        return await task

    async def refresh(self, reason: str = "manual") -> bool:
        """刷新 PigHub 索引；失败时保留最后一次成功缓存。"""

        async with self._lock:
            task = self._ensure_refresh_task(reason)
        return await task

    def schedule_startup_refresh(self) -> None:
        """启动后随机延迟刷新，避免多个 Bot/容器同一时间打到 PigHub。"""

        if self._startup_task and not self._startup_task.done():
            return
        self._startup_task = asyncio.create_task(self._delayed_startup_refresh())

    async def _delayed_startup_refresh(self) -> None:
        delay = random.randint(*PIGHUB_STARTUP_REFRESH_DELAY_SECONDS)
        try:
            await asyncio.sleep(delay)
            await self.refresh("startup")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # 启动期刷新不能影响 Bot 主流程；命令侧仍会使用本地旧索引或给出明确失败提示。
            logger.warning(f"PigHub 启动后台刷新失败: {error}")

    def _ensure_refresh_task(self, reason: str) -> asyncio.Task[bool]:
        if self._refresh_task and not self._refresh_task.done():
            return self._refresh_task
        self._refresh_task = asyncio.create_task(self._refresh_from_remote(reason))
        return self._refresh_task

    async def _refresh_from_remote(self, reason: str) -> bool:
        self.last_refresh_attempt = time.time()
        last_error: Exception | None = None
        headers = {"User-Agent": PIGHUB_USER_AGENT}
        try:
            async with httpx.AsyncClient(timeout=PIGHUB_HTTP_TIMEOUT_SECONDS, headers=headers) as client:
                for api_url in PIGHUB_API_URLS:
                    try:
                        response = await client.get(api_url)
                        response.raise_for_status()
                        images = parse_pighub_images_payload(response.json(), api_url)
                        self.images = images
                        self.last_loaded = time.time()
                        await asyncio.to_thread(self._save_cache_sync)
                        logger.info(f"PigHub 索引刷新完成: reason={reason}, images={len(images)}")
                        return True
                    except Exception as error:
                        # PigHub 曾切换过 API 结构；单个接口失败时继续尝试下一个。
                        last_error = error
                        logger.warning(f"PigHub 接口刷新失败，尝试备用接口: url={api_url}, error={error}")
        except Exception as error:
            last_error = error

        if self.images:
            logger.warning(f"PigHub 索引刷新失败，继续使用旧缓存（{len(self.images)} 张）: {last_error}")
            return True
        logger.warning(f"PigHub 索引刷新失败，且没有可用旧缓存: {last_error}")
        return False

    async def shutdown(self) -> None:
        """取消后台刷新任务，避免 NoneBot 退出时残留网络请求。"""

        tasks = [task for task in (self._startup_task, self._refresh_task) if task and not task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    # ================================ 查询接口 ================================ #
    def sample(self, count: int) -> list[dict[str, Any]]:
        if not self.images:
            return []
        pool_size = max(1, min(int(count), len(self.images)))
        return random.sample(self.images, pool_size)

    def search(self, keyword: str) -> list[dict[str, Any]]:
        return [item for item in self.images if match_pighub_keyword(item, keyword)]


def normalize_pighub_image_item(item: Any) -> Optional[dict[str, Any]]:
    """把 PigHub 新旧 API 条目归一成 title/thumbnail/filename。"""

    if not isinstance(item, dict):
        return None

    thumbnail = item.get("thumbnail") or item.get("image_url")
    if not isinstance(thumbnail, str) or not thumbnail:
        return None

    title = item.get("title")
    filename = item.get("filename") or thumbnail.split("/")[-1]
    normalized = dict(item)
    normalized["thumbnail"] = thumbnail
    normalized["title"] = str(title or filename or "未命名小猪")
    normalized["filename"] = str(filename or "")
    return normalized


def parse_pighub_images_payload(data: Any, api_url: str) -> list[dict[str, Any]]:
    """解析 PigHub 新旧 API 返回值，并过滤掉缺少图片地址的异常条目。"""

    if not isinstance(data, dict):
        raise ValueError(f"PigHub 返回结构异常（{api_url}）：不是 JSON 对象")

    raw_items = data.get("data") if isinstance(data.get("data"), list) else data.get("images")
    if not isinstance(raw_items, list):
        raise ValueError(f"PigHub 返回结构异常（{api_url}）：缺少 data/images 列表")

    valid = []
    for item in raw_items:
        normalized = normalize_pighub_image_item(item)
        if normalized:
            valid.append(normalized)
    if not valid:
        raise ValueError(f"PigHub 返回空图集（{api_url}）")
    return valid


def build_pighub_image_url(pig_item: dict[str, Any]) -> Optional[str]:
    thumbnail = pig_item.get("thumbnail")
    if not isinstance(thumbnail, str) or not thumbnail:
        return None

    if thumbnail.startswith(("http://", "https://")):
        image_url = thumbnail
    elif thumbnail.startswith("/"):
        image_url = urljoin(PIGHUB_ORIGIN, thumbnail)
    else:
        image_url = PIGHUB_IMAGE_BASE_URL + thumbnail.split("/")[-1]

    parsed = urlsplit(image_url)
    return urlunsplit((parsed.scheme, parsed.netloc, quote(parsed.path, safe="/%"), parsed.query, parsed.fragment))


def match_pighub_keyword(pig_item: dict[str, Any], keyword: str) -> bool:
    """按 PigHub 前端搜索习惯，同时匹配标题和文件名。"""

    lowered = keyword.lower()
    return any(
        lowered in str(pig_item.get(field, "")).lower()
        for field in ("title", "filename")
    )


pighub_service = PigHubService()
