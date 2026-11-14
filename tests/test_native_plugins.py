"""Contract tests for the five native migration plugins."""

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from jianer import Client, segments
from jianer.adapters.contracts import Capability
from jianer.plugins import PluginManager

from bot import plugin_state


ROOT = Path(__file__).resolve().parents[1]


class FakeCustom:
    def __init__(self, actions):
        self.actions = actions

    async def get_group_list(self):
        return SimpleNamespace(
            data=SimpleNamespace(
                raw=[{"group_id": "100"}, {"group_id": "200"}]
            )
        )

    async def group_poke(self, **kwargs):
        self.actions.calls.append(("group_poke", kwargs))

    async def friend_poke(self, **kwargs):
        self.actions.calls.append(("friend_poke", kwargs))

    async def set_group_whole_ban(self, **kwargs):
        self.actions.calls.append(("whole_ban", kwargs))

    async def set_group_leave(self, **kwargs):
        self.actions.calls.append(("leave", kwargs))


class FakeActions:
    protocol = "onebot"
    capabilities = frozenset({Capability.NATIVE_GROUP_FORWARD})

    def __init__(self):
        self.sent = []
        self.calls = []
        self.custom = FakeCustom(self)

    async def send(self, message, group_id=None, user_id=None):
        self.sent.append((message, group_id, user_id))
        return SimpleNamespace(data={"message_id": str(len(self.sent))})

    async def set_group_ban(self, **kwargs):
        self.calls.append(("ban", kwargs))

    async def set_group_kick(self, **kwargs):
        self.calls.append(("kick", kwargs))

    async def set_group_special_title(self, **kwargs):
        self.calls.append(("title", kwargs))

    async def del_message(self, message_id):
        self.calls.append(("delete", message_id))

    async def get_version_info(self):
        return SimpleNamespace(
            data=SimpleNamespace(
                raw={
                    "app_name": "TestBot",
                    "app_version": "1.0",
                    "protocol_version": "v11",
                }
            )
        )


def _configure(tmp_path: Path, *, admins=("1",)) -> None:
    if not (tmp_path / "config.json").exists():
        (tmp_path / "config.json").write_text(
            (ROOT / "config.example.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    (tmp_path / "Super_User.ini").write_text("\n".join(admins), encoding="utf-8")
    (tmp_path / "Manage_User.ini").write_text("", encoding="utf-8")
    plugin_state.configure(
        config=SimpleNamespace(
            protocol="OneBot",
            owner=[],
            others={
                "reminder": "~",
                "bot_name": "简儿",
                "bot_name_en": "Jianer",
                "slogan": "测试",
                "compliment": ["谢谢夸奖"],
                "poke_rejection_phrases": ["别戳啦"],
                "Auto_approval": ["答案"],
            },
        ),
        logger=logging.getLogger("native_plugin_test"),
        reminder="~",
        bot_name="简儿",
        bot_name_en="Jianer",
        one_slogan="测试",
        confused_word="{bot_name}不能这么做",
        root_users=["1"],
        cooldowns={},
        cooldowns1={},
    )
    plugin_state.get_runtime().update(
        {
            "started_at": 0,
            "emoji_repeat_enabled": True,
            "emoji_send_count": None,
            "self_service_titles": False,
            "wait_for_add_in": False,
        }
    )
    plugin_state.set_auth_snapshot(
        admins=list(admins),
        supers=list(admins),
        root_users=["1"],
        super_users=list(admins),
        manage_users=[],
    )


def _manager(plugin_dir: str, tmp_path: Path, *, protocol="onebot"):
    _configure(tmp_path)
    manager = PluginManager()
    result = manager.load_plugins(ROOT / "plugins" / plugin_dir)
    assert result.failed == []
    client = Client()
    manager.setup_client(client)
    actions = FakeActions()
    actions.protocol = protocol
    if protocol == "feishu":
        actions.capabilities = frozenset({
            Capability.SEND_REPLY,
            Capability.SEND_IMAGE,
        })
    return manager, client, actions


def _event(
    text: str,
    *,
    user_id: str = "1",
    group_id: str | None = "100",
    message=None,
):
    return SimpleNamespace(
        msg_str=text,
        message=message if message is not None else text,
        group_id=group_id,
        user_id=user_id,
        self_id="999",
        message_id="m-1",
        sender={"nickname": "tester", "card": "tester"},
        time_str="12:00:00",
        is_mentioned=False,
    )


def _texts(actions: FakeActions) -> str:
    return "\n".join(str(message) for message, _, _ in actions.sent)


def _run(manager, event, actions):
    return asyncio.run(manager.dispatch(event, actions, run_observers=False))


def test_interaction_ping_praise_and_emoji_toggle(tmp_path, monkeypatch):
    async def fake_nickname(*args, **kwargs):
        return "tester"

    monkeypatch.setattr("bot.plugin_support.get_user_nickname", fake_nickname)
    manager, client, actions = _manager("InteractionEvents", tmp_path)
    assert _run(manager, _event("ping"), actions) is True
    assert "pong" in _texts(actions)

    actions.sent.clear()
    assert _run(manager, _event("简儿真棒"), actions) is True
    assert "谢谢夸奖" in _texts(actions)

    actions.sent.clear()
    assert _run(manager, _event("😀"), actions) is False
    assert "😀" in _texts(actions)

    actions.sent.clear()
    assert _run(manager, _event("~表情复述"), actions) is True
    actions.sent.clear()
    assert _run(manager, _event("😀"), actions) is False
    assert actions.sent == []
    asyncio.run(manager.shutdown())
    asyncio.run(client.aclose())


def test_help_info_admin_and_private_help(tmp_path, monkeypatch):
    manager, client, actions = _manager("HelpInfo", tmp_path)
    assert _run(manager, _event("~设置帮助模式 文本"), actions) is True
    assert "已切换" in _texts(actions)
    actions.sent.clear()
    assert _run(manager, _event("~帮助"), actions) is True
    assert "管理我们的简儿" in _texts(actions)

    actions.sent.clear()
    assert _run(manager, _event("~用户帮助", group_id=None), actions) is True
    assert "如何与简儿私聊" in _texts(actions)

    actions.sent.clear()
    event = _event("帮助", group_id="100")
    event.is_mentioned = True
    assert _run(manager, event, actions) is True
    assert "管理我们的简儿" in _texts(actions)
    asyncio.run(manager.shutdown())
    asyncio.run(client.aclose())


def test_admin_calm_and_feishu_degradation(tmp_path, monkeypatch):
    async def fake_nickname(*args, **kwargs):
        return "tester"

    monkeypatch.setattr("bot.plugin_support.get_user_nickname", fake_nickname)
    manager, client, actions = _manager("Admin", tmp_path)
    event = _event(
        "~冷静 @target 60",
        message=[
            segments.Text("~冷静 "),
            segments.At("2"),
            segments.Text(" 60"),
        ],
    )
    assert _run(manager, event, actions) is True
    assert actions.calls[0][0] == "ban"
    assert actions.calls[0][1]["user_id"] == "2"
    assert actions.calls[0][1]["duration"] == "60"

    actions.sent.clear()
    feishu = FakeActions()
    feishu.protocol = "feishu"
    feishu.capabilities = frozenset()
    assert _run(manager, event, feishu) is True
    assert "不支持" in _texts(feishu)
    asyncio.run(manager.shutdown())
    asyncio.run(client.aclose())


def test_broadcast_skips_blacklist_and_preserves_text(tmp_path, monkeypatch):
    async def fake_nickname(*args, **kwargs):
        return "tester"

    monkeypatch.setattr("bot.plugin_support.get_user_nickname", fake_nickname)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "blacklist.sr").write_text("200\n", encoding="utf-8")
    manager, client, actions = _manager("Broadcast", tmp_path)
    assert _run(
        manager,
        _event(
            "~群发 原始群发文本",
            message=[segments.Text("~群发 原始群发文本")],
        ),
        actions,
    ) is True
    group_sends = [target for _, target, _ in actions.sent if target is not None]
    assert "100" in group_sends
    assert "200" not in group_sends
    assert any("原始群发文本" in str(message) for message, _, _ in actions.sent)
    asyncio.run(manager.shutdown())
    asyncio.run(client.aclose())


def test_feishu_binding_rejects_qq_and_queries_feishu(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager, client, actions = _manager("FeishuBinding", tmp_path)
    assert _run(manager, _event("~绑定QQ 123456"), actions) is True
    assert "仅支持飞书" in _texts(actions)

    actions.sent.clear()
    feishu = FakeActions()
    feishu.protocol = "feishu"
    monkeypatch.setattr(
        "bot.feishu_bindings.get_bound_qq",
        lambda open_id: "123456",
    )
    assert _run(manager, _event("~我的绑定", user_id="ou_1"), feishu) is True
    assert "123456" in _texts(feishu)
    asyncio.run(manager.shutdown())
    asyncio.run(client.aclose())
