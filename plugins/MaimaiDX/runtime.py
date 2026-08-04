"""Lifecycle and readiness management for the JianerCore port."""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

from . import adapter
from .config import dfconfig, log, maiconfig
from .resources import (
    ensure_runtime_dirs,
    plate_table_dir,
    rating_table_dir,
    resource_issues,
)


IDENTITY_RETRY_SECONDS = 30.0


class MaimaiRuntime:
    def __init__(self) -> None:
        self.actions: Any | None = None
        self.self_id: int | str | None = None
        self.data_ready = False
        self.initialization_error: str | None = None
        self.missing_resources: tuple[str, ...] = ()
        self._initialize_lock: asyncio.Lock | None = None
        self._refresh_generation = 0
        self._last_refresh_result = False
        self._daily_task: asyncio.Task[None] | None = None
        self._alias_task: asyncio.Task[None] | None = None
        self._identity_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    def attach_actions(self, actions: Any) -> None:
        self.actions = actions

    def observe_context(self, event: Any, actions: Any) -> None:
        """Retain the newest connection and identity from ordinary events."""

        self.attach_actions(actions)
        event_self_id = getattr(event, "self_id", None)
        if event_self_id is not None:
            self.self_id = event_self_id
            task = self._identity_task
            try:
                current_task = asyncio.current_task()
            except RuntimeError:
                current_task = None
            if (
                task is not None
                and not task.done()
                and task is not current_task
            ):
                task.cancel()
        if self.data_ready:
            self._start_background_tasks()

    async def start(self, event: Any, actions: Any) -> None:
        self.observe_context(event, actions)
        if not adapter.is_supported(event, actions):
            return

        if self.self_id is None and not await self._resolve_self_id_once():
            self._schedule_identity_retry()
        if await self.initialize():
            self._start_background_tasks()

    async def _resolve_self_id_once(self) -> bool:
        if self.self_id is not None:
            return True
        actions = self.actions
        if actions is None:
            return False
        try:
            info = await adapter.get_login_info(actions)
            self.self_id = info["user_id"]
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                f"MaimaiDX 获取机器人账号失败，稍后重试："
                f"{type(exc).__name__}: {exc}"
            )
            return False

    def _initialize_guard(self) -> asyncio.Lock:
        if self._initialize_lock is None:
            self._initialize_lock = asyncio.Lock()
        return self._initialize_lock

    async def initialize(self, *, force: bool = False) -> bool:
        """Initialize or refresh data, coalescing callers already in flight."""

        observed_generation = self._refresh_generation
        async with self._initialize_guard():
            if self._refresh_generation != observed_generation:
                return self._last_refresh_result
            if self.data_ready and not force:
                return True

            result = await self._refresh_data()
            self._last_refresh_result = result
            self._refresh_generation += 1
            if result and force and self.actions is not None:
                self._start_background_tasks()
            return result

    async def refresh(self) -> bool:
        """Public forced refresh used by commands and the daily schedule."""

        return await self.initialize(force=True)

    async def _refresh_data(self) -> bool:
        was_ready = self.data_ready
        self.initialization_error = None
        try:
            ensure_runtime_dirs()
            from .core.clients.divingfish.client import DivingFishAPI
            from .core.database.qq import create_database
            from .core.image import AssetsImage
            from .core.service import guess, mai

            await create_database()
            DivingFishAPI.reset_origin()
            if dfconfig.divingfish_prober_proxy and not DivingFishAPI.set_proxy():
                log.warning(
                    "存在水鱼查分器开发者 Token，已拒绝第三方代理并直连官方"
                )
            await mai.get_music()
            await mai.get_music_alias()
            await mai.get_plate_json()
            guess.guess()
            self.missing_resources = resource_issues()
            if not self.missing_resources and maiconfig.save_in_memory:
                AssetsImage._load_image()
            self.data_ready = True
            log.info(
                "MaimaiDX 初始化完成：曲目、别名、版本与静态资源已就绪"
            )
            if not any(rating_table_dir.iterdir()):
                log.warning("定数表目录为空，请使用“更新定数表”生成")
            if not any(plate_table_dir.iterdir()):
                log.warning("完成表目录为空，请使用“更新完成表”生成")
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.data_ready = was_ready
            self.initialization_error = f"{type(exc).__name__}: {exc}"
            log.exception("MaimaiDX 初始化失败")
            return False

    async def ensure_data(self, event: Any, actions: Any) -> bool:
        if not adapter.is_supported(event, actions):
            await adapter.send(
                event, actions, "该功能仅支持 QQ OneBot/Milky 协议。"
            )
            return False
        self.observe_context(event, actions)
        if not await self.initialize():
            log.warning(
                "MaimaiDX 请求因数据未就绪被拒绝："
                f"{self.initialization_error or '未知错误'}"
            )
            await adapter.send(
                event,
                actions,
                "MaimaiDX 数据暂未就绪，请稍后重试；"
                "若持续失败，请联系管理员查看日志。",
            )
            return False
        self._start_background_tasks()
        return True

    async def ensure_resources(self, event: Any, actions: Any) -> bool:
        if not await self.ensure_data(event, actions):
            return False
        self.missing_resources = resource_issues()
        if not self.missing_resources:
            return True
        log.warning(
            "MaimaiDX 静态资源检查未通过：\n"
            + "\n".join(self.missing_resources)
        )
        await adapter.send(
            event,
            actions,
            "MaimaiDX 静态资源不完整，请联系管理员查看日志并补齐资源。",
        )
        return False

    def _ensure_stop_event(self) -> asyncio.Event:
        if self._stop_event is None or self._stop_event.is_set():
            self._stop_event = asyncio.Event()
        return self._stop_event

    def _schedule_identity_retry(self) -> None:
        if self.self_id is not None or self.actions is None:
            return
        stop_event = self._ensure_stop_event()
        if self._identity_task is None or self._identity_task.done():
            self._identity_task = asyncio.create_task(
                self._identity_retry_loop(stop_event),
                name="maimaidx-login-info-retry",
            )

    def _start_background_tasks(self) -> None:
        if not self.data_ready:
            return
        stop_event = self._ensure_stop_event()
        if self._daily_task is None or self._daily_task.done():
            self._daily_task = asyncio.create_task(
                self._daily_update_loop(stop_event),
                name="maimaidx-daily-update",
            )
        if self.self_id is None:
            self._schedule_identity_retry()
        if (
            maiconfig.maimaidx_alias_push
            and self.actions is not None
            and self.self_id is not None
            and (self._alias_task is None or self._alias_task.done())
        ):
            from .core.alias_ws_push import ws_alias_server

            self._alias_task = asyncio.create_task(
                ws_alias_server(
                    lambda: (self.actions, self.self_id), stop_event
                ),
                name="maimaidx-alias-push",
            )

    async def _identity_retry_loop(self, stop_event: asyncio.Event) -> None:
        while self.self_id is None and not stop_event.is_set():
            if await self._wait_or_stop(stop_event, IDENTITY_RETRY_SECONDS):
                return
            if await self._resolve_self_id_once():
                self._start_background_tasks()
                return

    @staticmethod
    async def _wait_or_stop(
        stop_event: asyncio.Event, seconds: float
    ) -> bool:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    @staticmethod
    def next_daily_update(now: dt.datetime | None = None) -> dt.datetime:
        now = now or dt.datetime.now().astimezone()
        if now.tzinfo is None:
            now = now.astimezone()
        target = dt.datetime.combine(
            now.date(), dt.time(hour=4), tzinfo=now.tzinfo
        )
        if target <= now:
            target += dt.timedelta(days=1)
        return target

    async def _daily_update_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            now = dt.datetime.now().astimezone()
            timeout = max(
                1.0, (self.next_daily_update(now) - now).total_seconds()
            )
            if await self._wait_or_stop(stop_event, timeout):
                return
            try:
                if not await self.refresh():
                    log.error("MaimaiDX 每日数据更新失败")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("MaimaiDX 每日数据更新失败")

    async def shutdown(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        tasks = [
            task
            for task in (
                self._alias_task,
                self._daily_task,
                self._identity_task,
            )
            if task
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._alias_task = None
        self._daily_task = None
        self._identity_task = None
        try:
            from .core.database.qq import engine

            await engine.dispose()
        except Exception:
            log.exception("MaimaiDX 数据库关闭失败")
        self.actions = None
        self.self_id = None
        self.data_ready = False


runtime = MaimaiRuntime()
