"""Tool calling for JianerMate.

The plugin drives the OpenAI ``tools`` protocol itself and reuses the tool
implementations that already live in ``plugins/JianerAI/tools`` (time, maths,
profile/chat info, chat history search, web search, GitHub, weather, HTML
cards, platform APIs).  The JianerAI plugin does not have to be enabled: only
its modules are imported, and the import is optional so JianerMate keeps
working (without tools) if that package is missing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from plugins.JianerMate.config import ToolOptions


_LOGGER = logging.getLogger("jianer_mate.tools")


@dataclass(frozen=True, slots=True)
class ChatHistoryRecord:
    """Row shape expected by JianerAI's ``read_recent_chat`` tool."""

    id: str
    external_message_id: str
    direction: str
    sender_name: str
    sender_canonical_id: str
    content: str
    occurred_at: int
    message_type: str


class MateMemoryFacade:
    """Adapter that lets JianerAI tools read JianerMate's chat history."""

    def __init__(self, store: Any, conversation_ref: int) -> None:
        self._store = store
        self._conversation_ref = int(conversation_ref)

    def query_recent_chat(
        self,
        *,
        protocol: str = "",
        self_id: str = "",
        conversation_kind: str = "",
        conversation_id: str = "",
        limit: int = 20,
        max_characters: int = 8000,
        query: str = "",
    ) -> tuple[ChatHistoryRecord, ...]:
        del protocol, self_id, conversation_kind, conversation_id
        records = self._store.recent_messages(
            self._conversation_ref, limit=max(1, int(limit))
        )
        needle = str(query or "").strip().casefold()
        output: list[ChatHistoryRecord] = []
        budget = max(200, int(max_characters))
        for record in reversed(tuple(records)):  # newest first, like JianerAI
            content = str(getattr(record, "content", "") or "")
            if needle and needle not in content.casefold():
                continue
            budget -= len(content)
            if budget < 0:
                break
            output.append(
                ChatHistoryRecord(
                    id=str(getattr(record, "id", "")),
                    external_message_id=str(
                        getattr(record, "platform_message_id", "") or ""
                    ),
                    direction=str(getattr(record, "direction", "incoming")),
                    sender_name=str(getattr(record, "sender_name", "") or ""),
                    sender_canonical_id=str(
                        getattr(record, "sender_id", "") or ""
                    ),
                    content=content,
                    occurred_at=int(getattr(record, "created_at", 0) or 0),
                    message_type=str(
                        getattr(record, "message_type", "text") or "text"
                    ),
                )
            )
        return tuple(output)

    # JianerMate has no long-term memory backend: report that clearly instead
    # of letting the original tools fail with an AttributeError.
    def _long_memory_disabled(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise _tool_error(
            "long_memory_disabled",
            "本插件没有启用长期记忆，无法读写长期记忆。",
        )

    list_scoped_memories = _long_memory_disabled
    create_scoped_memory = _long_memory_disabled
    update_scoped_memory = _long_memory_disabled
    add_scoped_memory_evidence = _long_memory_disabled
    list_memories = _long_memory_disabled
    create_memory = _long_memory_disabled
    update_memory = _long_memory_disabled


class MateToolRuntime:
    """Registry wrapper that exposes JianerAI's tools to JianerMate."""

    def __init__(
        self,
        options: ToolOptions,
        *,
        project_root: str | Path,
        logger: Any | None = None,
    ) -> None:
        self.options = options
        self.project_root = Path(project_root)
        self._logger = logger or _LOGGER
        self._import_error = ""
        self._registry: Any | None = None
        self._tool_error: Any | None = None
        if options.enabled:
            self._build()

    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        try:
            from plugins.JianerAI.tools import (  # noqa: PLC0415 - optional
                ToolCall,
                ToolExecutionError,
                ToolRegistry,
                ToolRisk,
                register_builtin_tools,
                register_platform_api_tools,
            )
            from plugins.JianerAI.tools.qweather import register_qweather_tools
        except Exception as exc:  # noqa: BLE001 - tools are optional
            self._import_error = f"{exc.__class__.__name__}: {exc}"
            self._log(
                "warning",
                "未能加载 JianerAI 的工具实现，工具调用已禁用：" + self._import_error,
            )
            return
        self._tool_call = ToolCall
        self._tool_error = ToolExecutionError
        risks = {ToolRisk.READ_ONLY, ToolRisk.PRESENTATION}
        if self.options.allow_mutating:
            risks.add(ToolRisk.MUTATING)
        registry = ToolRegistry(allowed_risks=frozenset(risks))
        try:
            register_builtin_tools(
                registry,
                include_web_browser=self.options.include_web_browser,
                project_root=self.project_root,
                logger=self._logger,
            )
            if self.options.allow_mutating:
                register_platform_api_tools(registry)
            try:
                register_qweather_tools(
                    registry,
                    project_root=self.project_root,
                    logger=self._logger,
                )
            except Exception as exc:  # noqa: BLE001 - weather is optional
                self._log(
                    "info",
                    f"和风天气工具未注册：{exc.__class__.__name__}",
                )
        except Exception as exc:  # noqa: BLE001
            self._import_error = f"{exc.__class__.__name__}: {exc}"
            self._log("error", "工具注册失败：" + self._import_error)
            registry.close()
            return
        self._registry = registry

    @property
    def available(self) -> bool:
        return self._registry is not None

    @property
    def import_error(self) -> str:
        return self._import_error

    # ------------------------------------------------------------------ #
    # model-facing helpers
    # ------------------------------------------------------------------ #
    def schemas(self, context: Any) -> tuple[Mapping[str, Any], ...]:
        if self._registry is None:
            return ()
        allowed = self.options.allowlist
        output: list[Mapping[str, Any]] = []
        for spec in self._registry.available(context):
            if allowed is not None and spec.name not in allowed:
                continue
            output.append(
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": dict(spec.input_schema),
                    },
                }
            )
        return tuple(output)

    async def execute(
        self,
        call_id: str,
        name: str,
        arguments: Mapping[str, Any],
        context: Any,
    ) -> str:
        if self._registry is None:
            return "工具系统不可用。"
        spec_names = {spec.name for spec in self._registry.available(context)}
        if name not in spec_names:
            return "该工具不存在或当前不可用。"
        if self.options.allowlist is not None and name not in self.options.allowlist:
            return "该工具不在允许列表里，当前不可用。"
        result = await self._registry.execute(
            self._tool_call(id=str(call_id), name=str(name), arguments=dict(arguments)),
            context,
        )
        content = str(getattr(result, "content", "") or "")
        limit = max(200, int(self.options.max_output_chars))
        return content[:limit]

    def status(self) -> dict[str, Any]:
        if self._registry is None:
            return {
                "enabled": bool(self.options.enabled),
                "available": False,
                "error": self._import_error,
                "tools": 0,
            }
        names = sorted(spec.name for spec in self._registry.specs)
        allowed = self.options.allowlist
        if allowed is not None:
            names = [name for name in names if name in allowed]
        return {
            "enabled": True,
            "available": True,
            "error": "",
            "tools": len(names),
            "names": names,
        }

    async def shutdown(self) -> None:
        registry, self._registry = self._registry, None
        if registry is not None:
            try:
                await registry.shutdown()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass

    def _log(self, level: str, message: str) -> None:
        handler = getattr(self._logger, level, None)
        if callable(handler):
            handler(message)
            return
        fallback = getattr(self._logger, "info", None)
        if callable(fallback):
            fallback(message)


def _tool_error(code: str, message: str) -> Any:
    try:
        from plugins.JianerAI.tools import ToolExecutionError  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return RuntimeError(message)
    return ToolExecutionError(code, message)


def make_actor_resolver() -> Any | None:
    """Reuse JianerAI's permission resolver when its module is importable."""

    try:
        from plugins.JianerAI.permissions import ActorResolver  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - permissions are optional
        return None
    return ActorResolver()


def build_tool_context(
    *,
    event: Any,
    actions: Any,
    conversation: Any,
    canonical_user_id: str,
    runtime: Mapping[str, Any],
    memory: Any,
    actor: Any,
    model: str,
) -> Any | None:
    """Create JianerAI's ``ToolContext`` for one JianerMate turn."""

    try:
        from plugins.JianerAI.tools import ToolContext  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - tools are optional
        return None
    return ToolContext(
        event=event,
        actions=actions,
        conversation=conversation,
        canonical_user_id=str(canonical_user_id or ""),
        runtime=runtime,
        memory=memory,
        actor=actor,
        model=str(model or ""),
    )


