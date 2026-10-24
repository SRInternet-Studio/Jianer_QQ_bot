"""HypeR-style model/tool loop backed by JianerAI providers."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from plugins.JianerAI.agent_runtime.models import AgentRole, SessionKey
from plugins.JianerAI.agent_runtime.store import ContextStore
from plugins.JianerAI.providers import (
    AssistantTurn,
    ChatRequest,
    FunctionTool,
    ProviderRegistry,
    ProviderResponse,
    ToolResultTurn,
)
from plugins.JianerAI.tools.contracts import ToolCall, ToolContext, ToolSpec
from plugins.JianerAI.tools.registry import ToolRegistry


class AgentCoreError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AgentStopped(AgentCoreError):
    def __init__(self) -> None:
        super().__init__("agent_stopped", "Agent processing was stopped")


@dataclass(frozen=True, slots=True)
class CoreResult:
    text: str
    rounds: int
    tool_calls: int


class AgentCore:
    """One persistent Main or System context."""

    def __init__(
        self,
        key: SessionKey,
        *,
        role: AgentRole,
        providers: ProviderRegistry,
        tools: ToolRegistry,
        store: ContextStore,
        model_resolver: Callable[[SessionKey], str],
        prompt_builder: Callable[[tuple[ToolSpec, ...]], str],
        allowed_tool_names: frozenset[str] | None = None,
        total_timeout_seconds: float = 600.0,
        max_history_chars: int = 60000,
        logger: Any | None = None,
    ) -> None:
        self.key = key
        self.role = role
        self.providers = providers
        self.tools = tools
        self.store = store
        self.model_resolver = model_resolver
        self.prompt_builder = prompt_builder
        self.allowed_tool_names = allowed_tool_names
        self.total_timeout_seconds = max(1.0, float(total_timeout_seconds))
        self.max_history_chars = max(0, int(max_history_chars))
        self.logger = logger
        self.history: list[dict[str, Any]] = []
        self.tasks: list[str] = []
        self.last_used = time.monotonic()
        self.working = False
        self._loaded = False
        self._load_lock = asyncio.Lock()
        self._run_lock = asyncio.Lock()
        self._stop_event: asyncio.Event | None = None
        self._processing_started: float | None = None
        self._pending_notices: list[dict[str, Any]] = []

    async def ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._load_lock:
            if self._loaded:
                return
            self.history = await self.store.load_history(self.key)
            self.tasks = await self.store.load_tasks(self.key)
            self._repair_history()
            self._loaded = True

    async def event_handler(
        self,
        event: str | None,
        *,
        context: ToolContext,
        tool_choice: str = "auto",
    ) -> CoreResult:
        await self.ensure_loaded()
        started_at = time.monotonic()
        self.last_used = started_at
        async with self._run_lock:
            self.working = True
            self._stop_event = asyncio.Event()
            self._processing_started = started_at
            try:
                if event is not None and str(event).strip():
                    self.history.append({"role": "user", "content": str(event)})
                async with asyncio.timeout(self.total_timeout_seconds):
                    bad_requests = 0
                    errors = 0
                    while True:
                        try:
                            return await self._run_loop(
                                event=str(event or ""),
                                context=context,
                                tool_choice=tool_choice,
                            )
                        except AgentStopped:
                            raise
                        except Exception as exc:
                            is_bad_request = (
                                type(exc).__name__ == "BadRequestError"
                            )
                            if is_bad_request:
                                bad_requests += 1
                                if bad_requests >= 3:
                                    raise
                            else:
                                errors += 1
                                if errors >= 5:
                                    raise
                            self._repair_history()
                            await asyncio.sleep(min(2.0 * max(1, errors), 8.0))
            except TimeoutError as exc:
                raise AgentCoreError(
                    "agent_timeout",
                    "Agent exceeded its total processing time",
                ) from exc
            finally:
                self.working = False
                self._stop_event = None
                self._processing_started = None
                self._repair_history()
                await self.save()

    async def _run_loop(
        self,
        *,
        event: str,
        context: ToolContext,
        tool_choice: str,
    ) -> CoreResult:
        del tool_choice
        model = self.model_resolver(self.key)
        turns: list[AssistantTurn | ToolResultTurn] = []
        rounds = 0
        tool_count = 0
        final_text = ""
        while True:
            self._raise_if_stopped()
            rounds += 1
            specs = self.available_tools(context)
            function_tools = tuple(
                FunctionTool(
                    name=spec.name,
                    description=spec.description,
                    parameters=spec.input_schema,
                )
                for spec in specs
            )
            if not function_tools:
                raise AgentCoreError(
                    "agent_tools_unavailable",
                    "当前模型没有可用的 Agent 工具，无法使用工具发送消息",
                )
            declarations = {item.name: item for item in specs}
            request = ChatRequest(
                message=event,
                history=(),
                history_items=self._provider_raw_history(),
                system_prompt=self._system_prompt(context, function_tools),
                attachments=tuple(getattr(context, "attachments", ())),
                tools=function_tools,
                turns=tuple(turns),
                json_mode=True,
            )
            response = await self._complete_or_stop(model, request)
            final_text = str(response.text or "")
            if not response.tool_calls:
                if final_text.strip() and final_text.strip() not in {"{}", ""}:
                    self.history.append(
                        {
                            "role": "assistant",
                            "content": final_text.strip(),
                            "provider_content": _mapping_copy(
                                response.turn.provider_content
                            ),
                        }
                    )
                break
            self.history.append(
                {
                    "role": "assistant",
                    "content": final_text,
                    "tool_calls": [
                        {"id": call.id, "name": call.name, "arguments": call.arguments}
                        for call in response.tool_calls
                    ],
                    "provider_content": _mapping_copy(
                        response.turn.provider_content
                    ),
                }
            )
            turns.append(response.turn)
            for provider_call in response.tool_calls:
                self._raise_if_stopped()
                spec = declarations.get(str(provider_call.name))
                if spec is None:
                    result_content = json.dumps(
                        {"ok": False, "error": "unknown_tool"},
                        ensure_ascii=False,
                    )
                    result_name = str(provider_call.name)
                else:
                    result = await self.tools.execute(
                        ToolCall(
                            id=str(provider_call.id),
                            name=str(provider_call.name),
                            arguments=provider_call.arguments,
                        ),
                        context,
                    )
                    result_content = result.content
                    result_name = result.name
                tool_count += 1
                turns.append(
                    ToolResultTurn(
                        call_id=str(provider_call.id),
                        name=result_name,
                        content=result_content,
                    )
                )
                self.history.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(provider_call.id),
                        "name": result_name,
                        "content": result_content,
                    }
                )
                if (
                    bool(getattr(context, "release_requested", False))
                    or (spec is not None and spec.release)
                ):
                    return CoreResult(final_text, rounds, tool_count)
        return CoreResult(final_text, rounds, tool_count)

    async def _complete_or_stop(
        self,
        model: str,
        request: ChatRequest,
    ) -> ProviderResponse:
        event = self._stop_event
        if event is None:
            return await self.providers.complete_agent_turn(model, request)
        call_task = asyncio.create_task(
            self.providers.complete_agent_turn(model, request)
        )
        stop_task = asyncio.create_task(event.wait())
        try:
            done, pending = await asyncio.wait(
                {call_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            call_task.cancel()
            stop_task.cancel()
            raise
        for task in pending:
            task.cancel()
        if call_task in done:
            await asyncio.gather(stop_task, return_exceptions=True)
            return call_task.result()
        await asyncio.gather(call_task, return_exceptions=True)
        raise AgentStopped()

    def available_tools(self, context: ToolContext) -> tuple[ToolSpec, ...]:
        specs = self.tools.available(context, getattr(context, "actor", None))
        if self.allowed_tool_names is None:
            return specs
        required = {
            "send_message",
            "send_group_msg",
            "send_private_msg",
            "collected_send",
        }
        return tuple(
            spec
            for spec in specs
            if spec.name in self.allowed_tool_names or spec.name in required
        )

    def _provider_history(self) -> tuple[dict[str, str], ...]:
        output: list[dict[str, str]] = []
        history = self.history
        last_user = -1
        for index, item in enumerate(history):
            if item.get("role") == "user":
                last_user = index
        if last_user >= 0:
            history = history[:last_user]
        for item in history:
            role = str(item.get("role") or "")
            if role not in {"user", "assistant"}:
                continue
            content = str(item.get("content") or "").strip()
            if content and content != "{}":
                output.append({"role": role, "content": content})
        return tuple(output)

    def _provider_raw_history(self) -> tuple[dict[str, Any], ...]:
        history = self.history
        last_user = -1
        for index, item in enumerate(history):
            if item.get("role") == "user":
                last_user = index
        if last_user >= 0:
            history = history[:last_user]
        return tuple(
            dict(item)
            for item in history
            if isinstance(item, dict)
        )

    def _system_prompt(
        self,
        context: ToolContext,
        tools: Sequence[FunctionTool],
    ) -> str:
        available = {
            spec.name: spec
            for spec in self.available_tools(context)
        }
        specs = tuple(
            available[tool.name]
            for tool in tools
            if tool.name in available
        )
        return self.prompt_builder(specs)

    def request_stop(self) -> bool:
        event = self._stop_event
        if event is None:
            return False
        event.set()
        return True

    def processing_elapsed(self) -> float | None:
        started = self._processing_started
        return None if started is None else time.monotonic() - started

    async def replace_with_summary(self, content: str) -> str:
        await self.ensure_loaded()
        async with self._run_lock:
            self.history = [
                {
                    "role": "user",
                    "content": "SYSTEM -- 先前消息的全部总结 --",
                },
                {"role": "assistant", "content": str(content or "").strip()},
            ]
            await self.save()
        return "已更新上下文总结"

    async def inject_notice(
        self,
        content: str,
        *,
        source: str = "",
        kind: str = "message",
    ) -> None:
        await self.ensure_loaded()
        text = str(content or "").strip()
        if not text:
            return
        async with self._run_lock:
            self.history.append(
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "type": "context_message",
                            "kind": str(kind),
                            "source": str(source),
                            "content": text,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                }
            )
            await self.save()

    async def clear(self) -> None:
        await self.ensure_loaded()
        async with self._run_lock:
            self.history.clear()
            await self.save()

    async def task_add(self, content: str) -> int:
        await self.ensure_loaded()
        text = str(content or "").strip()
        if not text:
            raise AgentCoreError("empty_task", "任务内容不能为空")
        self.tasks.append(text)
        await self.save()
        return len(self.tasks)

    async def task_remove(self, index: int) -> bool:
        await self.ensure_loaded()
        position = int(index) - 1
        if position < 0 or position >= len(self.tasks):
            return False
        self.tasks.pop(position)
        await self.save()
        return True

    async def task_list(self) -> tuple[str, ...]:
        await self.ensure_loaded()
        return tuple(self.tasks)

    async def save(self) -> None:
        await self.store.save_history(self.key, self.history)
        await self.store.save_tasks(self.key, self.tasks)

    def history_chars(self) -> int:
        return sum(
            len(json.dumps(item, ensure_ascii=False, default=str))
            for item in self.history
            if isinstance(item, dict)
        )

    def should_auto_summarize(self) -> bool:
        return (
            self.max_history_chars > 0
            and self.history_chars() >= self.max_history_chars
        )

    def _raise_if_stopped(self) -> None:
        if self._stop_event is not None and self._stop_event.is_set():
            raise AgentStopped()

    def _repair_history(self) -> None:
        """Remove an incomplete tool-call suffix after interruption."""

        if not self.history:
            return
        last_user = -1
        for index, item in enumerate(self.history):
            if isinstance(item, dict) and item.get("role") == "user":
                last_user = index
        if last_user < 0:
            self.history.clear()
            return
        tail = self.history[last_user:]
        pending: set[str] = set()
        for item in tail:
            if not isinstance(item, dict):
                continue
            if item.get("role") == "assistant":
                for call in item.get("tool_calls") or ():
                    if isinstance(call, dict) and call.get("id"):
                        pending.add(str(call["id"]))
            elif item.get("role") == "tool":
                pending.discard(str(item.get("tool_call_id") or ""))
        if pending:
            del self.history[last_user:]


def _mapping_copy(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        return json.loads(
            json.dumps(dict(value), ensure_ascii=False, default=str)
        )
    except (TypeError, ValueError):
        return None
