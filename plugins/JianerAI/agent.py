from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from plugins.JianerAI.observability import (
    format_log_data,
    safe_log_info,
    sanitize_log_data,
)
from plugins.JianerAI.providers import (
    ChatRequest,
    EmptyProviderResponseError,
    FunctionTool,
    MediaAttachment,
    ProviderRegistry,
    ToolResultTurn,
    ToolsUnsupportedError,
)
from plugins.JianerAI.tools.contracts import ToolCall, ToolContext, ToolSpec
from plugins.JianerAI.tools.registry import ToolRegistry, duplicate_call_result


class AgentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)


class AgentInterrupted(Exception):
    """Raised when a newer message superseded the in-flight generation."""


@runtime_checkable
class AgentObserver(Protocol):
    """Receives incremental agent activity so callers can stream messages."""

    async def on_assistant_text(self, text: str, *, final: bool) -> None:
        ...

    async def on_tool_start(self, call: ToolCall) -> None:
        ...

    async def on_tool_result(self, call: ToolCall, result: Any) -> None:
        ...

    async def on_interrupt(self, reason: str) -> None:
        ...


async def notify_observer(
    observer: AgentObserver | None,
    method: str,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Invoke an observer callback without letting failures break the run."""

    if observer is None:
        return
    handler = getattr(observer, method, None)
    if not callable(handler):
        return
    try:
        value = handler(*args, **kwargs)
        if asyncio.iscoroutine(value):
            await value
    except asyncio.CancelledError:
        raise
    except Exception:
        safe_log_info(None, "JianerAI observer callback failed")


@dataclass(frozen=True, slots=True)
class AgentOptions:
    max_parallel_calls: int = 4
    total_timeout_seconds: float = 180.0
    max_depth: int = 1

    def __post_init__(self) -> None:
        if self.max_parallel_calls < 1:
            raise ValueError("agent max_parallel_calls must be positive")
        if self.total_timeout_seconds <= 0:
            raise ValueError("agent total_timeout_seconds must be positive")
        if self.max_depth < 0:
            raise ValueError("agent max_depth cannot be negative")


class AgentRunner:
    def __init__(
        self,
        providers: ProviderRegistry,
        tools: ToolRegistry,
        *,
        options: AgentOptions = AgentOptions(),
        allowed_tool_names: frozenset[str] | None = None,
        logger: Any | None = None,
    ) -> None:
        self.providers = providers
        self.tools = tools
        self.options = options
        self.allowed_tool_names = (
            None
            if allowed_tool_names is None
            else frozenset(
                str(item).strip()
                for item in allowed_tool_names
                if str(item).strip()
            )
        )
        self._logger = logger

    def _specs(self, context: ToolContext) -> tuple[ToolSpec, ...]:
        return tuple(
            spec
            for spec in self.tools.available(context)
            if self.allowed_tool_names is None
            or spec.name in self.allowed_tool_names
        )

    async def run(
        self,
        *,
        model: str,
        message: str,
        history: Sequence[Mapping[str, Any]],
        system_prompt: str,
        attachments: Sequence[MediaAttachment],
        context: ToolContext,
        enabled: bool,
        observer: AgentObserver | None = None,
        depth: int = 0,
        interrupt_event: asyncio.Event | None = None,
    ) -> str:
        specs = self._specs(context)
        complete = getattr(self.providers, "complete_request", None)
        supports = getattr(self.providers, "supports_tools", None)
        if (
            not enabled
            or not specs
            or not callable(complete)
            or (callable(supports) and not supports(model))
        ):
            return await self.providers.chat(
                model,
                message,
                history=history,
                system_prompt=system_prompt,
                attachments=attachments,
            )

        declarations = tuple(
            FunctionTool(
                name=spec.name,
                description=spec.description,
                parameters=spec.input_schema,
            )
            for spec in specs
        )
        turns: list[Any] = []
        seen_call_ids: set[str] = set()
        try:
            async with asyncio.timeout(self.options.total_timeout_seconds):
                while True:
                    if interrupt_event is not None and interrupt_event.is_set():
                        await notify_observer(
                            observer,
                            "on_interrupt",
                            "superseded",
                        )
                        raise AgentInterrupted()
                    try:
                        response = await self._complete_or_interrupt(
                            complete,
                            model,
                            ChatRequest(
                                message=message,
                                history=tuple(history),
                                system_prompt=system_prompt,
                                attachments=tuple(attachments),
                                tools=declarations,
                                turns=tuple(turns),
                            ),
                            interrupt_event,
                        )
                    except EmptyProviderResponseError:
                        safe_log_info(
                            self._logger,
                            "JianerAI Agent 工具请求返回空响应，回退到普通生成",
                        )
                        return await self.providers.chat(
                            model,
                            message,
                            history=history,
                            system_prompt=system_prompt,
                            attachments=attachments,
                        )
                    if response is None:
                        await notify_observer(
                            observer,
                            "on_interrupt",
                            "superseded",
                        )
                        raise AgentInterrupted()
                    text = str(response.text or "").rstrip()
                    if not response.tool_calls:
                        if text:
                            await notify_observer(
                                observer,
                                "on_assistant_text",
                                text,
                                final=True,
                            )
                        return text
                    if text:
                        await notify_observer(
                            observer,
                            "on_assistant_text",
                            text,
                            final=False,
                        )
                    turns.append(response.turn)
                    calls: list[tuple[int, ToolCall]] = []
                    results_by_index: dict[int, Any] = {}
                    for index, item in enumerate(response.tool_calls):
                        call = ToolCall(
                            id=str(item.id),
                            name=str(item.name),
                            arguments=item.arguments,
                        )
                        if call.id in seen_call_ids:
                            result = duplicate_call_result(call)
                            results_by_index[index] = result
                            self._log_tool_result(
                                call,
                                result,
                                context,
                                executed=False,
                            )
                            continue
                        seen_call_ids.add(call.id)
                        calls.append((index, call))
                    executed = await self._execute_calls(
                        [call for _, call in calls],
                        context,
                        observer=observer,
                    )
                    for (index, _), result in zip(calls, executed):
                        results_by_index[index] = result
                    for index in sorted(results_by_index):
                        result = results_by_index[index]
                        turns.append(
                            ToolResultTurn(
                                call_id=result.call_id,
                                name=result.name,
                                content=result.content,
                            )
                        )
        except ToolsUnsupportedError:
            mark = getattr(self.providers, "mark_tools_unsupported", None)
            if callable(mark):
                mark(model)
            return await self.providers.chat(
                model,
                message,
                history=history,
                system_prompt=system_prompt,
                attachments=attachments,
            )
        except TimeoutError as exc:
            raise AgentError("agent_timeout", "Agent 执行超过总时限。") from exc

    async def _complete_or_interrupt(
        self,
        complete: Any,
        model: str,
        request: ChatRequest,
        interrupt_event: asyncio.Event | None,
    ) -> Any | None:
        """Run one provider call, returning ``None`` when superseded."""

        if interrupt_event is None:
            return await complete(model, request)
        call_task = asyncio.ensure_future(complete(model, request))
        wait_task = asyncio.ensure_future(interrupt_event.wait())
        try:
            done, pending = await asyncio.wait(
                {call_task, wait_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            for task in (call_task, wait_task):
                task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.gather(
                    call_task,
                    wait_task,
                    return_exceptions=True,
                )
            raise
        for task in pending:
            task.cancel()
        if call_task in done:
            return call_task.result()
        with contextlib.suppress(BaseException):
            await asyncio.gather(
                call_task,
                wait_task,
                return_exceptions=True,
            )
        return None

    async def _execute_calls(
        self,
        calls: Sequence[ToolCall],
        context: ToolContext,
        *,
        observer: AgentObserver | None = None,
    ) -> tuple[Any, ...]:
        semaphore = asyncio.Semaphore(self.options.max_parallel_calls)

        async def execute(call: ToolCall):
            async with semaphore:
                self._log_tool_start(call, context)
                await notify_observer(observer, "on_tool_start", call)
                started_at = time.perf_counter()
                try:
                    result = await self.tools.execute(call, context)
                except asyncio.CancelledError:
                    self._log_tool_terminal_error(
                        call,
                        context,
                        started_at,
                        status="cancelled",
                    )
                    raise
                except Exception:
                    self._log_tool_terminal_error(
                        call,
                        context,
                        started_at,
                        status="unexpected_error",
                    )
                    raise
                self._log_tool_result(
                    call,
                    result,
                    context,
                    executed=True,
                    started_at=started_at,
                )
                await notify_observer(observer, "on_tool_result", call, result)
                return result

        if not calls:
            return ()
        return tuple(await asyncio.gather(*(execute(call) for call in calls)))

    def _log_tool_start(self, call: ToolCall, context: ToolContext) -> None:
        safe_log_info(
            self._logger,
            "JianerAI tool call 开始 | "
            + format_log_data(self._tool_log_context(call, context)),
        )

    def _log_tool_result(
        self,
        call: ToolCall,
        result: Any,
        context: ToolContext,
        *,
        executed: bool,
        started_at: float | None = None,
    ) -> None:
        payload = self._tool_log_context(call, context)
        payload.update(
            {
                "executed": bool(executed),
                "ok": bool(getattr(result, "ok", False)),
                "error_code": getattr(result, "error_code", None),
                "arguments": sanitize_log_data(
                    call.arguments,
                    sensitive_values=context.sensitive_values,
                    tool_name=call.name,
                ),
                "result": sanitize_log_data(
                    getattr(result, "content", ""),
                    sensitive_values=context.sensitive_values,
                    tool_name=call.name,
                ),
            }
        )
        if started_at is not None:
            payload["duration_ms"] = round(
                (time.perf_counter() - started_at) * 1000,
                2,
            )
        phase = "完成" if executed else "拒绝"
        safe_log_info(
            self._logger,
            f"JianerAI tool call {phase} | "
            + format_log_data(
                payload,
                sensitive_values=context.sensitive_values,
                tool_name=call.name,
            ),
        )

    def _log_tool_terminal_error(
        self,
        call: ToolCall,
        context: ToolContext,
        started_at: float,
        *,
        status: str,
    ) -> None:
        payload = self._tool_log_context(call, context)
        payload.update(
            {
                "status": status,
                "arguments": sanitize_log_data(
                    call.arguments,
                    sensitive_values=context.sensitive_values,
                    tool_name=call.name,
                ),
                "duration_ms": round(
                    (time.perf_counter() - started_at) * 1000,
                    2,
                ),
            }
        )
        safe_log_info(
            self._logger,
            "JianerAI tool call 异常 | "
            + format_log_data(
                payload,
                sensitive_values=context.sensitive_values,
                tool_name=call.name,
            ),
        )

    @staticmethod
    def _tool_log_context(
        call: ToolCall,
        context: ToolContext,
    ) -> dict[str, Any]:
        conversation = context.conversation
        return {
            "call_id": str(call.id),
            "tool": str(call.name),
            "protocol": str(conversation.protocol),
            "self_id": str(conversation.self_id),
            "conversation_kind": str(conversation.kind.value),
            "conversation_id": str(conversation.conversation_id),
            "preset": str(conversation.preset),
            "user_id": str(getattr(context.event, "user_id", "")),
        }
