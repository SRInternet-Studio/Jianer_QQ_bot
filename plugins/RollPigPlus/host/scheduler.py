"""Small asyncio job scheduler replacing the apscheduler dependency."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:  # pragma: no cover - platform dependent
    SHANGHAI = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:  # pragma: no cover
    SHANGHAI = dt.timezone(dt.timedelta(hours=8), "Asia/Shanghai")

_INTERVAL_UNITS = {
    "weeks": 604800.0,
    "days": 86400.0,
    "hours": 3600.0,
    "minutes": 60.0,
    "seconds": 1.0,
}


def _interval_seconds(params: dict[str, Any]) -> float:
    total = 0.0
    for unit, factor in _INTERVAL_UNITS.items():
        value = params.get(unit)
        if value is None:
            continue
        total += float(value) * factor
    if total <= 0:
        raise ValueError("interval 触发器需要正数间隔")
    return total


@dataclass
class ScheduledJob:
    func: Callable[..., Any]
    trigger: str
    job_id: str
    params: dict[str, Any] = field(default_factory=dict)
    next_run: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    interval_seconds: float = 0.0
    running: bool = False
    task: asyncio.Task | None = None

    def advance(self, now: dt.datetime) -> None:
        if self.trigger == "interval":
            delta = dt.timedelta(seconds=self.interval_seconds)
            while self.next_run <= now:
                self.next_run += delta
            return
        zone = self.params.get("timezone") or SHANGHAI
        hour = int(self.params.get("hour") or 0)
        minute = int(self.params.get("minute") or 0)
        candidate = dt.datetime.combine(
            now.astimezone(zone).date(), dt.time(hour, minute), tzinfo=zone
        )
        while candidate <= now.astimezone(zone):
            candidate += dt.timedelta(days=1)
        self.next_run = candidate


class Scheduler:
    """Register jobs with the same decorator shape the ported jobs use."""

    def __init__(self, *, tick_seconds: float = 5.0) -> None:
        self.jobs: dict[str, ScheduledJob] = {}
        self.tick_seconds = tick_seconds
        self._loop_task: asyncio.Task | None = None

    def scheduled_job(self, trigger: str, **params: Any) -> Callable[..., Any]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.add_job(func, trigger, **params)
            return func

        return decorator

    def add_job(
        self,
        func: Callable[..., Any],
        trigger: str = "interval",
        **params: Any,
    ) -> ScheduledJob:
        job_id = str(params.get("id") or f"{getattr(func, '__name__', 'job')}_{len(self.jobs) + 1}")
        job = ScheduledJob(
            func=func,
            trigger=trigger,
            job_id=job_id,
            params=dict(params),
        )
        if trigger == "interval":
            job.interval_seconds = _interval_seconds(params)
            job.next_run = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
                seconds=job.interval_seconds
            )
        else:
            job.advance(dt.datetime.now(dt.timezone.utc))
        self.jobs[job_id] = job
        return job

    def get_job(self, job_id: str) -> ScheduledJob | None:
        return self.jobs.get(job_id)

    def get_jobs(self) -> list[ScheduledJob]:
        return list(self.jobs.values())

    def clear(self) -> None:
        for job in self.jobs.values():
            if job.task is not None:
                job.task.cancel()
        self.jobs.clear()

    async def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._loop_task = asyncio.create_task(self._run_loop())

    async def shutdown(self) -> None:
        task = self._loop_task
        self._loop_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for job in self.jobs.values():
            if job.task is not None:
                job.task.cancel()
                job.task = None

    async def _run_loop(self) -> None:
        while True:
            now = dt.datetime.now(dt.timezone.utc)
            for job in list(self.jobs.values()):
                if job.next_run > now:
                    continue
                self._fire(job)
                job.advance(dt.datetime.now(dt.timezone.utc))
            await asyncio.sleep(self.tick_seconds)

    def _fire(self, job: ScheduledJob) -> None:
        if job.running and int(job.params.get("max_instances") or 1) == 1:
            return

        async def runner() -> None:
            job.running = True
            try:
                await job.func()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                from .logger import logger

                logger.error(f"rollpig 定时任务失败: job={job.job_id} error={error}")
            finally:
                job.running = False

        job.task = asyncio.create_task(runner())


scheduler = Scheduler()

__all__ = ["ScheduledJob", "Scheduler", "scheduler"]
