"""Main and System context management."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from plugins.JianerAI.agent_runtime.core import AgentCore
from plugins.JianerAI.agent_runtime.models import AgentRole, SessionKey
from plugins.JianerAI.agent_runtime.profiles import ProfileStore
from plugins.JianerAI.agent_runtime.prompts import (
    OUTPUT_RULE,
    SYSTEM_CONTEXT_PROMPT,
    build_main_prompt,
)
from plugins.JianerAI.agent_runtime.store import ContextStore
from plugins.JianerAI.providers import ProviderRegistry
from plugins.JianerAI.tools.contracts import ToolSpec
from plugins.JianerAI.tools.registry import ToolRegistry


class AgentSessionManager:
    """Own the persistent Main contexts and one System context."""

    def __init__(
        self,
        *,
        store: ContextStore,
        providers: ProviderRegistry,
        tools: ToolRegistry,
        profiles: ProfileStore,
        model_resolver: Callable[[SessionKey], str],
        owner_ids: tuple[str, ...],
        bot_name: str = "简儿",
        bot_name_en: str = "Jianer",
        allowed_tool_names: frozenset[str] | None = None,
        total_timeout_seconds: float = 600.0,
        max_history_chars: int = 60000,
        idle_seconds: float = 12 * 3600,
        system_request_timeout: float = 300.0,
        logger: Any | None = None,
    ) -> None:
        self.store = store
        self.providers = providers
        self.tools = tools
        self.profiles = profiles
        self.model_resolver = model_resolver
        self.owner_ids = tuple(str(item) for item in owner_ids)
        self.bot_name = str(bot_name or "简儿")
        self.bot_name_en = str(bot_name_en or "Jianer")
        self.allowed_tool_names = allowed_tool_names
        self.total_timeout_seconds = total_timeout_seconds
        self.max_history_chars = max_history_chars
        self.idle_seconds = max(60.0, float(idle_seconds))
        self.system_request_timeout = max(
            5.0, float(system_request_timeout)
        )
        self.logger = logger
        self._contexts: dict[SessionKey, AgentCore] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task[Any] | None = None
        self._pending_requests: dict[str, SessionKey] = {}
        self._request_tasks: set[asyncio.Task[Any]] = set()
        self.system = self._create(SessionKey.system(), AgentRole.SYSTEM)
        self._ensure_cleanup()

    async def get_main(self, key: SessionKey) -> AgentCore:
        async with self._lock:
            core = self._contexts.get(key)
            if core is None:
                core = self._create(key, AgentRole.MAIN)
                self._ensure_cleanup()
            core.last_used = asyncio.get_running_loop().time()
            return core

    def loaded(self) -> tuple[AgentCore, ...]:
        return (self.system, *self._contexts.values())

    async def list_contexts(self) -> str:
        for key in self.store.list_sessions():
            if key not in self._contexts:
                await self.get_main(key)
        lines: list[str] = []
        for key, core in sorted(
            self._contexts.items(),
            key=lambda item: item[0].value,
        ):
            await core.ensure_loaded()
            lines.append(
                f"- {key.value}: {len(core.history)} 条历史"
            )
        return "Main Context:\n" + ("\n".join(lines) if lines else "- (空)")

    async def read_context(
        self,
        target: SessionKey,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        core = await self.get_main(target)
        await core.ensure_loaded()
        return [dict(item) for item in core.history[-max(1, int(limit)) :]]

    async def context_status(self, target: SessionKey) -> str:
        core = await self.get_main(target)
        await core.ensure_loaded()
        return (
            f"{target.value}: {len(core.history)} 条历史, "
            f"{'处理中' if core.working else '空闲'}"
        )

    async def send_context(
        self,
        source: SessionKey,
        target: SessionKey,
        content: str,
        *,
        kind: str = "message",
        request_id: str | None = None,
    ) -> str:
        text = str(content or "").strip()
        if not text:
            return "上下文消息不能为空"
        normalized_kind = str(kind or "message").strip().casefold()
        if normalized_kind not in {"message", "request", "reply"}:
            return "kind 必须是 message、request 或 reply"
        if normalized_kind == "reply" and not request_id:
            return "reply 必须提供 request_id"
        if normalized_kind == "request" and not request_id:
            request_id = f"ctx_{uuid.uuid4().hex}"
        if normalized_kind == "request" and request_id:
            self._pending_requests[request_id] = source
            watchdog = asyncio.create_task(
                self._watch_request(request_id),
                name=f"jianer-context-request-{request_id}",
            )
            self._request_tasks.add(watchdog)
            watchdog.add_done_callback(self._request_tasks.discard)
        if normalized_kind == "reply" and request_id:
            self._pending_requests.pop(request_id, None)
        core = await self.get_main(target)
        await core.inject_notice(
            text,
            source=source.value,
            kind=normalized_kind,
        )
        suffix = f"，request_id={request_id}" if request_id else ""
        return f"已从 {source.value} 投递到 {target.value}{suffix}"

    async def _watch_request(self, request_id: str) -> None:
        await asyncio.sleep(self.system_request_timeout)
        source = self._pending_requests.pop(request_id, None)
        if source is None:
            return
        core = await self.get_main(source)
        await core.inject_notice(
            f"context request {request_id} timed out",
            source="system",
            kind="request_timeout",
        )

    @staticmethod
    def parse_target(value: str) -> SessionKey:
        parts = str(value or "").split(":", 3)
        if len(parts) != 4:
            raise ValueError(
                "target 必须是 protocol:self_id:scene_type:scene_id"
            )
        protocol, self_id, scene_type, scene_id = parts
        if scene_type not in {"group", "private"}:
            raise ValueError("target scene_type 必须是 group 或 private")
        return SessionKey(
            scene_type=scene_type,  # type: ignore[arg-type]
            scene_id=scene_id,
            protocol=protocol,
            self_id=self_id,
        )

    async def summarize(
        self,
        target: SessionKey,
        *,
        source: str = "manual",
    ) -> str:
        core = await self.get_main(target)
        await core.ensure_loaded()
        if not core.history:
            return "当前上下文没有可总结的内容"
        model = self.model_resolver(target)
        history_text = _history_text(core.history)
        await self.system.ensure_loaded()
        system_request = (
            f"总结上下文 {target.value}（来源：{source}）\n\n{history_text}"
        )
        self.system.history.append(
            {"role": "user", "content": system_request}
        )
        try:
            summary = await self.providers.chat(
                model,
                (
                    f"请总结以下 {source} 的 Agent 会话历史。"
                    "保留关键事件、人物、决定、未完成任务和需要跟进的信息。"
                    "不要输出 Markdown，只输出纯文本总结。\n\n"
                    + history_text
                ),
                system_prompt=SYSTEM_CONTEXT_PROMPT.replace(
                    "{output}", OUTPUT_RULE
                ).replace("{tools}", "无"),
            )
        except Exception as exc:
            if (
                self.system.history
                and self.system.history[-1].get("content") == system_request
            ):
                self.system.history.pop()
            if self.logger is not None:
                try:
                    self.logger.warning("Agent context summary failed: %s", exc)
                except Exception:
                    pass
            return "上下文总结失败，请稍后重试"
        self.system.history.append(
            {"role": "assistant", "content": summary}
        )
        await self.system.save()
        await core.replace_with_summary(summary)
        return "已更新当前上下文总结"

    async def switch_profile(self, name: str) -> str:
        self.profiles.switch(name)
        summaries: list[str] = []
        for core in self.loaded():
            if core is self.system:
                continue
            await self.summarize(
                core.key,
                source=f"切换到人设 {name} 前",
            )
            summaries.append(core.key.value)
        return (
            f"已切换全局人设「{name}」"
            + (
                f"，并重写 {len(summaries)} 个 Main Context"
                if summaries
                else ""
            )
        )

    async def heartbeat_tick(self) -> None:
        for core in tuple(self._contexts.values()):
            if core.working:
                continue
            await core.ensure_loaded()
            if core.should_auto_summarize():
                await self.summarize(core.key, source="heartbeat")

    async def close(self) -> None:
        task, self._cleanup_task = self._cleanup_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for core in self.loaded():
            if core.working:
                core.request_stop()
            await core.save()
        self._contexts.clear()
        for task in tuple(self._request_tasks):
            task.cancel()
        if self._request_tasks:
            await asyncio.gather(
                *self._request_tasks,
                return_exceptions=True,
            )
        self._request_tasks.clear()
        self._pending_requests.clear()

    def _create(self, key: SessionKey, role: AgentRole) -> AgentCore:
        if role is AgentRole.SYSTEM:
            prompt_builder = self._system_prompt
        else:
            prompt_builder = self._main_prompt
        core = AgentCore(
            key,
            role=role,
            providers=self.providers,
            tools=self.tools,
            store=self.store,
            model_resolver=self.model_resolver,
            prompt_builder=prompt_builder,
            allowed_tool_names=self.allowed_tool_names,
            total_timeout_seconds=self.total_timeout_seconds,
            max_history_chars=self.max_history_chars,
            logger=self.logger,
        )
        if role is AgentRole.MAIN:
            self._contexts[key] = core
        return core

    def _main_prompt(self, specs: tuple[ToolSpec, ...]) -> str:
        profile = self.profiles.current()
        tools = _tool_catalog(specs)
        profile_prompt = (
            profile.prompt.replace("{self.bot_name}", self.bot_name)
            .replace("{self.bot_name_en}", self.bot_name_en)
            .replace("{self.event_user}", "对方")
            .replace("{self.event_user_id}", "当前用户")
            .replace("{agent_tools}", ", ".join(spec.name for spec in specs) or "无")
            .replace("{agent_tools_info}", tools)
        )
        return build_main_prompt(
            profile_prompt=profile_prompt,
            inject_master=profile.inject_master,
            owner_ids=self.owner_ids,
            tools=tools,
        )

    @staticmethod
    def _system_prompt(specs: tuple[ToolSpec, ...]) -> str:
        return SYSTEM_CONTEXT_PROMPT.replace(
            "{output}", OUTPUT_RULE
        ).replace("{tools}", _tool_catalog(specs))

    def _ensure_cleanup(self) -> None:
        if not self._contexts:
            return
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(1800)
            now = asyncio.get_running_loop().time()
            removable = [
                key
                for key, core in self._contexts.items()
                if not core.working
                and now - core.last_used >= self.idle_seconds
            ]
            for key in removable:
                core = self._contexts.pop(key, None)
                if core is not None:
                    await core.save()


def _tool_catalog(specs: tuple[ToolSpec, ...]) -> str:
    if not specs:
        return "无"
    lines = [
        "完整参数由本轮原生工具声明提供。",
    ]
    for spec in specs:
        properties = spec.input_schema.get("properties")
        names = (
            tuple(str(item) for item in properties)
            if isinstance(properties, Mapping)
            else ()
        )
        lines.append(f"- {spec.name}({', '.join(names)})")
    return "\n".join(lines)


def _history_text(history: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in history:
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("role") or "")
        content = str(item.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content[:4000]}")
    return "\n".join(lines)
