from __future__ import annotations

import asyncio
import hashlib
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


def _context(
    *,
    bot_admin: bool = True,
    interrupt_event=None,
    runtime=None,
    actions=None,
    user_id: str = "platform-admin",
):
    return SimpleNamespace(
        actor=SimpleNamespace(is_bot_admin=bot_admin, is_privileged=True),
        canonical_user_id="canonical:admin",
        event=SimpleNamespace(
            user_id=user_id,
            sender={"card": "桃子", "nickname": "桃"},
        ),
        conversation=SimpleNamespace(
            protocol="milky",
            self_id="bot-1",
            kind=SimpleNamespace(value="group"),
            conversation_id="group-1",
        ),
        runtime=runtime or {},
        actions=actions or SimpleNamespace(),
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


def test_shell_approval_request_id_is_sha256(monkeypatch) -> None:
    monkeypatch.setattr("plugins.JianerAI.tools.bash.os.getpid", lambda: 123)
    monkeypatch.setattr(
        "plugins.JianerAI.tools.bash.time.time_ns",
        lambda: 456789,
    )
    manager = ShellApprovalManager(project_root=Path.cwd())
    scope = ("milky", "bot-1", "group", "group-1")
    request_id = manager._new_request_id(
        scope=scope,
        requester_external_id="platform-admin",
        command="printf reviewed",
    )
    material = "\x00".join(
        (
            *scope,
            "platform-admin",
            "printf reviewed",
            "123",
            "456789",
            "1",
        )
    )
    expected = hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]

    assert request_id == expected
    assert len(request_id) == 8


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


def test_bash_request_is_also_sent_to_root_user():
    root_messages: list[dict[str, object]] = []

    async def root_send(**kwargs: object) -> None:
        root_messages.append(kwargs)

    actions = SimpleNamespace(send=root_send)
    context = _context(
        runtime={"root_users": ["10001"]},
        actions=actions,
        user_id="2822554898",
    )
    manager = ShellApprovalManager(
        project_root=Path.cwd(),
        process_factory=lambda *_args, **_kwargs: _Process(),
    )

    async def run():
        task = asyncio.create_task(
            manager.execute(context, {"command": "git pull origin dev"})
        )
        await asyncio.wait_for(context.message_sink.sent.wait(), timeout=1)
        assert len(root_messages) == 1
        assert root_messages[0]["user_id"] == "10001"
        notice = str(root_messages[0]["message"])
        assert "用户 @桃子(2822554898)" in notice
        assert "以Agent的Bash工具执行了以下命令" in notice
        assert notice.endswith("git pull origin dev")
        request_id = _request_id(context.message_sink.messages[0][0])
        manager.resolve(
            request_id=request_id,
            scope=_scope(context),
            speaker_external_id="2822554898",
            approve=False,
        )
        await task

    asyncio.run(run())


def test_dangerous_command_is_blocked_after_root_notification():
    root_messages: list[dict[str, object]] = []

    async def root_send(**kwargs: object) -> None:
        root_messages.append(kwargs)

    context = _context(
        runtime={"root_users": ["10001"]},
        actions=SimpleNamespace(send=root_send),
        user_id="2822554898",
    )
    manager = ShellApprovalManager(project_root=Path.cwd())

    async def run():
        with pytest.raises(ToolExecutionError) as error:
            await manager.execute(context, {"command": "rm -rf /"})
        assert error.value.code == "bash_dangerous_command"
        assert len(root_messages) == 1
        assert context.message_sink.messages == []

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
