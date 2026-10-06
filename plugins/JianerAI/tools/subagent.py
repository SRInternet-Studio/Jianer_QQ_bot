"""Sub-Agent support.

The parent agent can fan out several long-running subtasks with one
``spawn_subagents`` call.  Each subtask runs its own nested tool loop with a
restricted tool whitelist; the parent waits for every subtask and receives the
collected results so it can summarise them.

Recursion is bounded by removing ``spawn_subagents`` from the child whitelist,
so a sub-agent can never spawn further sub-agents.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from plugins.JianerAI.agent import (
        AgentError,
        AgentInterrupted,
        AgentKernel,
        AgentOptions,
    )
from plugins.JianerAI.tools.contracts import (
    ToolContext,
    ToolExecutionError,
    ToolRisk,
    ToolSpec,
)

SPAWN_SUBAGENTS_TOOL_NAME = "spawn_subagents"
_DEFAULT_MAX_CONCURRENCY = 3
_DEFAULT_TIMEOUT_SECONDS = 300.0
_MIN_TIMEOUT_SECONDS = 5.0
_MAX_TIMEOUT_SECONDS = 1800.0
_MAX_TASKS = 8
_MAX_GOAL_CHARS = 4000
_MAX_OUTPUT_CHARS = 4000

_SUBAGENT_SYSTEM_PROMPT = (
    "你是一个 Sub-Agent，只负责完成被分配的单个子任务。\n"
    "请直接调用可用工具推进任务，不要与用户闲聊，也不要输出寒暄。\n"
    "完成后用简洁的中文总结结论、关键数据与不确定性。\n"
    "如果信息不足，明确说明缺少什么，不要编造。"
)


@dataclass(frozen=True, slots=True)
class SubAgentTask:
    goal: str
    tools: tuple[str, ...] | None = None
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class SubAgentRecord:
    """A small, non-sensitive snapshot of one active sub-agent task."""

    request_id: str
    scope: tuple[str, str, str, str, str]
    goal: str
    status: str
    started_at: float
    monotonic_started_at: float


class SubAgentRegistry:
    """Track active child tasks without retaining completed prompts/results."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, SubAgentRecord] = {}
        self._counter = 0

    def start(
        self,
        scope: tuple[str, str, str, str, str],
        goal: str,
    ) -> str:
        with self._lock:
            self._counter += 1
            request_id = f"subagent-{self._counter:04d}"
            now = time.time()
            self._records[request_id] = SubAgentRecord(
                request_id=request_id,
                scope=tuple(str(item) for item in scope),
                goal=_preview_goal(goal),
                status="queued",
                started_at=now,
                monotonic_started_at=time.monotonic(),
            )
            return request_id

    def update(self, request_id: str, *, status: str) -> None:
        with self._lock:
            record = self._records.get(str(request_id))
            if record is None:
                return
            self._records[record.request_id] = dataclasses.replace(
                record,
                status=str(status),
            )

    def finish(self, request_id: str) -> None:
        with self._lock:
            self._records.pop(str(request_id), None)

    def list_active(
        self,
        scope: tuple[str, str, str, str, str],
    ) -> tuple[Mapping[str, Any], ...]:
        wanted = tuple(str(item) for item in scope)
        now = time.monotonic()
        with self._lock:
            records = tuple(
                record
                for record in self._records.values()
                if record.scope == wanted
                and record.status in {"queued", "running"}
            )
        return tuple(
            {
                "request_id": record.request_id,
                "status": record.status,
                "goal": record.goal,
                "elapsed_seconds": round(
                    max(0.0, now - record.monotonic_started_at), 1
                ),
                "started_at": record.started_at,
            }
            for record in records
        )

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


def _preview_goal(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _context_scope(context: ToolContext) -> tuple[str, str, str, str, str]:
    conversation = context.conversation
    kind = getattr(conversation, "kind", "")
    return (
        str(getattr(conversation, "protocol", "")),
        str(getattr(conversation, "self_id", "")),
        str(getattr(kind, "value", kind)),
        str(getattr(conversation, "conversation_id", "")),
        str(getattr(conversation, "preset", "default")),
    )


def _parse_tasks(raw: Any) -> list[SubAgentTask]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise ToolExecutionError("invalid_tasks", "tasks 必须是数组。")
    tasks: list[SubAgentTask] = []
    for item in raw:
        if len(tasks) >= _MAX_TASKS:
            break
        if isinstance(item, str):
            goal = item.strip()
            tools = None
            timeout = _DEFAULT_TIMEOUT_SECONDS
        elif isinstance(item, Mapping):
            goal = str(item.get("goal") or "").strip()
            raw_tools = item.get("tools")
            if isinstance(raw_tools, str):
                tools = tuple(
                    part.strip() for part in raw_tools.split(",") if part.strip()
                )
            elif isinstance(raw_tools, Sequence) and not isinstance(
                raw_tools, (str, bytes, bytearray)
            ):
                tools = tuple(
                    str(part).strip() for part in raw_tools if str(part).strip()
                )
            else:
                tools = None
            try:
                timeout = float(
                    item.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
                )
            except (TypeError, ValueError):
                timeout = _DEFAULT_TIMEOUT_SECONDS
        else:
            continue
        if not goal:
            continue
        timeout = max(_MIN_TIMEOUT_SECONDS, min(_MAX_TIMEOUT_SECONDS, timeout))
        tasks.append(
            SubAgentTask(
                goal=goal[:_MAX_GOAL_CHARS],
                tools=tools,
                timeout_seconds=timeout,
            )
        )
    return tasks


def _child_allowed_names(
    *,
    tools: Any,
    context: ToolContext,
    parent_allowed_names: frozenset[str] | None,
    requested: tuple[str, ...] | None,
    can_send_message: bool = False,
    can_mutate: bool = False,
) -> frozenset[str]:
    available = {spec.name for spec in tools.available(context)}
    denied = {
        SPAWN_SUBAGENTS_TOOL_NAME,
        "list_subagents",
        "call_platform_api",
        "platform_command",
        "list_my_memories",
        "read_recent_chat",
        "search_current_chat",
    }
    denied.update(
        name
        for name in available
        if name.startswith(("onebot_", "milky_", "lark_"))
    )
    if not can_send_message:
        denied.add("send_message")
    if not can_mutate:
        denied.update({"bash", "create_my_memory", "update_my_memory"})
    available.difference_update(denied)
    if parent_allowed_names is not None:
        available &= set(parent_allowed_names)
    if requested is not None:
        available &= set(requested)
    return frozenset(available)


async def _run_task(
    index: int,
    task: SubAgentTask,
    *,
    providers: Any,
    tools: Any,
    logger: Any,
    context: ToolContext,
    model: str,
    parent_allowed_names: frozenset[str] | None,
    default_timeout_seconds: float,
    max_depth: int,
    semaphore: asyncio.Semaphore,
    runner_factory: Callable[..., Any] | None = None,
    agent_kernel: Any | None = None,
    can_send_message: bool = False,
    can_mutate: bool = False,
) -> dict[str, Any]:
    # ``tools`` is imported while ``agent.py`` is still defining its public
    # classes.  Importing AgentError/AgentKernel at module scope would make
    # the generation loader observe a partially initialized agent module.
    # Resolve the runtime classes only once a child task is actually started.
    from plugins.JianerAI.agent import (
        AgentError,
        AgentInterrupted,
        AgentKernel,
        AgentOptions,
    )

    async with semaphore:
        registry = getattr(context, "subagent_registry", None)
        tracking_id = str(getattr(context, "subagent_tracking_id", "") or "")
        if registry is not None and tracking_id:
            registry.update(tracking_id, status="running")
        started_at = time.perf_counter()
        if max_depth <= 0:
            return {
                "index": index,
                "goal": task.goal,
                "ok": False,
                "status": "rejected",
                "error": "max_depth_reached",
                "duration_ms": 0,
            }
        timeout = task.timeout_seconds or default_timeout_seconds
        allowed_names = _child_allowed_names(
            tools=tools,
            context=context,
            parent_allowed_names=parent_allowed_names,
            requested=task.tools,
            can_send_message=can_send_message,
            can_mutate=can_mutate,
        )
        if agent_kernel is not None:
            runner = agent_kernel
        elif runner_factory is not None:
            runner = runner_factory(
                providers=providers,
                tools=tools,
                timeout_seconds=timeout,
                allowed_tool_names=allowed_names,
                depth=max_depth - 1,
                logger=logger,
            )
        else:
            runner = AgentKernel(
                providers,
                tools,
                options=AgentOptions(
                    total_timeout_seconds=timeout,
                    max_depth=max_depth - 1,
                ),
                allowed_tool_names=allowed_names,
                logger=logger,
            )
        child_memory = getattr(context, "memory_snapshot", None)
        if child_memory is None:
            child_memory = _readonly_memory_snapshot(getattr(context, "memory", None))
        child_context = dataclasses.replace(
            context,
            memory=child_memory,
            memory_snapshot=child_memory,
            tool_permissions=allowed_names,
            depth=getattr(context, "depth", 0) + 1,
            agent=runner,
        )
        interrupt_event = getattr(context, "interrupt_event", None)
        child_system_prompt = _SUBAGENT_SYSTEM_PROMPT
        snapshot_prompt = getattr(child_memory, "as_prompt", None)
        if callable(snapshot_prompt):
            rendered_memory = str(snapshot_prompt() or "").strip()
            if rendered_memory:
                child_system_prompt += "\n\n" + rendered_memory
        try:
            async with asyncio.timeout(timeout):
                answer = await runner.run(
                    model=model,
                    message=task.goal,
                    history=(),
                    system_prompt=child_system_prompt,
                    attachments=(),
                    context=child_context,
                    enabled=True,
                    observer=None,
                    depth=1,
                    interrupt_event=interrupt_event,
                )
        except TimeoutError:
            return {
                "index": index,
                "goal": task.goal,
                "ok": False,
                "status": "timeout",
                "error": "timeout",
                "duration_ms": _duration_ms(started_at),
            }
        except AgentInterrupted:
            return {
                "index": index,
                "goal": task.goal,
                "ok": False,
                "status": "cancelled",
                "error": "cancelled",
                "cancelled": True,
                "duration_ms": _duration_ms(started_at),
            }
        except AgentError as exc:
            return {
                "index": index,
                "goal": task.goal,
                "ok": False,
                "status": "failed",
                "error": exc.code,
                "duration_ms": _duration_ms(started_at),
            }
        except asyncio.CancelledError:
            raise
        except Exception:
            return {
                "index": index,
                "goal": task.goal,
                "ok": False,
                "status": "failed",
                "error": "subagent_failed",
                "duration_ms": _duration_ms(started_at),
            }
        return {
            "index": index,
            "goal": task.goal,
            "ok": True,
            "status": "completed",
            "output": str(answer or "").strip()[:_MAX_OUTPUT_CHARS],
            "duration_ms": _duration_ms(started_at),
        }


def subagent_tool(
    *,
    providers: Any,
    tools: Any,
    logger: Any,
    model_resolver: Callable[[ToolContext], str],
    parent_allowed_names: frozenset[str] | None = None,
    max_concurrency: int = _DEFAULT_MAX_CONCURRENCY,
    default_timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    max_depth: int = 1,
    enabled: bool = True,
    runner_factory: Callable[..., Any] | None = None,
    agent_kernel: Any | None = None,
    can_send_message: bool = False,
    can_mutate: bool = False,
    registry: SubAgentRegistry | None = None,
) -> ToolSpec:
    cap = max(1, min(_MAX_TASKS, int(max_concurrency)))
    default_timeout = max(
        _MIN_TIMEOUT_SECONDS,
        min(_MAX_TIMEOUT_SECONDS, float(default_timeout_seconds)),
    )

    async def _spawn(
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not enabled:
            raise ToolExecutionError(
                "subagent_disabled",
                "Sub-Agent 功能当前已关闭。",
            )
        tasks = _parse_tasks(arguments.get("tasks"))
        if not tasks:
            raise ToolExecutionError("invalid_tasks", "至少需要一个有效子任务。")
        requested = arguments.get("max_concurrency")
        try:
            concurrency = int(requested) if requested is not None else cap
        except (TypeError, ValueError):
            concurrency = cap
        concurrency = max(1, min(cap, concurrency))
        model = str(model_resolver(context) or "").strip()
        if not model:
            raise ToolExecutionError("model_unavailable", "当前会话没有可用模型。")
        semaphore = asyncio.Semaphore(concurrency)
        async def tracked(index: int, task: SubAgentTask) -> dict[str, Any]:
            tracking_id = (
                registry.start(_context_scope(context), task.goal)
                if registry is not None
                else ""
            )
            child_context = dataclasses.replace(
                context,
                subagent_registry=registry,
                subagent_tracking_id=tracking_id,
            )
            try:
                return await _run_task(
                    index,
                    task,
                    providers=providers,
                    tools=tools,
                    logger=logger,
                    context=child_context,
                    model=model,
                    parent_allowed_names=parent_allowed_names,
                    default_timeout_seconds=default_timeout,
                    max_depth=max_depth,
                    semaphore=semaphore,
                    runner_factory=runner_factory,
                    agent_kernel=agent_kernel,
                    can_send_message=can_send_message,
                    can_mutate=can_mutate,
                )
            finally:
                if registry is not None and tracking_id:
                    registry.finish(tracking_id)

        results = await asyncio.gather(
            *(tracked(index, task) for index, task in enumerate(tasks))
        )
        return {
            "tasks": len(tasks),
            "concurrency": concurrency,
            "results": list(results),
        }

    return ToolSpec(
        name=SPAWN_SUBAGENTS_TOOL_NAME,
        description=(
            "派生多个 Sub-Agent 并发执行耗时的子任务，并收集它们的结论。"
            "适合需要分别搜索、抓取或计算的独立子任务；"
            "子任务之间不要有依赖关系。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "要并发执行的子任务列表。",
                    "minItems": 1,
                    "maxItems": _MAX_TASKS,
                    "items": {
                        "type": "object",
                        "properties": {
                            "goal": {
                                "type": "string",
                                "description": "该子任务的目标与期望产出。",
                                "minLength": 1,
                                "maxLength": _MAX_GOAL_CHARS,
                            },
                            "tools": {
                                "type": "array",
                                "description": "该子任务可使用的工具名（可选）。",
                                "items": {"type": "string"},
                            },
                            "timeout_seconds": {
                                "type": "number",
                                "description": "该子任务超时秒数。",
                                "minimum": _MIN_TIMEOUT_SECONDS,
                                "maximum": _MAX_TIMEOUT_SECONDS,
                            },
                        },
                        "required": ["goal"],
                        "additionalProperties": False,
                    },
                },
                "max_concurrency": {
                    "type": "integer",
                    "description": "同时运行的子 Agent 数量上限。",
                    "minimum": 1,
                    "maximum": cap,
                },
            },
            "required": ["tasks"],
            "additionalProperties": False,
        },
        handler=_spawn,
        risk=ToolRisk.READ_ONLY,
        timeout_seconds=default_timeout + 30.0,
        max_output_chars=16000,
    )


def list_subagents_tool(registry: SubAgentRegistry) -> ToolSpec:
    async def _list(
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        del arguments
        active = registry.list_active(_context_scope(context))
        return {"active": list(active), "count": len(active)}

    return ToolSpec(
        name="list_subagents",
        description="查看当前会话正在排队或运行的 Sub-Agent，只返回当前会话的状态。",
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=_list,
        risk=ToolRisk.READ_ONLY,
        timeout_seconds=5.0,
        max_output_chars=12000,
    )


__all__ = [
    "SubAgentRecord",
    "SubAgentRegistry",
    "SPAWN_SUBAGENTS_TOOL_NAME",
    "SubAgentTask",
    "list_subagents_tool",
    "subagent_tool",
]


def _readonly_memory_snapshot(memory: Any) -> Any:
    if isinstance(memory, Mapping):
        return MappingProxyType(dict(memory))
    snapshot = getattr(memory, "snapshot", None)
    if callable(snapshot):
        value = snapshot()
        if isinstance(value, Mapping):
            return MappingProxyType(dict(value))
        return value
    if memory is None:
        return None
    return _ReadOnlyMemoryProxy(memory)


def _duration_ms(started_at: float) -> int:
    return max(0, round((time.perf_counter() - started_at) * 1000))


class _ReadOnlyMemoryProxy:
    """Forward memory reads while making common mutation APIs unavailable."""

    _MUTATORS = frozenset(
        {
            "create_memory",
            "update_memory",
            "delete_memory",
            "create_scoped_memory",
            "update_scoped_memory",
            "delete_scoped_memory",
            "add_scoped_memory_evidence",
            "redact_transcript_values",
        }
    )

    def __init__(self, target: Any) -> None:
        object.__setattr__(self, "_target", target)

    def __getattr__(self, name: str) -> Any:
        if name in self._MUTATORS:
            raise AttributeError(f"memory mutation is unavailable to Sub-Agent: {name}")
        return getattr(self._target, name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("Sub-Agent memory context is read-only")
