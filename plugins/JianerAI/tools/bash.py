"""Human-reviewed Bash command execution for explicitly authorized bot admins."""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from plugins.JianerAI.tools.contracts import (
    ToolContext,
    ToolExecutionError,
    ToolRisk,
    ToolSpec,
)


_MAX_COMMAND_CHARS = 4096
_MAX_OUTPUT_CHARS = 12000


@dataclass(slots=True)
class _PendingApproval:
    request_id: str
    scope: tuple[str, str, str, str]
    requester_external_id: str
    command: str
    future: asyncio.Future[str]


class ShellApprovalManager:
    """Runs one command only after its originating speaker approves it."""

    def __init__(
        self,
        *,
        project_root: Path,
        reminder: str = "~",
        approval_timeout_seconds: float = 120.0,
        command_timeout_seconds: float = 60.0,
        process_factory: Any = asyncio.create_subprocess_shell,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.reminder = str(reminder or "~").strip() or "~"
        self.approval_timeout_seconds = max(
            5.0, min(float(approval_timeout_seconds), 600.0)
        )
        self.command_timeout_seconds = max(
            1.0, min(float(command_timeout_seconds), 300.0)
        )
        self._process_factory = process_factory
        self._pending: dict[str, _PendingApproval] = {}
        self._closed = False

    def tool(self) -> ToolSpec:
        return ToolSpec(
            name="bash",
            description=(
                "Request human approval to run one exact shell command from the "
                "current bot-admin speaker. The command is shown in this chat and "
                "is not executed until that speaker approves its request ID."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The exact shell command to show for approval.",
                        "minLength": 1,
                        "maxLength": _MAX_COMMAND_CHARS,
                    }
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            handler=self.execute,
            risk=ToolRisk.MUTATING,
            timeout_seconds=(
                self.approval_timeout_seconds
                + self.command_timeout_seconds
                + 15.0
            ),
            max_output_chars=_MAX_OUTPUT_CHARS,
            required_privilege=True,
        )

    async def execute(
        self,
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        actor = getattr(context, "actor", None)
        if not bool(getattr(actor, "is_bot_admin", False)):
            raise ToolExecutionError(
                "bash_bot_admin_required",
                "Bash 工具仅允许机器人管理员调用。",
            )
        if self._closed:
            raise ToolExecutionError("bash_unavailable", "Bash 工具正在关闭。")
        command = str(arguments.get("command") or "")
        if not command.strip() or len(command) > _MAX_COMMAND_CHARS:
            raise ToolExecutionError("invalid_bash_command", "Shell 命令为空或过长。")
        sink = getattr(context, "message_sink", None)
        send = getattr(sink, "send", None)
        if not callable(send):
            raise ToolExecutionError(
                "bash_review_unavailable",
                "当前会话无法发送命令审核请求。",
            )

        loop = asyncio.get_running_loop()
        request_id = self._new_request_id()
        pending = _PendingApproval(
            request_id=request_id,
            scope=_conversation_scope(context),
            requester_external_id=str(
                getattr(getattr(context, "event", None), "user_id", "") or ""
            ),
            command=command,
            future=loop.create_future(),
        )
        self._pending[request_id] = pending
        try:
            await send(
                self._review_message(pending),
                at_user_ids=(str(getattr(context.event, "user_id", "") or ""),),
                source="bash_review",
            )
            decision = await self._wait_for_decision(
                pending,
                getattr(context, "interrupt_event", None),
            )
            if decision != "approved":
                return {
                    "executed": False,
                    "status": decision,
                    "request_id": request_id,
                }
            return await self._run_command(command)
        except asyncio.CancelledError:
            raise
        except ToolExecutionError:
            raise
        except Exception as exc:
            raise ToolExecutionError(
                "bash_execution_failed",
                "命令审核或执行失败，命令未成功完成。",
            ) from exc
        finally:
            self._pending.pop(request_id, None)
            if not pending.future.done():
                pending.future.cancel()

    def resolve(
        self,
        *,
        request_id: str,
        scope: tuple[str, str, str, str],
        speaker_external_id: str,
        approve: bool,
    ) -> str:
        pending = self._pending.get(str(request_id or ""))
        if pending is None:
            return "not_found"
        if pending.scope != tuple(str(item) for item in scope):
            return "wrong_conversation"
        if not speaker_external_id or pending.requester_external_id != str(
            speaker_external_id
        ):
            return "not_requester"
        if pending.future.done():
            return "already_resolved"
        pending.future.set_result("approved" if approve else "rejected")
        return "approved" if approve else "rejected"

    def close(self) -> None:
        self._closed = True
        for pending in tuple(self._pending.values()):
            if not pending.future.done():
                pending.future.set_result("cancelled")

    def _new_request_id(self) -> str:
        while True:
            request_id = secrets.token_hex(4)
            if request_id not in self._pending:
                return request_id

    def _review_message(
        self,
        pending: _PendingApproval,
    ) -> str:
        return (
            "待审核 Shell 命令（尚未执行）\n"
            f"请求 ID：{pending.request_id}\n"
            f"工作目录：{self.project_root}\n"
            "命令原文：\n"
            f"{pending.command}\n"
            f"请发言人于 {int(self.approval_timeout_seconds)} 秒内回复：\n"
            f"{self.reminder}批准Shell {pending.request_id}\n"
            f"{self.reminder}拒绝Shell {pending.request_id}"
        )

    async def _wait_for_decision(
        self,
        pending: _PendingApproval,
        interrupt_event: Any,
    ) -> str:
        approval_wait = asyncio.ensure_future(asyncio.shield(pending.future))
        interrupt_wait = None
        waiters: set[asyncio.Future[Any]] = {approval_wait}
        if interrupt_event is not None and callable(
            getattr(interrupt_event, "wait", None)
        ):
            interrupt_wait = asyncio.create_task(interrupt_event.wait())
            waiters.add(interrupt_wait)
        try:
            done, _ = await asyncio.wait(
                waiters,
                timeout=self.approval_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                return "timed_out"
            if interrupt_wait is not None and interrupt_wait in done:
                return "interrupted"
            return str(approval_wait.result())
        finally:
            for task in waiters:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    async def _run_command(self, command: str) -> Mapping[str, Any]:
        kwargs: dict[str, Any] = {
            "cwd": str(self.project_root),
            "stdin": asyncio.subprocess.DEVNULL,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.STDOUT,
        }
        if os.name == "posix":
            kwargs["start_new_session"] = True
        elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            process = await self._process_factory(command, **kwargs)
        except (OSError, ValueError) as exc:
            raise ToolExecutionError(
                "bash_start_failed",
                "Shell 命令无法启动。",
            ) from exc
        try:
            output, truncated = await asyncio.wait_for(
                _capture_limited_output(process, _MAX_OUTPUT_CHARS),
                timeout=self.command_timeout_seconds,
            )
        except asyncio.CancelledError:
            await self._terminate_process(process)
            raise
        except TimeoutError as exc:
            await self._terminate_process(process)
            raise ToolExecutionError(
                "bash_command_timeout",
                "Shell 命令执行超时，进程已终止。",
            ) from exc
        decoded = bytes(output or b"").decode("utf-8", errors="replace")
        return {
            "executed": True,
            "exit_code": int(getattr(process, "returncode", 0) or 0),
            "output": decoded,
            "truncated": truncated,
            "working_directory": str(self.project_root),
        }

    @staticmethod
    async def _terminate_process(process: Any) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (ProcessLookupError, PermissionError, OSError):
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                process.kill()
        await process.wait()


async def _capture_limited_output(
    process: Any,
    limit: int,
) -> tuple[bytes, bool]:
    stream = getattr(process, "stdout", None)
    if stream is None:
        output, _ = await process.communicate()
        raw = bytes(output or b"")
        return raw[:limit], len(raw) > limit
    captured = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        remaining = limit - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated = True
    await process.wait()
    return bytes(captured), truncated


def _conversation_scope(context: ToolContext) -> tuple[str, str, str, str]:
    conversation = context.conversation
    kind = getattr(conversation, "kind", "")
    return (
        str(getattr(conversation, "protocol", "")),
        str(getattr(conversation, "self_id", "")),
        str(getattr(kind, "value", kind)),
        str(getattr(conversation, "conversation_id", "")),
    )


__all__ = ["ShellApprovalManager"]
