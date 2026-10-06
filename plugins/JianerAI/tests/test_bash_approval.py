from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.JianerAI.tools.bash import ShellApprovalManager
from plugins.JianerAI.tools.contracts import ToolExecutionError, ToolRisk
from plugins.JianerAI.tools.registry import ToolRegistry


class _Reader:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = iter(chunks)

    async def read(self, size: int) -> bytes:
        return next(self._chunks, b"")


class _Process:
    def __init__(self, output: bytes = b"done") -> None:
        self.stdout = _Reader([output])
        self.returncode = None
        self.pid = 12345
        self.killed = False

    async def wait(self) -> int:
        self.returncode = 0
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class _Sink:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, object]]] = []
        self.sent = asyncio.Event()

    async def send(self, text: str, **kwargs: object) -> None:
        self.messages.append((text, kwargs))
        self.sent.set()


def _context(*, bot_admin: bool = True, interrupt_event=None):
    return SimpleNamespace(
        actor=SimpleNamespace(is_bot_admin=bot_admin, is_privileged=True),
        canonical_user_id="canonical:admin",
        event=SimpleNamespace(user_id="platform-admin"),
        conversation=SimpleNamespace(
            protocol="milky",
            self_id="bot-1",
            kind=SimpleNamespace(value="group"),
            conversation_id="group-1",
        ),
        message_sink=_Sink(),
        interrupt_event=interrupt_event,
    )


def _scope(context) -> tuple[str, str, str, str]:
    return (
        context.conversation.protocol,
        context.conversation.self_id,
        context.conversation.kind.value,
        context.conversation.conversation_id,
    )


def _request_id(message: str) -> str:
    return next(
        line.removeprefix("请求 ID：")
        for line in message.splitlines()
        if line.startswith("请求 ID：")
    )


def test_bash_is_hidden_from_non_bot_admins_and_handler_rechecks_admin():
    manager = ShellApprovalManager(project_root=Path.cwd())
    tool = manager.tool()
    context = _context(bot_admin=False)
    registry = ToolRegistry(
        allowed_risks=frozenset({ToolRisk.MUTATING}),
        allowed_mutating_tools=frozenset({"bash"}),
    )
    registry.register(tool)
    context.tool_permissions = frozenset()
    context.actions = SimpleNamespace(capabilities=frozenset())

    assert tool.required_privilege is True
    assert registry.available(context) == ()

    async def run():
        with pytest.raises(ToolExecutionError) as error:
            await manager.execute(context, {"command": "whoami"})
        assert error.value.code == "bash_bot_admin_required"
        assert context.message_sink.messages == []

    asyncio.run(run())


def test_command_is_posted_before_execution_and_only_requester_can_approve():
    started: list[tuple[str, dict[str, object]]] = []
    process = _Process()

    async def process_factory(command: str, **kwargs: object) -> _Process:
        started.append((command, kwargs))
        return process

    manager = ShellApprovalManager(
        project_root=Path.cwd(),
        reminder="#",
        process_factory=process_factory,
    )
    context = _context()

    async def run():
        task = asyncio.create_task(
            manager.execute(context, {"command": "printf reviewed"})
        )
        await asyncio.wait_for(context.message_sink.sent.wait(), timeout=1)
        message, send_options = context.message_sink.messages[0]
        request_id = _request_id(message)

        assert "命令原文：\nprintf reviewed" in message
        assert "#批准Shell " + request_id in message
        assert send_options["source"] == "bash_review"
        assert send_options["at_user_ids"] == ("platform-admin",)
        assert started == []

        assert manager.resolve(
            request_id=request_id,
            scope=_scope(context),
            speaker_external_id="someone-else",
            approve=True,
        ) == "not_requester"
        assert started == []

        assert manager.resolve(
            request_id=request_id,
            scope=_scope(context),
            speaker_external_id="platform-admin",
            approve=True,
        ) == "approved"
        assert manager.resolve(
            request_id=request_id,
            scope=_scope(context),
            speaker_external_id="platform-admin",
            approve=True,
        ) == "already_resolved"

        result = await task
        assert result["executed"] is True
        assert result["output"] == "done"
        assert len(started) == 1
        assert started[0][0] == "printf reviewed"

    asyncio.run(run())


@pytest.mark.parametrize("decision", ["reject", "interrupt", "close", "timeout"])
def test_unapproved_command_is_never_started(decision: str):
    started: list[str] = []

    async def process_factory(command: str, **kwargs: object) -> _Process:
        started.append(command)
        return _Process()

    interrupt = asyncio.Event()
    manager = ShellApprovalManager(
        project_root=Path.cwd(),
        process_factory=process_factory,
    )
    if decision == "timeout":
        manager.approval_timeout_seconds = 0.01
    context = _context(interrupt_event=interrupt)

    async def run():
        task = asyncio.create_task(
            manager.execute(context, {"command": "printf never"})
        )
        await asyncio.wait_for(context.message_sink.sent.wait(), timeout=1)
        request_id = _request_id(context.message_sink.messages[0][0])

        if decision == "reject":
            assert manager.resolve(
                request_id=request_id,
                scope=_scope(context),
                speaker_external_id="platform-admin",
                approve=False,
            ) == "rejected"
        elif decision == "interrupt":
            interrupt.set()
        elif decision == "close":
            manager.close()

        result = await asyncio.wait_for(task, timeout=1)
        expected = {
            "reject": "rejected",
            "interrupt": "interrupted",
            "close": "cancelled",
            "timeout": "timed_out",
        }[decision]
        assert result == {
            "executed": False,
            "status": expected,
            "request_id": request_id,
        }
        assert started == []

    asyncio.run(run())
