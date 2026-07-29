import asyncio
from types import SimpleNamespace

from bot import broadcast, group_commands, misc_commands


class FakeSegments:
    class Text:
        def __init__(self, text):
            self.text = str(text)

        def __str__(self):
            return self.text

    class Reply:
        def __init__(self, message_id):
            self.id = str(message_id)


class FakeManager:
    class Ret:
        @staticmethod
        def fetch(value):
            return value

    @staticmethod
    def Message(*segments):
        return tuple(segments)


class FakeActions:
    def __init__(self):
        self.sent = []
        self.custom = SimpleNamespace(get_group_list=self._group_list)

    async def _group_list(self):
        return SimpleNamespace(
            data=SimpleNamespace(raw=[{"group_id": 100}]),
        )

    async def send(self, message, **target):
        self.sent.append((target, message))


def _message_text(message):
    return "".join(
        segment.text
        for segment in message
        if isinstance(segment, FakeSegments.Text)
    )


def test_ping_and_sleep_messages_are_not_processed_by_ai_suffixes():
    async def scenario():
        actions = FakeActions()
        event = SimpleNamespace(
            group_id=100,
            user_id=1,
            message_id="message-1",
            time_str="12:00:00",
            sender={"nickname": "admin"},
        )

        await group_commands.cmd_ping(
            actions,
            FakeManager,
            FakeSegments,
            event,
        )
        assert _message_text(actions.sent[-1][1]) == "pong! 爆炸！v(◦'ωˉ◦)~♡ "

        async def nickname(*args, **kwargs):
            return "admin"

        assert await misc_commands.cmd_sleep(
            actions,
            FakeManager,
            FakeSegments,
            event,
            ["1"],
            ["9"],
            "{bot_name}不能这么做。",
            "简儿",
            nickname,
        )
        assert (
            _message_text(actions.sent[-1][1])
            == "谢谢喵，简儿睡觉去了 ヾ(＠ ˘ω˘ ＠)ノ💤"
        )

    asyncio.run(scenario())


def test_broadcast_sends_original_text_without_ai_suffix(monkeypatch):
    async def scenario():
        actions = FakeActions()

        async def no_sleep(_seconds):
            return None

        monkeypatch.setattr(broadcast, "load_blacklist", lambda: set())
        monkeypatch.setattr(broadcast.asyncio, "sleep", no_sleep)
        await broadcast.send_msg_all_groups(
            "原始群发文本",
            actions,
            FakeManager,
            FakeSegments,
            logger=SimpleNamespace(
                info=lambda *args, **kwargs: None,
                warning=lambda *args, **kwargs: None,
            ),
        )

        assert actions.sent[0][0] == {"group_id": 100}
        assert _message_text(actions.sent[0][1]) == "原始群发文本"

    asyncio.run(scenario())
