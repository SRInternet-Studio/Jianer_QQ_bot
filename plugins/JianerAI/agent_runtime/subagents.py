"""Persistent HypeR-style Sub-Agents."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from jianer.adapters import ConversationKey, ConversationKind

from plugins.JianerAI.agent_runtime.core import AgentCore
from plugins.JianerAI.agent_runtime.models import AgentRole, SessionKey
from plugins.JianerAI.agent_runtime.prompts import SUBAGENT_RULE
from plugins.JianerAI.agent_runtime.sessions import AgentSessionManager
from plugins.JianerAI.agent_runtime.store import ContextStore
from plugins.JianerAI.providers import ProviderRegistry
from plugins.JianerAI.tools.contracts import (
    ToolContext,
    ToolExecutionError,
    ToolRisk,
    ToolSpec,
)
from plugins.JianerAI.tools.registry import ToolRegistry


@dataclass(slots=True)
class SubAgent:
    sub_id: int
    name: str
    prompt: str
    owner: SessionKey
    key: SessionKey
    core: AgentCore
    parent_context: ToolContext | None
    status: str = "idle"
    created_at: int = 0


class SubAgentManager:
    MAX_SUBAGENTS = 3

    def __init__(
        self,
        *,
        root: str | Path,
        providers: ProviderRegistry,
        tools: ToolRegistry,
        model_resolver: Callable[[SessionKey], str],
        logger: Any | None = None,
    ) -> None:
        self.root = Path(root)
        self.providers = providers
        self.tools = tools
        self.model_resolver = model_resolver
        self.logger = logger
        self._agents: dict[int, SubAgent] = {}
        self._next_id = 1
        self._reports: dict[int, list[dict[str, Any]]] = {}
        self._report_index: dict[str, dict[str, Any]] = {}
        self._report_seq = 0
        self._tasks: set[asyncio.Task[Any]] = set()
        self._discover()

    async def create(
        self,
        *,
        name: str,
        prompt: str,
        parent: ToolContext,
        owner: SessionKey,
    ) -> str:
        if len(self._agents) >= self.MAX_SUBAGENTS:
            return f"Sub-Agent 数量已达上限（{self.MAX_SUBAGENTS}）。"
        normalized_name = str(name or "").strip()
        normalized_prompt = str(prompt or "").strip()
        if not normalized_name or not normalized_prompt:
            return "Sub-Agent 名称和 prompt 不能为空。"
        sub_id = self._next_id
        self._next_id += 1
        key = SessionKey(
            scene_type=owner.scene_type,
            scene_id=f"{owner.scene_id}#sub-{sub_id}",
            protocol=owner.protocol,
            self_id=owner.self_id,
        )
        store_root = self.root / f"sub-{sub_id}"
        core = self._build_core(
            sub_id,
            normalized_prompt,
            key,
            store_root,
        )
        agent = SubAgent(
            sub_id=sub_id,
            name=normalized_name,
            prompt=normalized_prompt,
            owner=owner,
            key=key,
            core=core,
            parent_context=parent,
            created_at=int(time.time()),
        )
        self._agents[sub_id] = agent
        store_root.mkdir(parents=True, exist_ok=True)
        (store_root / "metadata.json").write_text(
            json.dumps(
                {
                    "sub_id": sub_id,
                    "name": normalized_name,
                    "prompt": normalized_prompt,
                    "owner": owner.value,
                    "created_at": agent.created_at,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return f"Sub-Agent #{sub_id}「{normalized_name}」已创建。"

    async def destroy(self, sub_id: int) -> str:
        agent = self._agents.pop(int(sub_id), None)
        if agent is None:
            return f"Sub-Agent #{sub_id} 不存在。"
        if agent.core.working:
            agent.core.request_stop()
        await agent.core.save()
        shutil.rmtree(
            self.root / f"sub-{int(sub_id)}",
            ignore_errors=True,
        )
        return f"Sub-Agent #{sub_id}「{agent.name}」已销毁。"

    def list(self) -> str:
        if not self._agents:
            return f"暂无 Sub-Agent（0/{self.MAX_SUBAGENTS}）。"
        lines = [f"Sub-Agent 列表（{len(self._agents)}/{self.MAX_SUBAGENTS}）："]
        for agent in self._agents.values():
            lines.append(
                f"#{agent.sub_id}「{agent.name}」[{agent.status}] "
                f"owner={agent.owner.value}"
            )
        return "\n".join(lines)

    async def status(self, sub_id: int) -> str:
        agent = self._agents.get(int(sub_id))
        if agent is None:
            return f"Sub-Agent #{sub_id} 不存在。"
        await agent.core.ensure_loaded()
        return (
            f"Sub-Agent #{sub_id}「{agent.name}」\n"
            f"状态：{agent.status}\n"
            f"历史：{len(agent.core.history)} 条\n"
            f"创建时间：{agent.created_at}"
        )

    async def feed(
        self,
        sub_id: int,
        content: str,
        *,
        parent_context: ToolContext | None = None,
    ) -> str:
        agent = self._agents.get(int(sub_id))
        if agent is None:
            return f"Sub-Agent #{sub_id} 不存在。"
        text = str(content or "").strip()
        if not text:
            return "投喂内容不能为空。"
        if parent_context is not None:
            agent.parent_context = parent_context
        if agent.parent_context is None:
            return "Sub-Agent 缺少可用的父上下文，请从有效会话中重新投喂。"
        task = asyncio.create_task(self._feed(agent, text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return f"已向 Sub-Agent #{sub_id} 投喂消息。"

    async def _feed(self, agent: SubAgent, text: str) -> None:
        agent.status = "running"
        try:
            context = dataclasses.replace(
                agent.parent_context,
                conversation=ConversationKey(
                    protocol=agent.key.protocol,
                    self_id=agent.key.self_id,
                    kind=(
                        ConversationKind.GROUP
                        if agent.key.scene_type == "group"
                        else ConversationKind.PRIVATE
                    ),
                    conversation_id=agent.key.scene_id,
                    preset="subagent",
                ),
                role="sub",
                agent=agent.core,
                memory=(
                    agent.parent_context.memory_snapshot
                    or agent.parent_context.memory
                ),
                message_sink=None,
                release_requested=False,
            )
            result = await agent.core.event_handler(text, context=context)
            if result.text.strip() and result.text.strip() != "{}":
                self.report(agent, result.text.strip())
        finally:
            agent.status = "idle"

    def report(
        self,
        agent: SubAgent,
        content: str,
        *,
        need_response: bool = False,
    ) -> str:
        self._report_seq += 1
        report_id = f"subreport-{self._report_seq:04d}"
        record = {
            "report_id": report_id,
            "sub_id": agent.sub_id,
            "name": agent.name,
            "content": str(content),
            "need_response": bool(need_response),
            "created_at": int(time.time()),
            "owner": agent.owner.value,
        }
        self._reports.setdefault(agent.owner.value, []).append(record)
        self._report_index[report_id] = record
        return report_id

    async def reply(self, report_id: str, content: str) -> str:
        text = str(content or "").strip()
        report = self._report_index.pop(str(report_id), None)
        if report is None:
            return "找不到待回复的 Sub-Agent 报告。"
        agent = self._agents.get(int(report.get("sub_id") or 0))
        if agent is None:
            return "对应 Sub-Agent 已不存在。"
        report["response"] = text
        await agent.core.inject_notice(
            text,
            source="main",
            kind="subagent_reply",
        )
        return "已回复 Sub-Agent。"

    def reports_for(self, key: SessionKey) -> list[dict[str, Any]]:
        return list(self._reports.get(key.value, ()))

    def take_reports(self, key: SessionKey) -> list[dict[str, Any]]:
        return list(self._reports.pop(key.value, ()))

    async def close(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        for agent in self._agents.values():
            await agent.core.save()
        self._agents.clear()
        self._reports.clear()
        self._report_index.clear()

    def agent_for(self, key: SessionKey) -> SubAgent | None:
        for agent in self._agents.values():
            if agent.key == key:
                return agent
        return None

    def _discover(self) -> None:
        if not self.root.is_dir():
            return
        for directory in sorted(self.root.glob("sub-*")):
            metadata = directory / "metadata.json"
            try:
                raw = json.loads(metadata.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            try:
                sub_id = int(raw["sub_id"])
                profile = str(raw["prompt"])
                name = str(raw["name"])
                owner = AgentSessionManager.parse_target(str(raw["owner"]))
            except Exception:
                continue
            key = SessionKey(
                scene_type=owner.scene_type,
                scene_id=f"{owner.scene_id}#sub-{sub_id}",
                protocol=owner.protocol,
                self_id=owner.self_id,
            )
            agent = SubAgent(
                sub_id=sub_id,
                name=name,
                prompt=profile,
                owner=owner,
                key=key,
                core=self._build_core(
                    sub_id,
                    profile,
                    key,
                    directory,
                ),
                parent_context=None,
                created_at=int(raw.get("created_at", 0) or 0),
            )
            self._agents[sub_id] = agent
            self._next_id = max(self._next_id, sub_id + 1)

    def _build_core(
        self,
        sub_id: int,
        prompt: str,
        key: SessionKey,
        store_root: Path,
    ) -> AgentCore:
        del sub_id
        return AgentCore(
            key,
            role=AgentRole.SUB,
            providers=self.providers,
            tools=self.tools,
            store=ContextStore(store_root),
            model_resolver=self.model_resolver,
            prompt_builder=_SubPromptBuilder(prompt),
            total_timeout_seconds=600.0,
            max_history_chars=30000,
            logger=self.logger,
        )


class _SubPromptBuilder:
    def __init__(self, prompt: str) -> None:
        self.prompt = prompt

    def __call__(self, specs: tuple[ToolSpec, ...]) -> str:
        tools = "\n".join(f"- {spec.name}" for spec in specs) or "无"
        return (
            self.prompt
            + "\n\n"
            + SUBAGENT_RULE
            + "\n\n# 可用工具\n\n"
            + tools
        )


def subagent_tools(manager: SubAgentManager) -> tuple[ToolSpec, ...]:
    async def create(context: ToolContext, arguments: dict[str, Any]) -> str:
        return await manager.create(
            name=str(arguments.get("name") or ""),
            prompt=str(arguments.get("prompt") or ""),
            parent=context,
            owner=_source(context),
        )

    async def destroy(context: ToolContext, arguments: dict[str, Any]) -> str:
        del context
        return await manager.destroy(int(arguments.get("sub_id", 0)))

    async def list_agents(context: ToolContext, arguments: dict[str, Any]) -> str:
        del context, arguments
        return manager.list()

    async def status(context: ToolContext, arguments: dict[str, Any]) -> str:
        del context
        return await manager.status(int(arguments.get("sub_id", 0)))

    async def feed(context: ToolContext, arguments: dict[str, Any]) -> str:
        return await manager.feed(
            int(arguments.get("sub_id", 0)),
            str(arguments.get("content") or ""),
            parent_context=context,
        )

    async def report(context: ToolContext, arguments: dict[str, Any]) -> str:
        agent = manager.agent_for(_source(context))
        if agent is None:
            raise ToolExecutionError(
                "subagent_not_found",
                "当前上下文不是已注册的 Sub-Agent。",
            )
        report_id = manager.report(
            agent,
            str(arguments.get("content") or ""),
            need_response=bool(arguments.get("need_response", False)),
        )
        return f"报告已提交：{report_id}"

    async def reply(context: ToolContext, arguments: dict[str, Any]) -> str:
        del context
        return await manager.reply(
            str(arguments.get("report_id") or ""),
            str(arguments.get("content") or ""),
        )

    asset = {
        "type": "object",
        "properties": {"sub_id": {"type": "integer", "minimum": 1}},
        "required": ["sub_id"],
        "additionalProperties": False,
    }
    return (
        ToolSpec(
            name="sub_create",
            description="创建一个持久 Sub-Agent。",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "prompt": {"type": "string", "minLength": 1},
                },
                "required": ["name", "prompt"],
                "additionalProperties": False,
            },
            handler=create,
            risk=ToolRisk.MUTATING,
            group="subagent",
            sub_visible=False,
        ),
        ToolSpec(
            name="sub_destroy",
            description="销毁指定 Sub-Agent。",
            input_schema=asset,
            handler=destroy,
            risk=ToolRisk.MUTATING,
            group="subagent",
            sub_visible=False,
        ),
        ToolSpec(
            name="sub_list",
            description="列出 Sub-Agent。",
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            handler=list_agents,
            group="subagent",
            sub_visible=False,
        ),
        ToolSpec(
            name="sub_status",
            description="查看 Sub-Agent 状态。",
            input_schema=asset,
            handler=status,
            group="subagent",
            sub_visible=False,
        ),
        ToolSpec(
            name="sub_feed",
            description="向持久 Sub-Agent 投喂任务。",
            input_schema={
                "type": "object",
                "properties": {
                    "sub_id": {"type": "integer", "minimum": 1},
                    "content": {"type": "string", "minLength": 1},
                },
                "required": ["sub_id", "content"],
                "additionalProperties": False,
            },
            handler=feed,
            risk=ToolRisk.MUTATING,
            group="subagent",
            sub_visible=False,
        ),
        ToolSpec(
            name="sub_report",
            description="Sub-Agent 向主 Agent 汇报任务结果。",
            input_schema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "minLength": 1},
                    "need_response": {"type": "boolean", "default": False},
                },
                "required": ["content"],
                "additionalProperties": False,
            },
            handler=report,
            group="subagent",
            main_visible=False,
            sub_visible=True,
        ),
        ToolSpec(
            name="sub_reply",
            description="回复需要决策或额外信息的 Sub-Agent 报告。",
            input_schema={
                "type": "object",
                "properties": {
                    "report_id": {"type": "string", "minLength": 1},
                    "content": {"type": "string", "minLength": 1},
                },
                "required": ["report_id", "content"],
                "additionalProperties": False,
            },
            handler=reply,
            group="subagent",
            sub_visible=False,
        ),
    )


def _source(context: ToolContext) -> SessionKey:
    kind = str(getattr(context.conversation.kind, "value", "private"))
    return SessionKey(
        scene_type="group" if kind == "group" else "private",
        scene_id=str(context.conversation.conversation_id),
        protocol=str(context.conversation.protocol),
        self_id=str(context.conversation.self_id),
    )
