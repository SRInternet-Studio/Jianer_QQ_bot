from __future__ import annotations

import asyncio
from typing import Any

from plugins.JianerAI.memorix.core.runtime.services.background_task_service import (
    MemoryBackgroundTaskService,
)

_DEFAULT_FINGERPRINT = object()


class _StubKernel:
    """Minimal kernel surface used by the vector rebuild watchdog."""

    def __init__(
        self,
        *,
        state: str = "incompatible",
        stage: str = "rebuild_required",
        vector_read: bool = False,
        fingerprint: Any = _DEFAULT_FINGERPRINT,
        rebuild_success: bool = True,
    ) -> None:
        self._background_stopping = False
        self._runtime_capabilities = {
            "vector_read": vector_read,
            "vector_write": vector_read,
        }
        self._vector_health = {
            "state": state,
            "recovery_stage": stage,
            "error_code": "v2_fingerprint_missing",
        }
        self.metadata_store = object()
        self.embedding_manager = object()
        self._fingerprint = (
            {"hash": "sha256:test", "source": "observed"}
            if fingerprint is _DEFAULT_FINGERPRINT
            else fingerprint
        )
        self._rebuild_success = rebuild_success
        self.rebuild_calls = 0
        self.slept: list[float] = []

    def _current_embedding_fingerprint_for_validation(self) -> dict[str, Any] | None:
        return self._fingerprint

    async def _sleep_impl(self, seconds: float) -> None:
        self.slept.append(float(seconds))
        await asyncio.sleep(0)

    async def _rebuild_all_vectors(self) -> dict[str, Any]:
        self.rebuild_calls += 1
        if not self._rebuild_success:
            return {"success": False, "errors": ["embedding_api_unavailable"]}
        self._runtime_capabilities["vector_read"] = True
        self._runtime_capabilities["vector_write"] = True
        self._vector_health = {
            "state": "healthy",
            "recovery_stage": "idle",
            "error_code": "",
        }
        return {"success": True}


async def _drive(service: MemoryBackgroundTaskService, kernel: _StubKernel, *, cycles: int = 200) -> None:
    # ``_sleep_background`` 是服务自身的方法，只有把它塞进内核实例字典
    # 才能通过属性代理覆盖，避免测试真的等待分钟级重试间隔。
    service._sleep_background = kernel._sleep_impl
    task = asyncio.create_task(service._vector_rebuild_recovery_loop())
    for _ in range(cycles):
        await asyncio.sleep(0)
        if kernel.rebuild_calls:
            break
    kernel._background_stopping = True
    for _ in range(50):
        if task.done():
            break
        await asyncio.sleep(0)
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_rebuild_recovery_required_only_for_untrusted_disabled_channel() -> None:
    kernel = _StubKernel()
    service = MemoryBackgroundTaskService(kernel)
    assert service._vector_rebuild_recovery_required() is True

    kernel._runtime_capabilities["vector_read"] = True
    assert service._vector_rebuild_recovery_required() is False

    kernel._runtime_capabilities["vector_read"] = False
    kernel._vector_health["recovery_stage"] = "idle"
    assert service._vector_rebuild_recovery_required() is False

    kernel._vector_health["recovery_stage"] = "rebuild_required"
    kernel._vector_health["state"] = "verification_pending"
    assert service._vector_rebuild_recovery_required() is False

    kernel._vector_health["state"] = "incompatible"
    assert service._vector_rebuild_recovery_required() is True

    kernel.metadata_store = None
    assert service._vector_rebuild_recovery_required() is False


def test_rebuild_recovery_restores_channel_after_rebuild() -> None:
    kernel = _StubKernel()
    service = MemoryBackgroundTaskService(kernel)
    asyncio.run(_drive(service, kernel))
    assert kernel.rebuild_calls == 1
    assert kernel._runtime_capabilities["vector_read"] is True
    assert kernel._vector_health["state"] == "healthy"
    assert kernel.slept and kernel.slept[0] > 0


def test_rebuild_recovery_waits_for_observed_fingerprint() -> None:
    kernel = _StubKernel(fingerprint=None)
    service = MemoryBackgroundTaskService(kernel)
    asyncio.run(_drive(service, kernel, cycles=60))
    assert kernel.rebuild_calls == 0
