from __future__ import annotations

import asyncio
from types import SimpleNamespace

from plugins.JianerAI.service import JianerAIService
from jianer.adapters import Capability


def test_command_text_is_not_segmented() -> None:
    service = object.__new__(JianerAIService)
    service.options = SimpleNamespace(
        max_reply_chars=20,
        max_reply_parts=3,
    )
    captured: list[list[str]] = []

    async def capture(event, actions, parts, *, reply, at_user_ids):
        del event, actions, reply, at_user_ids
        captured.append(list(parts))

    service._send_text_parts = capture
    text = (
        "AI 管理菜单\n"
        "~模型菜单\n"
        "~人设\n"
        "~Agent 状态"
    )

    asyncio.run(
        service._send_text(
            SimpleNamespace(),
            SimpleNamespace(),
            text,
            reply=False,
        )
    )

    assert captured == [[text]]


def test_send_ai_text_splits_single_newlines_into_messages() -> None:
    service = object.__new__(JianerAIService)
    service.options = SimpleNamespace(
        max_reply_chars=350,
        max_reply_parts=3,
    )
    captured: list[list[str]] = []

    async def capture(event, actions, parts, *, reply, at_user_ids):
        del event, actions, reply, at_user_ids
        captured.append(list(parts))

    service._send_text_parts = capture

    asyncio.run(
        service._send_ai_text(
            SimpleNamespace(),
            SimpleNamespace(),
            "第一段内容\n第二段内容",
            reply=True,
        )
    )

    assert captured == [["第一段内容", "第二段内容"]]


def test_send_ai_text_splits_long_paragraph_and_caps_parts() -> None:
    service = object.__new__(JianerAIService)
    service.options = SimpleNamespace(
        max_reply_chars=20,
        max_reply_parts=3,
    )
    captured: list[list[str]] = []

    async def capture(event, actions, parts, *, reply, at_user_ids):
        del event, actions, reply, at_user_ids
        captured.append(list(parts))

    service._send_text_parts = capture

    asyncio.run(
        service._send_ai_text(
            SimpleNamespace(),
            SimpleNamespace(),
            "一" * 25 + "。" + "二" * 25 + "。" + "三" * 25,
            reply=True,
        )
    )

    assert len(captured[0]) == 3
    assert all(len(part) <= 60 for part in captured[0])
    assert "".join(captured[0]).replace("\n", "") == (
        "一" * 25 + "。" + "二" * 25 + "。" + "三" * 25
    )


def test_send_text_parts_returns_message_ids_and_uses_reply_override() -> None:
    service = object.__new__(JianerAIService)
    service._target_kwargs = lambda event: {"group_id": event.group_id}
    sent = []

    class Actions:
        capabilities = frozenset({Capability.SEND_REPLY})

        @staticmethod
        async def send(**kwargs):
            sent.append(kwargs)
            return SimpleNamespace(
                data=SimpleNamespace(message_id=f"out-{len(sent)}")
            )

    message_ids = asyncio.run(
        service._send_text_parts(
            SimpleNamespace(group_id="group-1", message_id="trigger-1"),
            Actions(),
            ["第一段", "第二段"],
            reply=True,
            reply_to_message_id="target-1",
        )
    )

    assert message_ids == ["out-1", "out-2"]
    assert "target-1" in repr(sent[0]["message"])
    assert "trigger-1" not in repr(sent[0]["message"])
