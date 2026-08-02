from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from plugins.JianerAI.providers import (
    ChatRequest,
    FunctionTool,
    MediaAttachment,
    ProviderRegistry,
    ToolResultTurn,
    ToolsUnsupportedError,
)
from plugins.JianerAI.tools import (
    ToolCall,
    ToolContext,
    ToolRegistry,
    limit_result,
)


class AgentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)


@dataclass(frozen=True, slots=True)
class AgentOptions:
    max_tool_calls: int = 8
    max_parallel_calls: int = 4
    total_timeout_seconds: float = 180.0

    def __post_init__(self) -> None:
        if self.max_tool_calls < 1:
            raise ValueError("agent max_tool_calls must be positive")
        if self.max_parallel_calls < 1:
            raise ValueError("agent max_parallel_calls must be positive")
        if self.total_timeout_seconds <= 0:
            raise ValueError("agent total_timeout_seconds must be positive")


class AgentRunner:
    def __init__(
        self,
        providers: ProviderRegistry,
        tools: ToolRegistry,
        *,
        options: AgentOptions = AgentOptions(),
        allowed_tool_names: frozenset[str] | None = None,
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
    ) -> str:
        specs = tuple(
            spec
            for spec in self.tools.available(context)
            if self.allowed_tool_names is None
            or spec.name in self.allowed_tool_names
        )
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
        used_calls = 0
        try:
            async with asyncio.timeout(self.options.total_timeout_seconds):
                while True:
                    response = await complete(
                        model,
                        ChatRequest(
                            message=message,
                            history=tuple(history),
                            system_prompt=system_prompt,
                            attachments=tuple(attachments),
                            tools=declarations,
                            turns=tuple(turns),
                        ),
                    )
                    if not response.tool_calls:
                        return str(response.text or "").rstrip()
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
                            results_by_index[index] = limit_result(
                                call, code="duplicate_tool_call_id"
                            )
                            continue
                        seen_call_ids.add(call.id)
                        if used_calls >= self.options.max_tool_calls:
                            results_by_index[index] = limit_result(call)
                            continue
                        used_calls += 1
                        calls.append((index, call))
                    executed = await self._execute_calls(
                        [call for _, call in calls], context
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

    async def _execute_calls(
        self,
        calls: Sequence[ToolCall],
        context: ToolContext,
    ) -> tuple[Any, ...]:
        semaphore = asyncio.Semaphore(self.options.max_parallel_calls)

        async def execute(call: ToolCall):
            async with semaphore:
                return await self.tools.execute(call, context)

        if not calls:
            return ()
        return tuple(await asyncio.gather(*(execute(call) for call in calls)))
