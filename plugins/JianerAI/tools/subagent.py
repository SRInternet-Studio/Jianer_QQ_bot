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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from plugins.JianerAI.agent import (
    AgentError,
    AgentInterrupted,
    AgentOptions,
    AgentRunner,
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
) -> frozenset[str]:
    available = {spec.name for spec in tools.available(context)}
    available.discard(SPAWN_SUBAGENTS_TOOL_NAME)
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
) -> dict[str, Any]:
    async with semaphore:
        if max_depth <= 0:
            return {
                "index": index,
                "goal": task.goal,
                "ok": False,
                "error": "max_depth_reached",
            }
        timeout = task.timeout_seconds or default_timeout_seconds
        runner = AgentRunner(
            providers,
            tools,
            options=AgentOptions(
                total_timeout_seconds=timeout,
                max_depth=max_depth - 1,
            ),
            allowed_tool_names=_child_allowed_names(
                tools=tools,
                context=context,
                parent_allowed_names=parent_allowed_names,
                requested=task.tools,
            ),
            logger=logger,
        )
        interrupt_event = getattr(context, "interrupt_event", None)
        try:
            async with asyncio.timeout(timeout):
                answer = await runner.run(
                    model=model,
                    message=task.goal,
                    history=(),
                    system_prompt=_SUBAGENT_SYSTEM_PROMPT,
                    attachments=(),
                    context=context,
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
                "error": "timeout",
            }
        except AgentInterrupted:
            return {
                "index": index,
                "goal": task.goal,
                "ok": False,
                "error": "cancelled",
            }
        except AgentError as exc:
            return {
                "index": index,
                "goal": task.goal,
                "ok": False,
                "error": exc.code,
            }
        except asyncio.CancelledError:
            raise
        except Exception:
            return {
                "index": index,
                "goal": task.goal,
                "ok": False,
                "error": "subagent_failed",
            }
        return {
            "index": index,
            "goal": task.goal,
            "ok": True,
            "output": str(answer or "").strip()[:_MAX_OUTPUT_CHARS],
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
        results = await asyncio.gather(
            *(
                _run_task(
                    index,
                    task,
                    providers=providers,
                    tools=tools,
                    logger=logger,
                    context=context,
                    model=model,
                    parent_allowed_names=parent_allowed_names,
                    default_timeout_seconds=default_timeout,
                    max_depth=max_depth,
                    semaphore=semaphore,
                )
                for index, task in enumerate(tasks)
            )
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


__all__ = [
    "SPAWN_SUBAGENTS_TOOL_NAME",
    "SubAgentTask",
    "subagent_tool",
]
