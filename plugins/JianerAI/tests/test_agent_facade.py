from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from jianer.adapters import ConversationKey, ConversationKind

from plugins.JianerAI.agent_runtime.facade import AgentFacade
from plugins.JianerAI.agent_runtime.models import SessionKey
from plugins.JianerAI.service import HypeRMessageSink
from plugins.JianerAI.tools.contracts import ToolContext


def _event(
    *,
    text: str = "hello",
    mentioned: bool = True,
    group_id: str = "group-1",
):
    return SimpleNamespace(
        message=[SimpleNamespace(type="text")],
        msg_str=text,
        user_id="user-1",
        group_id=group_id,
        self_id="bot-1",
        protocol="milky",
        message_id="message-1",
        is_mentioned=mentioned,
        sender={"nickname": "User", "card": "", "role": "member"},
    )


def _context() -> ToolContext:
    return ToolContext(
        event=_event(),
        actions=SimpleNamespace(capabilities=frozenset()),
        conversation=ConversationKey(
            protocol="milky",
            self_id="bot-1",
            kind=ConversationKind.GROUP,
            conversation_id="group-1",
            preset="default",
        ),
        canonical_user_id="qq:user-1",
        runtime={},
        memory=None,
        ev_type="group",
        scene_id="group-1",
        perm_group="member",
        principal_id="user-1",
        self_id="bot-1",
    )


def test_group_without_whitelist_does_not_cache_or_process(tmp_path: Path) -> None:
    core_calls: list[str] = []
    sessions = _Sessions(core_calls)
    facade, _, _ = _facade(tmp_path, sessions)

    handled = asyncio.run(
        facade.observe(_event(mentioned=False), SimpleNamespace())
    )
    assert handled is False
    assert facade._collectors == {}


def test_mention_processes_buffered_batch_immediately(tmp_path: Path) -> None:
    core_calls: list[str] = []
    after_runs: list[Exception | None] = []
    sessions = _Sessions(core_calls)
    facade, _, _ = _facade(tmp_path, sessions, after_runs=after_runs)
    key = facade.session_key(_event())
    facade.set_whitelisted(key, "user-1", True)

    async def scenario():
        handled = await facade.observe(_event(), SimpleNamespace())
        assert handled is True
        await asyncio.sleep(0.05)
        await facade.close()

    asyncio.run(scenario())
    assert core_calls
    assert after_runs == [None]


def test_agent_on_off_command_updates_whitelist(tmp_path: Path) -> None:
    sessions = _Sessions([])
    facade, replies, _ = _facade(tmp_path, sessions)
    event = _event(text="~agent.on", mentioned=False)
    key = facade.session_key(event)

    async def scenario():
        assert await facade.fallback(event, SimpleNamespace(), text=event.msg_str)
        event.message_id = "message-2"
        event.msg_str = "~agent.off"
        assert await facade.fallback(event, SimpleNamespace(), text=event.msg_str)

    asyncio.run(scenario())
    assert "user-1" not in facade.group_whitelist(key)
    assert len(replies) == 2


def test_hype_sink_applies_suffix_and_emits_one_tts() -> None:
    sent: list[str] = []
    speech: list[str] = []

    class Suffixes:
        @staticmethod
        def apply_ai_reply(text, canonical):
            del canonical
            return text + "后缀"

    service = SimpleNamespace(
        suffixes=Suffixes(),
        options=SimpleNamespace(content_moderation_ai_reply_enabled=False),
        _plain_text_reply=lambda text: text,
        _histories={},
        _tts_for=lambda key: True,
    )

    async def send_ai_text(event, actions, text, *, reply, at_user_ids=()):
        del event, actions, reply, at_user_ids
        sent.append(text)

    async def send_speech(event, actions, text):
        del event, actions
        speech.append(text)

    service._send_ai_text = send_ai_text
    service._send_speech = send_speech
    key = ConversationKey(
        protocol="milky",
        self_id="bot-1",
        kind=ConversationKind.GROUP,
        conversation_id="group-1",
        preset="Role",
    )
    sink = HypeRMessageSink(service, _event(), SimpleNamespace(), key, "qq:user")

    async def scenario():
        await sink.send("first")
        await sink.send("second")
        await sink.finalize()

    asyncio.run(scenario())
    assert sent == ["first后缀", "second后缀"]
    assert speech == ["first后缀\nsecond后缀"]


class _Core:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.history = []
        self.working = False

    async def event_handler(self, event, *, context, tool_choice="auto"):
        del context, tool_choice
        self.calls.append(event)
        return SimpleNamespace(text="{}")

    def should_auto_summarize(self) -> bool:
        return False

    def processing_elapsed(self):
        return None


class _Sessions:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def get_main(self, key: SessionKey):
        del key
        return _Core(self.calls)

    async def summarize(self, key, *, source="manual"):
        del key, source
        return "summarized"

    async def context_status(self, key):
        return key.value

    async def close(self):
        return None


def _facade(
    tmp_path: Path,
    sessions: _Sessions,
    *,
    after_runs: list[Exception | None] | None = None,
) -> tuple[AgentFacade, list[str], list[Exception | None]]:
    replies: list[str] = []
    runs = after_runs if after_runs is not None else []
    profiles = SimpleNamespace(
        active_name="Role",
        list=lambda: {"Role": object()},
        upsert=lambda *args, **kwargs: None,
        save=lambda: None,
    )

    async def build_context(*args, **kwargs):
        del args, kwargs
        return _context()

    async def after_run(*args):
        runs.append(args[-1])

    async def reply(event, actions, text):
        del event, actions
        replies.append(text)

    return (
        AgentFacade(
            sessions=sessions,
            profiles=profiles,
            reminder="~",
            owner_ids=("owner",),
            config_path=tmp_path / "config.json",
            build_context=build_context,
            after_run=after_run,
            reply=reply,
            take_notices=lambda _key: (),
        ),
        replies,
        runs,
    )
