"""Event facade for the HypeR-style JianerAI Agent runtime."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from plugins.JianerAI.agent_runtime.collector import Collector
from plugins.JianerAI.agent_runtime.core import AgentCoreError, AgentStopped
from plugins.JianerAI.agent_runtime.models import (
    AgentEvent,
    PermissionGroup,
    SessionKey,
    permission_level,
)
from plugins.JianerAI.agent_runtime.sessions import AgentSessionManager
from plugins.JianerAI.tools.contracts import ToolContext

ContextBuilder = Callable[
    [SessionKey, Any, Any, Sequence[dict[str, Any]], str],
    Awaitable[ToolContext],
]
AfterRun = Callable[
    [SessionKey, Any, Any, Sequence[dict[str, Any]], str, Exception | None],
    Awaitable[None],
]
Reply = Callable[[Any, Any, str], Awaitable[None]]


class AgentFacade:
    """Route host events into collectors and persistent Agent contexts."""

    def __init__(
        self,
        *,
        sessions: AgentSessionManager,
        profiles: Any,
        reminder: str,
        owner_ids: Sequence[str],
        config_path: str | Path,
        build_context: ContextBuilder,
        after_run: AfterRun,
        reply: Reply,
        model_manager: Any | None = None,
        tools: Any | None = None,
        collector_max_buffer: int = 80,
        collector_keep_after_compress: int = 60,
        collector_max_summary_chars: int = 8000,
        stop_permission: str = "any_admin",
        stop_open_after: float = 10.0,
        max_concurrency: int = 0,
        take_notices: Callable[[SessionKey], Sequence[Any]] | None = None,
        logger: Any | None = None,
    ) -> None:
        self.sessions = sessions
        self.profiles = profiles
        self.reminder = str(reminder or "~")
        self.owner_ids = frozenset(str(item) for item in owner_ids)
        self.config_path = Path(config_path)
        self.build_context = build_context
        self.after_run = after_run
        self.reply = reply
        self.model_manager = model_manager
        self.tools = tools
        self.collector_max_buffer = max(2, int(collector_max_buffer))
        self.collector_keep_after_compress = max(
            1, int(collector_keep_after_compress)
        )
        self.collector_max_summary_chars = max(
            512, int(collector_max_summary_chars)
        )
        self.stop_permission = str(stop_permission or "any_admin")
        self.stop_open_after = max(0.1, float(stop_open_after))
        self._api_semaphore = (
            asyncio.Semaphore(max(1, int(max_concurrency)))
            if int(max_concurrency) > 0
            else None
        )
        self.take_notices = take_notices
        self.logger = logger
        self._collectors: dict[SessionKey, Collector] = {}
        self._latest: dict[SessionKey, tuple[Any, Any, PermissionGroup, str]] = {}
        self._seen: dict[str, float] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._heartbeat_task: asyncio.Task[Any] | None = None
        self._whitelist = self._load_whitelist()

    def start(self, enabled: bool | None = None) -> None:
        others = self._read_config()
        heartbeat = (
            others.get("agent_heartbeat", False)
            if enabled is None
            else enabled
        )
        if not _as_bool(heartbeat):
            return
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(),
                name="jianer-agent-heartbeat",
            )

    # ------------------------------------------------------------------ #
    # ingress
    # ------------------------------------------------------------------ #
    async def observe(
        self,
        event: Any,
        actions: Any,
        *,
        text: str = "",
    ) -> bool:
        if not self._is_message_event(event):
            return False
        raw_text = str(text or getattr(event, "msg_str", "") or "").strip()
        if self._is_command(raw_text):
            return False
        key = self.session_key(event)
        token = self._event_token(event, key)
        if token in self._seen:
            return True
        self._seen[token] = time.monotonic()
        self._prune_seen()
        principal = str(getattr(event, "user_id", "") or "")
        perm = self.permission_of(event, actions)
        self._latest[key] = (event, actions, perm, principal)
        agent_event = self._agent_event(event, raw_text)

        if key.scene_type == "group":
            group_white = self.group_whitelist(key)
            if not group_white and not bool(getattr(event, "is_mentioned", False)):
                return False
            collector = self._collector(key)
            if bool(getattr(event, "is_mentioned", False)):
                await self._immediate(
                    key,
                    collector,
                    agent_event,
                    perm,
                    principal,
                )
                return True
            if principal not in group_white:
                await collector.append_passive(agent_event)
                return True
            await collector.append(agent_event)
            await collector.start(principal, perm, self._self_id(event))
            return True

        collector = self._collector(key)
        if principal in self.owner_ids:
            await self._immediate(
                key,
                collector,
                agent_event,
                perm,
                principal,
            )
            return True
        await collector.append(agent_event)
        await collector.start(principal, perm, self._self_id(event))
        return True

    async def fallback(
        self,
        event: Any,
        actions: Any,
        *,
        text: str = "",
    ) -> bool:
        if not self._is_message_event(event):
            return False
        raw_text = str(text or getattr(event, "msg_str", "") or "").strip()
        if self._is_command(raw_text):
            key = self.session_key(event)
            if await self.handle_command(event, actions, key, raw_text):
                return True
        key = self.session_key(event)
        if self._event_token(event, key) in self._seen:
            return True
        return await self.observe(event, actions, text=raw_text)

    def mark_seen(self, event: Any) -> None:
        if not self._is_message_event(event):
            return
        key = self.session_key(event)
        self._seen[self._event_token(event, key)] = time.monotonic()
        self._prune_seen()

    async def on_notice(self, event: Any, actions: Any) -> bool:
        name = type(event).__name__
        key = self.session_key(event)
        if key.scene_type == "group" and not self.group_whitelist(key):
            return False
        self._latest[key] = (
            event,
            actions,
            self.permission_of(event, actions),
            str(getattr(event, "user_id", "") or ""),
        )
        agent_event = self._agent_event(event, f"[{name}]")
        collector = self._collector(key)
        if self._is_immediate_notice(event):
            await self._immediate(
                key,
                collector,
                agent_event,
                self.permission_of(event, actions),
                str(getattr(event, "user_id", "") or ""),
            )
        else:
            await collector.append_passive(agent_event)
        return True

    # ------------------------------------------------------------------ #
    # execution
    # ------------------------------------------------------------------ #
    async def _process(
        self,
        key: SessionKey,
        batch: list[dict[str, Any]],
        perm: PermissionGroup,
        principal: str | None,
        self_id: str | None,
    ) -> None:
        if not batch:
            return
        latest = self._latest.get(key)
        if latest is None:
            return
        event, actions, current_perm, current_principal = latest
        actual_principal = str(principal or current_principal or "")
        del self_id
        error: Exception | None = None
        try:
            manager = (
                self._api_semaphore
                if self._api_semaphore is not None
                else contextlib.nullcontext()
            )
            async with manager:
                core = await self.sessions.get_main(key)
                prepare_main = getattr(self.sessions, "prepare_main", None)
                if callable(prepare_main):
                    await prepare_main(core)
                context = await self.build_context(
                    key,
                    event,
                    actions,
                    tuple(batch),
                    actual_principal,
                )
                context = dataclasses.replace(context, agent=core)
                payload: dict[str, Any] = {
                    "event": {
                        "type": "message_batch",
                        "scene_type": key.scene_type,
                        "scene_id": key.scene_id,
                        "protocol": key.protocol,
                        "self_id": key.self_id,
                        "principal_id": actual_principal,
                        "messages": batch,
                    },
                    "system_message": "如果要回复消息，唯一正确方法是调用工具",
                }
                if self.take_notices is not None:
                    notices = tuple(self.take_notices(key))
                    if notices:
                        payload["notices"] = notices
                snapshot = getattr(context, "memory_snapshot", None)
                render = getattr(snapshot, "as_prompt", None)
                if callable(render):
                    memory = str(render() or "").strip()
                    if memory:
                        payload["retrieved_memory"] = memory
                await core.event_handler(
                    json.dumps(payload, ensure_ascii=False),
                    context=context,
                )
                if core.should_auto_summarize():
                    await self.sessions.summarize(key, source="自动压缩")
        except AgentStopped as exc:
            error = exc
        except AgentCoreError as exc:
            error = exc
        except asyncio.CancelledError:
            error = AgentStopped()
        except Exception as exc:
            error = exc
            if self.logger is not None:
                try:
                    self.logger.exception("JianerAI Agent run failed")
                except Exception:
                    pass
        finally:
            await self.after_run(
                key,
                event,
                actions,
                tuple(batch),
                actual_principal,
                error,
            )

    async def _immediate(
        self,
        key: SessionKey,
        collector: Collector,
        event: AgentEvent,
        perm: PermissionGroup,
        principal: str,
    ) -> None:
        batch = [*collector.buffer, event.to_dict()]
        collector.buffer.clear()
        await collector.stop()
        task = asyncio.create_task(
            self._process(
                key,
                batch,
                perm,
                principal,
                event.self_id,
            ),
            name=f"jianer-agent-{key.directory_name}",
        )
        self._track(task)

    def _track(self, task: asyncio.Task[Any]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ #
    # commands
    # ------------------------------------------------------------------ #
    async def handle_command(
        self,
        event: Any,
        actions: Any,
        key: SessionKey,
        text: str,
    ) -> bool:
        normalized = _normalize_command(text, self.reminder)
        if normalized is None:
            return False
        parts = normalized.split()
        sub = _normalize_subcommand(parts[1] if len(parts) > 1 else "")
        if not sub:
            await self.reply(event, actions, self.command_help(key))
            return True
        uid = str(getattr(event, "user_id", "") or "")
        perm = self.permission_of(event, actions)
        if sub.startswith("func."):
            _, action, *tail = sub.split(".")
            parts = ["agent", "func", action, *tail]
            sub = "func"
        if sub.startswith("api."):
            action = sub.split(".", 1)[1]
            if action in {"reload", "rl"}:
                parts = ["agent", "api", "reload"]
            elif action in {"use", "show", "sh"} and len(parts) > 2:
                parts = ["agent", "api", parts[2], *parts[3:]]
            else:
                parts = ["agent", "api", action, *parts[2:]]
            sub = "api"
        if sub in {"on", "off"}:
            if key.scene_type != "group":
                await self.reply(
                    event,
                    actions,
                    "私聊自动处理始终开启，无需白名单。",
                )
                return True
            target = self._command_target(parts, event, uid)
            if target is None:
                await self.reply(event, actions, f"用法：{self.reminder}agent.{sub} [QQ号或@用户]")
                return True
            if target != uid and permission_level(perm) < permission_level("any_admin"):
                await self.reply(
                    event,
                    actions,
                    "只有群管理员或 Bot 主人可以修改其他用户的 Agent 白名单。",
                )
                return True
            self.set_whitelisted(key, target, sub == "on")
            action = "加入" if sub == "on" else "移出"
            await self.reply(
                event,
                actions,
                f"已将用户 {target} {action}当前群的 Agent 白名单。",
            )
            return True
        if sub == "status":
            await self.reply(event, actions, self.command_help(key))
            return True
        if sub == "stop":
            if key.scene_type == "main":
                return False
            core = await self.sessions.get_main(key)
            elapsed = core.processing_elapsed()
            if elapsed is None:
                await self.reply(event, actions, "当前没有正在进行的处理。")
                return True
            allowed = (
                permission_level(perm)
                >= permission_level(self.stop_permission)
                or elapsed >= self.stop_open_after
            )
            if not allowed:
                await self.reply(
                    event,
                    actions,
                    f"处理超过 {self.stop_open_after:g} 秒后所有用户都可以中止。",
                )
                return True
            core.request_stop()
            await self.reply(
                event,
                actions,
                f"已请求中止当前处理（已进行 {elapsed:.1f} 秒）。",
            )
            return True
        if sub in {"model", "api"}:
            if permission_level(perm) < permission_level("bot_owner"):
                await self.reply(event, actions, "仅 Bot 主人可以切换模型。")
                return True
            if self.model_manager is None:
                await self.reply(event, actions, "模型管理器不可用。")
                return True
            if len(parts) <= 2:
                await self.reply(
                    event,
                    actions,
                    json.dumps(
                        self.model_manager.redacted_status(),
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
                return True
            if parts[2].casefold() in {"reload", "rl"}:
                self.model_manager.reload()
                await self.reply(event, actions, "已重新加载 aiconfig 模型。")
                return True
            try:
                selected = self.model_manager.activate(parts[2])
                self._persist_agent_setting("agent_api_profile", selected)
                text = f"已切换全局 Agent 模型：{selected}"
            except Exception as exc:
                text = str(exc)
            await self.reply(event, actions, text)
            return True
        if sub in {"func", "function"}:
            if permission_level(perm) < permission_level("bot_owner"):
                await self.reply(event, actions, "仅 Bot 主人可以管理工具。")
                return True
            if self.tools is None:
                await self.reply(event, actions, "工具注册表不可用。")
                return True
            if len(parts) <= 2:
                lines = [
                    (
                        f"{spec.name} [{spec.group}] "
                        f"{'disabled' if self.tools.is_disabled(spec.name) else 'enabled'}"
                    )
                    for spec in self.tools.specs
                ]
                await self.reply(event, actions, "\n".join(lines) or "无工具。")
                return True
            action = sub
            if action == "function":
                action = "func"
            if len(parts) > 2 and parts[2] in {"en", "enable"}:
                name = parts[3] if len(parts) > 3 else ""
                ok = self.tools.enable(name)
                await self.reply(
                    event,
                    actions,
                    f"已启用 {name}。" if ok else f"工具 {name} 不存在或未禁用。",
                )
                return True
            if len(parts) > 2 and parts[2] in {"dis", "disable"}:
                name = parts[3] if len(parts) > 3 else ""
                seconds = _duration_seconds(parts[4]) if len(parts) > 4 else None
                ok = self.tools.disable(name, seconds=seconds)
                await self.reply(
                    event,
                    actions,
                    f"已禁用 {name}。" if ok else f"工具 {name} 不存在。",
                )
                return True
            await self.reply(event, actions, "用法：agent.func [en|dis] <名称> [时长]")
            return True
        if sub == "profile":
            if len(parts) <= 2:
                await self.reply(event, actions, self.profile_status())
                return True
            if permission_level(perm) < permission_level("bot_owner"):
                await self.reply(event, actions, "仅 Bot 主人可以切换全局人设。")
                return True
            try:
                result = await self.sessions.switch_profile(parts[2])
            except Exception as exc:
                result = str(exc)
            await self.reply(event, actions, result)
            return True
        if sub in {"profile.add", "profile.remove", "profile.master"}:
            if permission_level(perm) < permission_level("bot_owner"):
                await self.reply(event, actions, "仅 Bot 主人可以管理全局人设。")
                return True
            if not self._save_profiles_enabled():
                await self.reply(event, actions, "人设文件不可写。")
                return True
            if sub == "profile.add":
                if len(parts) < 4:
                    await self.reply(
                        event,
                        actions,
                        f"用法：{self.reminder}agent.profile.add <名称> <人设原文>",
                    )
                    return True
                name = parts[2]
                prompt = " ".join(parts[3:])
                profile = self.profiles.upsert(
                    name,
                    prompt,
                    display_name=name,
                )
                await self.reply(
                    event,
                    actions,
                    f"已保存全局人设「{profile.display_name or name}」。",
                )
                return True
            if len(parts) < 3:
                await self.reply(event, actions, "缺少人设名称。")
                return True
            name = parts[2]
            if sub == "profile.remove":
                try:
                    removed = self.profiles.remove(name)
                    text = f"已删除全局人设「{name}」。" if removed else "人设不存在。"
                except Exception as exc:
                    text = str(exc)
                await self.reply(event, actions, text)
                return True
            enabled: bool | None = None
            if len(parts) > 3:
                value = parts[3].casefold()
                enabled = value in {"on", "1", "true", "开启"}
            try:
                key = self.profiles.resolve_key(name)
                profile = self.profiles.get(key)
            except Exception:
                await self.reply(event, actions, "人设不存在。")
                return True
            label = self.profiles.label(key)
            if enabled is None:
                await self.reply(
                    event,
                    actions,
                    f"「{label}」inject_master="
                    f"{'on' if profile.inject_master else 'off'}",
                )
                return True
            self.profiles.upsert(
                key,
                profile.prompt,
                inject_master=enabled,
                display_name=profile.display_name,
            )
            await self.reply(
                event,
                actions,
                f"已更新「{label}」的 inject_master。",
            )
            return True
        if sub in {"context.clear", "context.summary"}:
            action = sub.split(".", 1)[1]
            sub = "context"
            parts = ["agent", "context", action]
        if sub == "context":
            required = (
                "any_admin"
                if key.scene_type == "group"
                else "bot_owner"
            )
            if permission_level(perm) < permission_level(required):
                await self.reply(
                    event,
                    actions,
                    "仅群管理员或 Bot 主人可以管理上下文。"
                    if key.scene_type == "group"
                    else "仅 Bot 主人可以管理私聊上下文。",
                )
                return True
            if len(parts) > 2 and parts[2] == "clear":
                core = await self.sessions.get_main(key)
                await core.clear()
                await self.reply(event, actions, "已清空当前 Main Context。")
                return True
            if len(parts) > 2 and parts[2] in {"summary", "sum"}:
                await self.reply(
                    event,
                    actions,
                    await self.sessions.summarize(key, source="手动请求"),
                )
                return True
            await self.reply(
                event,
                actions,
                await self.sessions.context_status(key),
            )
            return True
        await self.reply(event, actions, f"未知 Agent 子命令：{sub}")
        return True

    def command_help(self, key: SessionKey) -> str:
        active = bool(self.group_whitelist(key)) if key.scene_type == "group" else True
        model = getattr(self.model_manager, "active", "") or "未配置"
        return (
            f"Agent 状态：{'已启用' if active else '未启用'}\n"
            f"白名单用户：{', '.join(sorted(self.group_whitelist(key))) or '无'}\n"
            f"当前人设：{self._active_profile_label()}\n"
            f"当前模型：{model}\n"
            f"命令：{self.reminder}agent.on/off/status/stop/profile/context\n"
            f"清空当前上下文：{self.reminder}清空上下文"
        )

    def profile_status(self) -> str:
        names = ", ".join(self._profile_labels())
        return (
            f"可用人设：{names}\n"
            f"当前人设：{self._active_profile_label()}"
        )

    # ------------------------------------------------------------------ #
    # whitelist and lifecycle
    # ------------------------------------------------------------------ #
    def group_whitelist(self, key: SessionKey) -> frozenset[str]:
        if key.scene_type != "group":
            return frozenset()
        return frozenset(self._whitelist.get(key.value, ()))

    def set_whitelisted(self, key: SessionKey, user_id: str, enabled: bool) -> None:
        if key.scene_type != "group":
            return
        values = set(self._whitelist.get(key.value, ()))
        normalized = str(user_id or "").strip()
        if not normalized:
            return
        if enabled:
            values.add(normalized)
        else:
            values.discard(normalized)
        if values:
            self._whitelist[key.value] = values
        else:
            self._whitelist.pop(key.value, None)
        self._save_whitelist()

    def migrate_legacy_whitelist(
        self,
        *,
        protocol: str,
        self_id: str,
    ) -> int:
        migrated = 0
        for legacy_key, users in tuple(self._whitelist.items()):
            if not str(legacy_key).isdigit():
                continue
            key = SessionKey(
                scene_type="group",
                scene_id=str(legacy_key),
                protocol=str(protocol or "unknown"),
                self_id=str(self_id or "unknown"),
            )
            self._whitelist[key.value] = set(users)
            self._whitelist.pop(legacy_key, None)
            migrated += 1
        if migrated:
            self._save_whitelist()
        return migrated

    async def close(self) -> None:
        heartbeat, self._heartbeat_task = self._heartbeat_task, None
        if heartbeat is not None and not heartbeat.done():
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
        for collector in self._collectors.values():
            await collector.stop()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.sessions.close()

    async def _heartbeat_loop(self) -> None:
        delay = 300.0
        while True:
            await asyncio.sleep(delay)
            try:
                await self.sessions.heartbeat_tick()
            except Exception:
                if self.logger is not None:
                    try:
                        self.logger.exception("JianerAI Agent heartbeat failed")
                    except Exception:
                        pass

    def permission_of(self, event: Any, actions: Any) -> PermissionGroup:
        uid = str(getattr(event, "user_id", "") or "")
        if uid in self.owner_ids:
            return "bot_owner"
        sender = getattr(event, "sender", None)
        role = str(
            sender.get("role", "")
            if isinstance(sender, Mapping)
            else getattr(sender, "role", "")
        ).casefold()
        if role in {"owner", "admin"}:
            return "any_admin"
        key = self.session_key(event)
        if key.scene_type == "group" and uid in self.group_whitelist(key):
            return "whitelist"
        return "member"

    @staticmethod
    def session_key(event: Any) -> SessionKey:
        group_id = getattr(event, "group_id", None)
        scene_type = "group" if group_id is not None else "private"
        scene_id = str(
            group_id
            if group_id is not None
            else getattr(event, "user_id", "") or "unknown"
        )
        return SessionKey(
            scene_type=scene_type,
            scene_id=scene_id,
            protocol=str(getattr(event, "protocol", "") or "unknown"),
            self_id=str(getattr(event, "self_id", "") or "unknown"),
        )

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _active_profile_label(self) -> str:
        method = getattr(self.profiles, "active_label", None)
        if callable(method):
            return str(method())
        return str(getattr(self.profiles, "active_name", "") or "")

    def _profile_labels(self) -> tuple[str, ...]:
        method = getattr(self.profiles, "labels", None)
        if callable(method):
            return tuple(str(value) for value in method().values())
        method = getattr(self.profiles, "list", None)
        if callable(method):
            return tuple(str(value) for value in method())
        return ()

    def _collector(self, key: SessionKey) -> Collector:
        collector = self._collectors.get(key)
        if collector is None:
            collector = Collector(
                max_buffer=self.collector_max_buffer,
                keep_after_compress=self.collector_keep_after_compress,
                max_summary_chars=self.collector_max_summary_chars,
                process=lambda batch, perm, principal, self_id: self._process(
                    key,
                    batch,
                    perm,
                    principal,
                    self_id,
                )
            )
            self._collectors[key] = collector
        return collector

    def _agent_event(self, event: Any, text: str) -> AgentEvent:
        key = self.session_key(event)
        fields = {
            name: _safe_value(getattr(event, name, None))
            for name in (
                "sub_type",
                "operator_id",
                "target_id",
                "duration",
                "message_id",
                "sender_id",
                "honor_type",
                "code",
                "count",
                "file",
            )
            if getattr(event, name, None) is not None
        }
        payload: dict[str, Any] = {
            "text": text or _notice_text(type(event).__name__, fields),
            "event_type": type(event).__name__,
            "message_id": str(getattr(event, "message_id", "") or ""),
            "sender": self._sender(event),
            "fields": fields,
        }
        return AgentEvent(
            type=type(event).__name__,
            scene_type=key.scene_type,  # type: ignore[arg-type]
            scene_id=key.scene_id,
            source="host",
            payload=payload,
            protocol=key.protocol,
            self_id=key.self_id,
            timestamp=int(time.time()),
        )

    def _event_token(self, event: Any, key: SessionKey) -> str:
        return (
            f"{key.value}:"
            f"{getattr(event, 'message_id', None) or id(event)}"
        )

    def _prune_seen(self) -> None:
        cutoff = time.monotonic() - 900
        self._seen = {
            token: seen_at
            for token, seen_at in self._seen.items()
            if seen_at >= cutoff
        }

    def _load_whitelist(self) -> dict[str, set[str]]:
        raw = self._read_config().get("agent_white")
        if not isinstance(raw, Mapping):
            return {}
        output: dict[str, set[str]] = {}
        for key, values in raw.items():
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                continue
            users = {str(item) for item in values if str(item).strip()}
            if users:
                output[str(key)] = users
        return output

    def _save_whitelist(self) -> None:
        config = self._read_root_config()
        others = config.setdefault("others", {})
        if not isinstance(others, dict):
            others = {}
            config["others"] = others
        others["agent_white"] = {
            key: sorted(values)
            for key, values in sorted(self._whitelist.items())
        }
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_name(
            f"{self.config_path.name}.tmp-{os.getpid()}"
        )
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.config_path)

    def _persist_active_profile(self, name: str) -> None:
        self._persist_agent_setting("agent_profile", str(name))

    def _persist_agent_setting(self, key: str, value: Any) -> None:
        config = self._read_root_config()
        others = config.setdefault("others", {})
        if not isinstance(others, dict):
            others = {}
            config["others"] = others
        others[str(key)] = value
        temporary = self.config_path.with_name(
            f"{self.config_path.name}.tmp-{os.getpid()}"
        )
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.config_path)

    def _save_profiles_enabled(self) -> bool:
        try:
            self.profiles.save()
            return True
        except Exception:
            return False

    def _read_config(self) -> dict[str, Any]:
        others = self._read_root_config().get("others")
        return dict(others) if isinstance(others, Mapping) else {}

    def _read_root_config(self) -> dict[str, Any]:
        try:
            with self.config_path.open("r", encoding="utf-8") as stream:
                value = json.load(stream)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(value, dict):
            return {}
        return value

    @staticmethod
    def _is_message_event(event: Any) -> bool:
        return hasattr(event, "message") and hasattr(event, "user_id")

    def _is_command(self, text: str) -> bool:
        value = str(text or "").strip()
        return bool(value and value.startswith(self.reminder))

    @staticmethod
    def _self_id(event: Any) -> str:
        return str(getattr(event, "self_id", "") or "unknown")

    @staticmethod
    def _is_immediate_notice(event: Any) -> bool:
        name = type(event).__name__
        if name == "NotifyEvent":
            return str(getattr(event, "target_id", "")) == str(
                getattr(event, "self_id", "")
            )
        if name == "GroupRecallEvent":
            return str(getattr(event, "user_id", "")) == str(
                getattr(event, "self_id", "")
            ) and str(getattr(event, "operator_id", "")) != str(
                getattr(event, "self_id", "")
            )
        return False

    @staticmethod
    def _sender(event: Any) -> dict[str, str]:
        sender = getattr(event, "sender", None)
        values = {}
        for name in ("nickname", "card", "role", "user_id"):
            value = (
                sender.get(name)
                if isinstance(sender, Mapping)
                else getattr(sender, name, None)
            )
            values[name] = str(value or "")
        return values

    @staticmethod
    def _command_target(
        parts: Sequence[str],
        event: Any,
        fallback: str,
    ) -> str | None:
        if len(parts) > 2:
            return str(parts[2]).lstrip("@") or None
        for segment in getattr(event, "message", ()) or ():
            qq = getattr(segment, "qq", None)
            if qq is not None and str(qq) != str(getattr(event, "self_id", "")):
                return str(qq)
        return fallback or None


def _normalize_command(text: str, reminder: str) -> str | None:
    value = str(text or "").strip()
    if not value.startswith(reminder):
        if value.casefold().startswith(("~agent", "~ag")):
            value = "~" + value[1:]
        else:
            return None
    body = value[len(reminder) :].strip()
    if not body:
        return None
    separated = body.replace(".", " ", 1)
    parts = separated.split()
    if not parts:
        return None
    head = parts[0].casefold()
    if head not in {"agent", "ag"}:
        return None
    if head == "ag":
        head = "agent"
    rest = body[len(parts[0]) :].lstrip(" .")
    return "agent" + (f" {rest}" if rest else "")


def _normalize_subcommand(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    aliases = {
        "pf": "profile",
        "ctx": "context",
        "clr": "clear",
        "sum": "summary",
        "st": "stop",
        "状态": "status",
        "开启": "on",
        "打开": "on",
        "关闭": "off",
        "工具": "func",
        "子代理": "sub_list",
        "pf.ad": "profile.add",
        "pf.rm": "profile.remove",
        "pf.ma": "profile.master",
        "ctx.clr": "context.clear",
        "ctx.sum": "context.summary",
        "func.en": "func.en",
        "func.enable": "func.en",
        "function.en": "func.en",
        "function.enable": "func.en",
        "func.dis": "func.dis",
        "func.disable": "func.dis",
        "function.dis": "func.dis",
        "function.disable": "func.dis",
    }
    return aliases.get(normalized, normalized)


def _max_permission(
    first: PermissionGroup,
    second: PermissionGroup,
) -> PermissionGroup:
    if permission_level(first) >= permission_level(second):
        return first
    return second


def _duration_seconds(value: str) -> float | None:
    text = str(value or "").strip().casefold()
    if not text:
        return None
    match = __import__("re").fullmatch(
        r"(\d+(?:\.\d+)?)\s*([smhd秒分时天]?)",
        text,
    )
    if match is None:
        return None
    amount = float(match.group(1))
    unit = match.group(2) or "m"
    factor = {
        "s": 1,
        "秒": 1,
        "m": 60,
        "分": 60,
        "h": 3600,
        "时": 3600,
        "d": 86400,
        "天": 86400,
    }.get(unit, 60)
    return amount * factor


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {
        "1",
        "true",
        "on",
        "yes",
        "开启",
    }


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _safe_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value[:20]]
    return str(value)[:500]


def _notice_text(name: str, fields: Mapping[str, Any]) -> str:
    if not fields:
        return f"[{name}]"
    rendered = ", ".join(
        f"{key}={value}"
        for key, value in fields.items()
    )
    return f"[{name}] {rendered}"
