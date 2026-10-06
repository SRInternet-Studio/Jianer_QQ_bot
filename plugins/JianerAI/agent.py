from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
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
    ProviderResponse,
    ProviderRegistry,
    ProviderStreamEvent,
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


@dataclass(frozen=True, slots=True)
class _StreamResult:
    response: ProviderResponse
    streamed_text: bool


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """A structured lifecycle event emitted by an Agent run.

    ``payload`` deliberately contains the same typed objects passed to the
    legacy observer callbacks.  Consumers that need to stream messages can
    subscribe to ``on_event`` without having to mirror the tool loop.
    """

    kind: str
    run_id: str
    created_at: float
    payload: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def final(self) -> bool:
        return self.kind == "assistant_text" and bool(self.payload.get("final"))

    @property
    def text(self) -> str:
        return str(self.payload.get("text") or "")


@dataclass(slots=True)
class AgentRun:
    """Mutable state for one root or nested Agent execution."""

    run_id: str
    model: str
    depth: int = 0
    started_at: float = field(default_factory=time.time)
    tool_calls: int = 0
    rounds: int = 0
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class AgentRunContext:
    """Immutable metadata handed to event consumers and Sub-Agents."""

    run: AgentRun
    conversation: Any
    actor: Any = None
    memory: Any = None


class AgentEventSink(Protocol):
    async def on_event(self, event: AgentEvent) -> None:
        ...


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


async def notify_event(
    observer: AgentObserver | AgentEventSink | None,
    event: AgentEvent,
) -> None:
    """Deliver a structured event while preserving legacy callback behavior."""

    await notify_observer(observer, "on_event", event)


@dataclass(frozen=True, slots=True)
class AgentOptions:
    max_parallel_calls: int = 4
    total_timeout_seconds: float = 180.0
    max_depth: int = 1
    # Zero means unlimited for backwards compatibility.  A positive value
    # provides a guard against a provider repeatedly requesting tools.
    max_tool_calls: int = 0
    max_turns: int = 0

    def __post_init__(self) -> None:
        if self.max_parallel_calls < 1:
            raise ValueError("agent max_parallel_calls must be positive")
        if self.total_timeout_seconds <= 0:
            raise ValueError("agent total_timeout_seconds must be positive")
        if self.max_depth < 0:
            raise ValueError("agent max_depth cannot be negative")
        if self.max_tool_calls < 0:
            raise ValueError("agent max_tool_calls cannot be negative")
        if self.max_turns < 0:
            raise ValueError("agent max_turns cannot be negative")


class AgentKernel:
    """Event-driven tool-calling runtime.

    ``AgentRunner`` remains as a compatibility subclass below.  Keeping the
    implementation here gives Sub-Agent providers and new session managers a
    stable name while existing callers continue to use the old one.
    """

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

    def _new_run(self, model: str, depth: int) -> AgentRun:
        return AgentRun(run_id=uuid.uuid4().hex, model=str(model), depth=depth)

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
        run_id: str | None = None,
    ) -> str:
        run = AgentRun(
            run_id=str(run_id or uuid.uuid4().hex),
            model=str(model),
            depth=depth,
        )
        run_context = AgentRunContext(
            run=run,
            conversation=getattr(context, "conversation", None),
            actor=getattr(context, "actor", None),
            memory=getattr(context, "memory", None),
        )
        await notify_event(
            observer,
            AgentEvent(
                "run_started",
                run.run_id,
                time.time(),
                {"context": run_context},
            ),
        )
        specs = self._specs(context)
        complete = getattr(self.providers, "complete_agent_turn", None)
        if not callable(complete):
            complete = getattr(self.providers, "complete_request", None)
        stream = getattr(self.providers, "stream_agent_turn", None)
        supports = getattr(self.providers, "supports_tools", None)
        if (
            not enabled
            or not specs
            or (not callable(complete) and not callable(stream))
            or (callable(supports) and not supports(model))
        ):
            request = ChatRequest(
                message=message,
                history=tuple(history),
                system_prompt=system_prompt,
                attachments=tuple(attachments),
            )
            if callable(stream):
                response = await self._stream_or_interrupt(
                    stream,
                    model,
                    request,
                    interrupt_event,
                    observer=observer,
                    run=run,
                    run_context=run_context,
                )
                if response is None:
                    run.cancelled = True
                    raise AgentInterrupted()
                result = str(response.response.text or "")
            else:
                result = await self.providers.chat(
                    model,
                    message,
                    history=history,
                    system_prompt=system_prompt,
                    attachments=attachments,
                )
            await notify_event(
                observer,
                AgentEvent(
                    "assistant_text",
                    run.run_id,
                    time.time(),
                    {"text": str(result or ""), "final": True, "context": run_context},
                ),
            )
            await notify_observer(observer, "on_assistant_text", result, final=True)
            return result

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
                    run.rounds += 1
                    if (
                        self.options.max_turns > 0
                        and run.rounds > self.options.max_turns
                    ):
                        raise AgentError("agent_max_turns", "Agent 达到最大轮数。")
                    if interrupt_event is not None and interrupt_event.is_set():
                        run.cancelled = True
                        await notify_event(
                            observer,
                            AgentEvent(
                                "interrupted",
                                run.run_id,
                                time.time(),
                                {"reason": "superseded", "context": run_context},
                            ),
                        )
                        await notify_observer(
                            observer,
                            "on_interrupt",
                            "superseded",
                        )
                        raise AgentInterrupted()
                    try:
                        request = ChatRequest(
                            message=message,
                            history=tuple(history),
                            system_prompt=system_prompt,
                            attachments=tuple(attachments),
                            tools=declarations,
                            turns=tuple(turns),
                        )
                        if callable(stream):
                            streamed = await self._stream_or_interrupt(
                                stream,
                                model,
                                request,
                                interrupt_event,
                                request_id=run.run_id,
                                observer=observer,
                                run=run,
                                run_context=run_context,
                            )
                            response = (
                                streamed.response if streamed is not None else None
                            )
                            streamed_text = (
                                streamed.streamed_text if streamed is not None else False
                            )
                        else:
                            response = await self._complete_or_interrupt(
                                complete,
                                model,
                                request,
                                interrupt_event,
                                request_id=run.run_id,
                            )
                            streamed_text = False
                    except EmptyProviderResponseError:
                        safe_log_info(
                            self._logger,
                            "JianerAI Agent 工具请求返回空响应，回退到普通生成",
                        )
                        result = await self.providers.chat(
                            model,
                            message,
                            history=history,
                            system_prompt=system_prompt,
                            attachments=attachments,
                        )
                        await notify_event(
                            observer,
                            AgentEvent(
                                "assistant_text",
                                run.run_id,
                                time.time(),
                                {
                                    "text": str(result or ""),
                                    "final": True,
                                    "context": run_context,
                                },
                            ),
                        )
                        return result
                    if response is None:
                        run.cancelled = True
                        await notify_event(
                            observer,
                            AgentEvent(
                                "interrupted",
                                run.run_id,
                                time.time(),
                                {"reason": "superseded", "context": run_context},
                            ),
                        )
                        await notify_observer(
                            observer,
                            "on_interrupt",
                            "superseded",
                        )
                        raise AgentInterrupted()
                    text = str(response.text or "").rstrip()
                    if not response.tool_calls:
                        if text:
                            await notify_event(
                                observer,
                                AgentEvent(
                                    "assistant_text",
                                    run.run_id,
                                    time.time(),
                                    {
                                        "text": text,
                                        "final": True,
                                        "context": run_context,
                                    },
                                ),
                            )
                            await notify_observer(
                                observer,
                                "on_assistant_text",
                                text,
                                final=True,
                            )
                        return text
                    if text and not streamed_text:
                        await notify_event(
                            observer,
                            AgentEvent(
                                "assistant_text",
                                run.run_id,
                                time.time(),
                                {
                                    "text": text,
                                    "final": False,
                                    "context": run_context,
                                },
                            ),
                        )
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
                    run.tool_calls += len(calls)
                    if (
                        self.options.max_tool_calls > 0
                        and run.tool_calls > self.options.max_tool_calls
                    ):
                        raise AgentError(
                            "agent_max_tool_calls",
                            "Agent 达到最大工具调用次数。",
                        )
                    executed = await self._execute_calls(
                        [call for _, call in calls],
                        context,
                        observer=observer,
                        run=run,
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
            result = await self.providers.chat(
                model,
                message,
                history=history,
                system_prompt=system_prompt,
                attachments=attachments,
            )
            await notify_event(
                observer,
                AgentEvent(
                    "assistant_text",
                    run.run_id,
                    time.time(),
                    {"text": str(result or ""), "final": True, "context": run_context},
                ),
            )
            return result
        except TimeoutError as exc:
            raise AgentError("agent_timeout", "Agent 执行超过总时限。") from exc

    async def _emit_tool_start(
        self,
        observer: AgentObserver | None,
        call: ToolCall,
        run: AgentRun,
        context: ToolContext,
    ) -> None:
        await notify_event(
            observer,
            AgentEvent(
                "tool_started",
                run.run_id,
                time.time(),
                {"call": call, "context": context, "run": run},
            ),
        )

    async def _emit_tool_result(
        self,
        observer: AgentObserver | None,
        call: ToolCall,
        result: Any,
        run: AgentRun,
        context: ToolContext,
    ) -> None:
        await notify_event(
            observer,
            AgentEvent(
                "tool_finished",
                run.run_id,
                time.time(),
                {
                    "call": call,
                    "result": result,
                    "context": context,
                    "run": run,
                },
            ),
        )

    async def _stream_or_interrupt(
        self,
        provider_method: Any,
        model: str,
        request: ChatRequest,
        interrupt_event: asyncio.Event | None,
        *,
        request_id: str | None = None,
        observer: AgentObserver | None = None,
        run: AgentRun | None = None,
        run_context: AgentRunContext | None = None,
    ) -> _StreamResult | None:
        """Consume a provider stream, returning ``None`` when superseded."""

        if getattr(provider_method, "__name__", "") == "complete_agent_turn":
            return await self._complete_or_interrupt(
                provider_method,
                model,
                request,
                interrupt_event,
                request_id=request_id,
            )

        async def consume() -> _StreamResult:
            kwargs: dict[str, Any] = {}
            if request_id:
                try:
                    parameters = inspect.signature(provider_method).parameters
                except (TypeError, ValueError):
                    parameters = {}
                if "request_id" in parameters:
                    kwargs["request_id"] = request_id
            value = provider_method(model, request, **kwargs)
            if inspect.isawaitable(value):
                value = await value
            if isinstance(value, ProviderResponse):
                return _StreamResult(value, False)
            if not hasattr(value, "__aiter__"):
                raise EmptyProviderResponseError(
                    "provider stream returned a non-iterable response"
                )
            final: ProviderResponse | None = None
            streamed_text = False
            iterator = value.__aiter__()
            try:
                async for raw_event in iterator:
                    if isinstance(raw_event, ProviderResponse):
                        final = raw_event
                        continue
                    if not isinstance(raw_event, ProviderStreamEvent):
                        continue
                    if raw_event.kind == "text_delta" and raw_event.text:
                        streamed_text = True
                        await notify_event(
                            observer,
                            AgentEvent(
                                "assistant_delta",
                                run.run_id if run else "",
                                time.time(),
                                {
                                    "text": raw_event.text,
                                    "final": False,
                                    "context": run_context,
                                },
                            ),
                        )
                        await notify_observer(
                            observer,
                            "on_assistant_delta",
                            raw_event.text,
                        )
                    elif raw_event.kind == "tool_call_delta":
                        await notify_event(
                            observer,
                            AgentEvent(
                                "tool_call_delta",
                                run.run_id if run else "",
                                time.time(),
                                {"context": run_context},
                            ),
                        )
                        await notify_observer(observer, "on_tool_call_delta")
                    if raw_event.response is not None:
                        final = raw_event.response
            finally:
                close = getattr(iterator, "aclose", None)
                if callable(close):
                    with contextlib.suppress(Exception):
                        await close()
            if final is None:
                raise EmptyProviderResponseError("provider stream returned no response")
            return _StreamResult(final, streamed_text)

        if interrupt_event is None:
            return await consume()
        call_task = asyncio.ensure_future(consume())
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
        cancel = getattr(self.providers, "cancel_request", None)
        if request_id and callable(cancel):
            with contextlib.suppress(Exception):
                value = cancel(request_id)
                if asyncio.iscoroutine(value):
                    await value
        return None

    async def _complete_or_interrupt(
        self,
        complete: Any,
        model: str,
        request: ChatRequest,
        interrupt_event: asyncio.Event | None,
        *,
        request_id: str | None = None,
    ) -> Any | None:
        if interrupt_event is None:
            return await self._invoke_provider(
                complete, model, request, request_id=request_id
            )
        call_task = asyncio.ensure_future(
            self._invoke_provider(complete, model, request, request_id=request_id)
        )
        wait_task = asyncio.ensure_future(interrupt_event.wait())
        try:
            done, pending = await asyncio.wait(
                {call_task, wait_task}, return_when=asyncio.FIRST_COMPLETED
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
            await asyncio.gather(call_task, wait_task, return_exceptions=True)
        cancel = getattr(self.providers, "cancel_request", None)
        if request_id and callable(cancel):
            with contextlib.suppress(Exception):
                value = cancel(request_id)
                if asyncio.iscoroutine(value):
                    await value
        return None

    @staticmethod
    async def _invoke_provider(
        complete: Any,
        model: str,
        request: ChatRequest,
        *,
        request_id: str | None,
    ) -> Any:
        kwargs: dict[str, Any] = {}
        if request_id:
            try:
                parameters = inspect.signature(complete).parameters
            except (TypeError, ValueError):
                parameters = {}
            if "request_id" in parameters:
                kwargs["request_id"] = request_id
        value = complete(model, request, **kwargs)
        if inspect.isawaitable(value):
            return await value
        return value

    async def _execute_calls(
        self,
        calls: Sequence[ToolCall],
        context: ToolContext,
        *,
        observer: AgentObserver | None = None,
        run: AgentRun | None = None,
    ) -> tuple[Any, ...]:
        semaphore = asyncio.Semaphore(self.options.max_parallel_calls)

        async def execute(call: ToolCall):
            async with semaphore:
                self._log_tool_start(call, context)
                if run is not None:
                    await self._emit_tool_start(observer, call, run, context)
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
                if run is not None:
                    await self._emit_tool_result(
                        observer,
                        call,
                        result,
                        run,
                        context,
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


class AgentRunner(AgentKernel):
    """Compatibility name for the pre-Kernel public API."""

    pass
