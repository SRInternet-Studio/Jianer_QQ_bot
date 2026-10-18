import asyncio
from types import SimpleNamespace

from jianer.plugins.builtin import alconna as alconna_plugin

from plugins.JianerMate import setup as mate_setup


class _RecordingService:
    def __init__(self):
        self.calls = []

    async def handle_active_command(self, event, actions, action="", value=""):
        self.calls.append(("active", action, value))
        return True

    async def set_conversation_prompt(self, event, actions, text):
        self.calls.append(("prompt", text))
        return True

    async def clear_conversation_prompt(self, event, actions):
        self.calls.append(("clear_prompt",))
        return True

    async def handle_persona_command(self, event, actions, action="", value=""):
        self.calls.append(("persona", action, value))
        return True

    async def steal_meme_command(self, event, actions):
        self.calls.append(("steal",))
        return True

    async def meme_command(
        self, event, actions, action="", argument="", extra=""
    ):
        self.calls.append(("meme", action, argument, extra))
        return True


def _dispatch(text, service):
    mate_setup._service = service
    event = SimpleNamespace(
        msg_str=text,
        message=text,
        protocol="milky",
        self_id="10001",
        user_id="5",
        group_id="222",
    )
    actions = SimpleNamespace(capabilities=frozenset())
    handled = asyncio.run(alconna_plugin.on_message(event, actions))
    return handled, service.calls


def test_active_speech_commands_are_dispatched():
    service = _RecordingService()
    handled, calls = _dispatch("~主动发言", service)
    assert handled is True
    assert calls == [("active", "", "")]

    handled, calls = _dispatch("~主动发言 开启", service)
    assert handled is True
    assert calls[-1] == ("active", "开启", "")

    handled, calls = _dispatch("~主动发言 频率 0.3", service)
    assert handled is True
    assert calls[-1] == ("active", "频率", "0.3")

    handled, calls = _dispatch("~主动发言 免打扰 23:00-08:00", service)
    assert handled is True
    assert calls[-1] == ("active", "免打扰", "23:00-08:00")


def test_group_prompt_and_meme_commands_are_dispatched():
    service = _RecordingService()
    handled, calls = _dispatch("~设置提示 说话别太长 多用短句", service)
    assert handled is True
    assert calls[-1] == ("prompt", "说话别太长 多用短句")

    handled, calls = _dispatch("~设置群提示 说话别太长", service)
    assert handled is True
    assert calls[-1] == ("prompt", "说话别太长")

    handled, calls = _dispatch("~清空提示", service)
    assert handled is True
    assert calls[-1] == ("clear_prompt",)

    handled, calls = _dispatch("~清空群提示", service)
    assert handled is True
    assert calls[-1] == ("clear_prompt",)

    handled, calls = _dispatch("~偷表情", service)
    assert handled is True
    assert calls[-1] == ("steal",)

    handled, calls = _dispatch("~表情包", service)
    assert handled is True
    assert calls[-1] == ("meme", "", "", "")

    handled, calls = _dispatch("~表情包 列表 2", service)
    assert handled is True
    assert calls[-1] == ("meme", "列表", "2", "")

    handled, calls = _dispatch("~表情包 标签 3 猫,无语", service)
    assert handled is True
    assert calls[-1] == ("meme", "标签", "3", "猫,无语")


def test_persona_commands_are_dispatched():
    service = _RecordingService()
    handled, calls = _dispatch("~人格", service)
    assert handled is True
    assert calls[-1] == ("persona", "", "")

    handled, calls = _dispatch("~人格 状态", service)
    assert handled is True
    assert calls[-1] == ("persona", "状态", "")

    handled, calls = _dispatch("~人格 转换 我是机娘，说话简短", service)
    assert handled is True
    assert calls[-1] == ("persona", "转换", "我是机娘，说话简短")

    handled, calls = _dispatch("~人格 导入 机娘", service)
    assert handled is True
    assert calls[-1] == ("persona", "导入", "机娘")

    handled, calls = _dispatch("~人格 概率 0.2", service)
    assert handled is True
    assert calls[-1] == ("persona", "概率", "0.2")

    handled, calls = _dispatch("~人格 风格", service)
    assert handled is True
    assert calls[-1] == ("persona", "风格", "")


def test_unrelated_text_is_not_handled():
    service = _RecordingService()
    handled, calls = _dispatch("~别的命令", service)
    assert handled is False
    assert calls == []
