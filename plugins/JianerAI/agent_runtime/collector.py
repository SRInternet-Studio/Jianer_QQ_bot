"""Adaptive event collector matching HypeR's aggregation behavior."""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from plugins.JianerAI.agent_runtime.models import AgentEvent, PermissionGroup

ProcessBatch = Callable[
    [list[dict[str, Any]], PermissionGroup, str | None, str | None],
    Awaitable[None],
]


class Collector:
    DEFAULT_MAX_BUFFER = 80
    DEFAULT_KEEP_AFTER_COMPRESS = 60
    DEFAULT_MAX_SUMMARY_CHARS = 8000
    TRUNCATED_MARK = "（更早的压缩内容已按字符上限淘汰）"

    def __init__(
        self,
        *,
        max_buffer: int = DEFAULT_MAX_BUFFER,
        keep_after_compress: int = DEFAULT_KEEP_AFTER_COMPRESS,
        max_summary_chars: int = DEFAULT_MAX_SUMMARY_CHARS,
        process: ProcessBatch | None = None,
    ) -> None:
        self.max_buffer = max(2, int(max_buffer))
        self.keep_after_compress = max(1, int(keep_after_compress))
        self.max_summary_chars = max(512, int(max_summary_chars))
        if self.keep_after_compress >= self.max_buffer:
            self.max_buffer = self.DEFAULT_MAX_BUFFER
            self.keep_after_compress = self.DEFAULT_KEEP_AFTER_COMPRESS
        self.process = process
        self.buffer: list[Any] = []
        self.delay = 8.0
        self.last_receive = 0.0
        self.active = False
        self.replying = False
        self.principal_id: str | None = None
        self.perm_group: PermissionGroup = "member"
        self.self_id: str | None = None
        self._task: asyncio.Task[Any] | None = None

    async def append(self, event: AgentEvent) -> None:
        await self.append_batch([event])

    async def append_passive(self, event: AgentEvent) -> None:
        self.buffer.append(event.to_dict())
        self._maybe_compress()

    async def append_batch(self, events: Sequence[AgentEvent]) -> None:
        if not events:
            return
        had_pending = bool(self.buffer)
        self.buffer.extend(item.to_dict() for item in events)
        self._maybe_compress()
        now = time.time()
        latest_text = json.dumps(
            events[-1].to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if had_pending:
            self._update_delay(now - self.last_receive, len(latest_text))
        else:
            weight = max(1.0, len(latest_text) * 0.2)
            self.delay = max(min(self.delay * weight, 16.0), 5.0)
        self.last_receive = now
        if self.active and not self.replying:
            self._restart_timer()

    async def start(
        self,
        principal_id: str | None,
        perm_group: PermissionGroup,
        self_id: str | None = None,
    ) -> None:
        if self.active or self.replying:
            return
        self.active = True
        self.principal_id = principal_id
        self.perm_group = perm_group
        self.self_id = self_id
        self._restart_timer()

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.active = False

    def _restart_timer(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._sleep_loop())

    async def _sleep_loop(self) -> None:
        try:
            await asyncio.sleep(self.delay)
            if self.process is None:
                return
            self.replying = True
            batch = list(self.buffer)
            try:
                await self.process(
                    batch,
                    self.perm_group,
                    self.principal_id,
                    self.self_id,
                )
            finally:
                self.replying = False
                delivered = {id(item) for item in batch}
                self.buffer = [
                    item for item in self.buffer if id(item) not in delivered
                ]
                if self.buffer:
                    self.active = True
                    self._restart_timer()
                else:
                    self.active = False
        except asyncio.CancelledError:
            pass

    def _update_delay(self, last: float, length: int) -> None:
        rate = last / self.delay if self.delay else 0.0
        weight = max(1.0, length * 0.2)
        self.delay = (
            (2 / 3) * (1 - math.cos(math.pi * min(rate, 1.0)))
            + (2 / 3) * weight
            + 2.5
        )
        self.delay = max(min(self.delay, 16.0), 5.0)

    def _maybe_compress(self) -> None:
        if len(self.buffer) < self.max_buffer:
            return
        overflow = len(self.buffer) - self.keep_after_compress
        folded = self.buffer[:overflow]
        self.buffer = self.buffer[overflow:]
        text = "\n".join(self._timeline(item) for item in folded)
        if len(text) > self.max_summary_chars:
            tail = text[-(self.max_summary_chars - len(self.TRUNCATED_MARK) - 1) :]
            newline = tail.find("\n")
            if 0 <= newline < 200:
                tail = tail[newline + 1 :]
            text = f"{self.TRUNCATED_MARK}\n{tail}"
        count = sum(
            1
            for item in folded
            if not (isinstance(item, dict) and item.get("compressed"))
        )
        self.buffer.insert(
            0,
            {
                "compressed": True,
                "count": count,
                "summary": text,
            },
        )

    @staticmethod
    def _timeline(item: Any) -> str:
        if isinstance(item, dict) and item.get("compressed"):
            return str(item.get("summary") or "")
        if isinstance(item, str):
            return item
        return json.dumps(item, ensure_ascii=False, separators=(",", ":"))
