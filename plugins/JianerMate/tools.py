"""Tool calling for JianerMate.

The plugin drives the OpenAI ``tools`` protocol itself and reuses the tool
implementations that already live in ``plugins/JianerAI/tools`` (time, maths,
profile/chat info, chat history search, web search, GitHub, weather, HTML
cards, platform APIs).  The JianerAI plugin does not have to be enabled: only
its modules are imported, and the import is optional so JianerMate keeps
working (without tools) if that package is missing.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from jianer import common as Manager
from jianer import segments as Segments

from plugins.JianerMate.config import ToolOptions
from plugins.JianerMate.compat import ensure_jianer_ai


ensure_jianer_ai()


_LOGGER = logging.getLogger("jianer_mate.tools")


class MateMessageSink:
    """Minimal sink expected by JianerAI's privileged tools (e.g. ``bash``)."""

    def __init__(self, event: Any, actions: Any) -> None:
        self.event = event
        self.actions = actions

    @staticmethod
    def _target(event: Any) -> dict[str, Any]:
        group_id = getattr(event, "group_id", None)
        if group_id is not None:
            return {"group_id": group_id}
        return {"user_id": getattr(event, "user_id", None)}

    async def send(
        self,
        text: str,
        *,
        at_user_ids: Sequence[str] = (),
        reply_to_message_id: str | None = None,
        source: str = "agent",
        **kwargs: Any,
    ) -> str:
        del reply_to_message_id, source, kwargs
        segments: list[Any] = []
        for user_id in at_user_ids:
            mention = str(user_id or "").strip()
            if mention:
                segments.append(Segments.At(mention))
        segments.append(Segments.Text(str(text)))
        result = await self.actions.send(
            message=Manager.Message(*segments), **self._target(self.event)
        )
        data = getattr(result, "data", None)
        message_id = getattr(data, "message_id", None)
        return "" if message_id is None else str(message_id)


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

    def __init__(
        self,
        store: Any,
        conversation_ref: int,
        *,
        long_term: Any | None = None,
        preset: str = "mate",
    ) -> None:
        self._store = store
        self._conversation_ref = int(conversation_ref)
        self._long_term = long_term
        self._preset = str(preset or "mate")

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

    def _long_term_call(self, name: str) -> Any:
        target = getattr(self._long_term, name, None)
        if target is None or not getattr(self._long_term, "available", False):
            detail = getattr(self._long_term, "error", "") if self._long_term else ""
            raise _tool_error(
                "long_memory_disabled",
                "长期记忆不可用" + (f"（{detail}）" if detail else "。"),
            )
        return target

    def _scoped(self, name: str, kwargs: dict[str, Any]) -> Any:
        kwargs.setdefault("preset", self._preset)
        canonical = str(kwargs.get("canonical_user_id") or "")
        if canonical:
            self._ensure_identity(canonical)
        return self._long_term_call(name)(**kwargs)

    def _ensure_identity(self, canonical_user_id: str) -> None:
        """Register the canonical identity on first use (legacy store needs it)."""

        if not getattr(self._long_term, "available", False):
            return
        ensure = getattr(self._long_term, "ensure_identity", None)
        if not callable(ensure):
            return
        protocol, _, external = canonical_user_id.partition(":")
        try:
            ensure(
                protocol=protocol or "qq",
                self_id="",
                external_id=external or canonical_user_id,
            )
        except Exception:  # noqa: BLE001 - memory errors surface on the call
            pass

    def list_scoped_memories(self, **kwargs: Any) -> Any:
        return self._scoped("list_scoped_memories", kwargs)

    def create_scoped_memory(self, **kwargs: Any) -> Any:
        return self._scoped("create_scoped_memory", kwargs)

    def update_scoped_memory(self, **kwargs: Any) -> Any:
        return self._scoped("update_scoped_memory", kwargs)

    def add_scoped_memory_evidence(self, **kwargs: Any) -> Any:
        return self._scoped("add_scoped_memory_evidence", kwargs)

    def list_memories(self, **kwargs: Any) -> Any:
        return self._scoped("list_memories", kwargs)

    def create_memory(self, **kwargs: Any) -> Any:
        return self._scoped("create_memory", kwargs)

    def update_memory(self, **kwargs: Any) -> Any:
        return self._scoped("update_memory", kwargs)


class MateToolRuntime:
    """Registry wrapper that exposes JianerAI's tools to JianerMate."""

    def __init__(
        self,
        options: ToolOptions,
        *,
        project_root: str | Path,
        logger: Any | None = None,
        runtime: Mapping[str, Any] | None = None,
    ) -> None:
        self.options = options
        self.project_root = Path(project_root)
        self._logger = logger or _LOGGER
        self._runtime: dict[str, Any] = dict(runtime or {})
        self._import_error = ""
        self._registry: Any | None = None
        self._tool_error: Any | None = None
        self._extra_tools: set[str] = set()
        self._mcp_loaded = False
        self._shell_manager: Any | None = None
        self.subagent_runner: Any | None = None
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
        mutating_tools: frozenset[str] | None = None
        if self.options.allow_mutating:
            risks.add(ToolRisk.MUTATING)
        if self.options.bash_enabled:
            # ``bash`` is a mutating tool guarded by its own approval queue.
            risks.add(ToolRisk.MUTATING)
            if not self.options.allow_mutating:
                mutating_tools = frozenset({"bash"})
        registry = ToolRegistry(
            allowed_risks=frozenset(risks),
            allowed_mutating_tools=mutating_tools,
        )
        try:
            register_builtin_tools(
                registry,
                include_web_browser=self.options.include_web_browser,
                project_root=self.project_root,
                logger=self._logger,
            )
            # Read-only platform APIs (milky_get_*, onebot_get_*, Lark GET)
            # stay available; mutating ones are filtered by the risk filter
            # unless ``allow_mutating`` is enabled.
            register_platform_api_tools(registry)
            self._register_skills(registry)
            self._register_bash(registry)
            self._register_subagents(registry)
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

    async def load_external_tools(self, manager: Any) -> tuple[str, ...]:
        """Register tools exported by other loaded plugins (opt-in hooks)."""

        if self._registry is None or manager is None:
            return ()
        try:
            from plugins.JianerAI.tools.loaded_plugins import (  # noqa: PLC0415
                load_active_plugin_tools,
            )
        except Exception as exc:  # noqa: BLE001 - optional
            self._log(
                "info", f"第三方插件工具加载器不可用：{exc.__class__.__name__}"
            )
            return ()
        try:
            return tuple(
                await load_active_plugin_tools(
                    self._registry,
                    manager,
                    runtime=self._runtime,
                    service=None,
                    logger=self._logger,
                )
            )
        except Exception as exc:  # noqa: BLE001
            self._log(
                "warning", f"加载第三方插件工具失败：{exc.__class__.__name__}"
            )
            return ()

    async def ensure_ready(self) -> None:
        """Register providers that need async setup (MCP servers)."""

        if self._registry is None or self._mcp_loaded:
            return
        self._mcp_loaded = True
        if not self.options.mcp_enabled or not self.options.mcp_servers:
            return
        try:
            from plugins.JianerAI.tools.mcp_client import (  # noqa: PLC0415
                MCPToolPlugin,
            )
            from plugins.JianerAI.tools.plugin_registry import (  # noqa: PLC0415
                ToolPluginManager,
            )
        except Exception as exc:  # noqa: BLE001 - optional
            self._log("warning", f"MCP 支持不可用：{exc.__class__.__name__}")
            return
        try:
            provider = MCPToolPlugin(self.options.mcp_servers)
            await ToolPluginManager(self._registry).load_async(provider)
            errors = list(getattr(provider, "errors", None) or ())
            loaded = [
                spec.name
                for spec in self._registry.specs
                if spec.name.startswith("mcp_")
            ]
            self._log(
                "info",
                f"MCP 工具已加载 {len(loaded)} 个"
                + (f"，{len(errors)} 个服务不可用" if errors else ""),
            )
        except Exception as exc:  # noqa: BLE001 - never break chat over MCP
            self._log("warning", f"MCP 工具加载失败：{exc.__class__.__name__}")

    def register_tool(self, spec: Any) -> Any:
        """Public API for sibling plugins (mirrors JianerAI's register_tool)."""

        if self._registry is None:
            raise RuntimeError("工具系统不可用")
        registration = self._registry.register(spec)
        self._extra_tools.add(str(getattr(spec, "name", "")))
        return registration

    def unregister_tool(self, registration: Any) -> bool:
        if self._registry is None:
            return False
        name = str(getattr(registration, "name", ""))
        removed = bool(self._registry.unregister(registration))
        if removed and name:
            self._extra_tools.discard(name)
        return removed

    def _register_skills(self, registry: Any) -> None:
        roots = []
        for item in self.options.skills_dirs:
            path = Path(item)
            if not path.is_absolute():
                path = self.project_root / path
            if path.is_dir():
                roots.append(path)
        if not roots:
            return
        try:
            from plugins.JianerAI.tools.agent_skills import (  # noqa: PLC0415
                AgentSkillManager,
            )
        except Exception as exc:  # noqa: BLE001 - skills are optional
            self._log(
                "info", f"Agent Skills 未加载：{exc.__class__.__name__}"
            )
            return
        manager = AgentSkillManager(
            roots, max_file_bytes=self.options.skills_max_file_bytes
        )
        specs = manager.provide_tools()
        if not specs:
            self._log("info", "Agent Skills 目录为空，未注册技能工具")
            return
        for spec in specs:
            try:
                registry.register(spec)
            except Exception as exc:  # noqa: BLE001
                self._log("warning", f"注册技能工具失败：{exc}")

    def _register_bash(self, registry: Any) -> None:
        """Register the admin-only Shell tool with its approval queue."""

        if not self.options.bash_enabled:
            return
        try:
            from plugins.JianerAI.tools.bash import (  # noqa: PLC0415
                ShellApprovalManager,
            )
        except Exception as exc:  # noqa: BLE001 - optional
            self._log("warning", f"Shell 工具不可用：{exc.__class__.__name__}")
            return
        try:
            manager = ShellApprovalManager(
                project_root=self.project_root,
                approval_timeout_seconds=self.options.bash_approval_timeout_seconds,
                command_timeout_seconds=self.options.bash_command_timeout_seconds,
            )
            spec = manager.tool()
            registry.register(spec)
        except Exception as exc:  # noqa: BLE001
            self._log("warning", f"Shell 工具注册失败：{exc.__class__.__name__}")
            return
        self._shell_manager = manager
        self._extra_tools.add(str(getattr(spec, "name", "bash")))
        self._log("info", "Shell 工具已注册（仅机器人管理员，需人工批准）")

    def resolve_shell(
        self,
        *,
        request_id: str,
        scope: Sequence[str],
        speaker_external_id: str,
        approve: bool,
    ) -> str:
        manager = self._shell_manager
        if manager is None:
            return "unavailable"
        return manager.resolve(
            request_id=str(request_id),
            scope=tuple(str(item) for item in scope),
            speaker_external_id=str(speaker_external_id or ""),
            approve=bool(approve),
        )

    def set_subagent_runner(self, runner: Any) -> None:
        """Service-provided coroutine: ``runner(prompt, tool_names, context)``."""

        self.subagent_runner = runner

    def tool_error(self, code: str, message: str) -> Any:
        return _tool_error(code, message)

    def _register_subagents(self, registry: Any) -> None:
        if not self.options.subagents_enabled:
            return
        try:
            spec = _subagent_spec(self)
            registry.register(spec)
        except Exception as exc:  # noqa: BLE001
            self._log(
                "warning", f"子代理工具注册失败：{exc.__class__.__name__}"
            )
            return
        self._extra_tools.add("spawn_subagents")
        self._log("info", "子代理工具已注册（只读子任务，默认关闭）")

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
            if (
                allowed is not None
                and spec.name not in allowed
                and spec.name not in self._extra_tools
            ):
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
        if (
            self.options.allowlist is not None
            and name not in self.options.allowlist
            and name not in self._extra_tools
        ):
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
            names = [
                name
                for name in names
                if name in allowed or name in self._extra_tools
            ]
        return {
            "enabled": True,
            "available": True,
            "error": "",
            "tools": len(names),
            "names": names,
        }

    def all_tool_names(self) -> tuple[str, ...]:
        """Every registered tool name, ignoring the per-call allowlist."""

        if self._registry is None:
            return ()
        return tuple(sorted(spec.name for spec in self._registry.specs))

    async def shutdown(self) -> None:
        manager, self._shell_manager = self._shell_manager, None
        if manager is not None:
            try:
                manager.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
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


def _subagent_spec(runtime: "MateToolRuntime") -> Any:
    """Build the ``spawn_subagents`` spec backed by the service runner."""

    from plugins.JianerAI.tools import ToolRisk, ToolSpec  # noqa: PLC0415

    max_tasks = max(1, int(runtime.options.subagent_max_tasks))
    timeout = max(5.0, float(runtime.options.subagent_timeout_seconds)) + 30.0

    async def handler(context: Any, arguments: Mapping[str, Any]) -> Any:
        runner = runtime.subagent_runner
        if runner is None:
            raise runtime.tool_error(
                "subagents_unavailable", "子代理运行器不可用。"
            )
        raw_tasks = arguments.get("tasks")
        if not isinstance(raw_tasks, Sequence) or isinstance(
            raw_tasks, (str, bytes)
        ):
            raise runtime.tool_error(
                "invalid_subagent_tasks", "tasks 必须是非空数组。"
            )
        tasks = [item for item in raw_tasks if isinstance(item, Mapping)][:max_tasks]
        if not tasks:
            raise runtime.tool_error(
                "invalid_subagent_tasks", "至少需要一个子任务。"
            )
        outcomes = await asyncio.gather(
            *(
                runner(
                    str(item.get("prompt") or ""),
                    tuple(
                        str(name) for name in (item.get("tools") or ())
                    ),
                    context,
                )
                for item in tasks
            ),
            return_exceptions=True,
        )
        results: list[dict[str, Any]] = []
        for item, outcome in zip(tasks, outcomes):
            prompt = str(item.get("prompt") or "")
            if isinstance(outcome, BaseException):
                results.append(
                    {
                        "prompt": prompt,
                        "ok": False,
                        "error": outcome.__class__.__name__,
                    }
                )
            else:
                results.append(
                    {
                        "prompt": prompt,
                        "ok": True,
                        "result": str(outcome)[:2000],
                    }
                )
        return {"results": results}

    return ToolSpec(
        name="spawn_subagents",
        description=(
            "把一个耗时任务拆给若干只读子代理并行处理，返回它们各自的结论。"
            "子代理不能再派生、不能发消息、不能写长期记忆。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": max_tasks,
                    "items": {
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string", "minLength": 1},
                            "tools": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["prompt"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["tasks"],
            "additionalProperties": False,
        },
        handler=handler,
        risk=ToolRisk.READ_ONLY,
        timeout_seconds=timeout,
        max_output_chars=6000,
    )


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
    message_sink: Any = None,
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
        message_sink=message_sink,
    )


