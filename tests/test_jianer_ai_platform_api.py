from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jianer.adapters import ConversationKey, ConversationKind

from plugins.JianerAI.permissions import Actor, ActorResolver
from plugins.JianerAI.tools import (
    ToolCall,
    ToolContext,
    ToolRegistry,
    ToolRisk,
    register_platform_api_tools,
)
from plugins.JianerAI.tools.platform_api import (
    PlatformTransport,
    parse_platform_command,
)


class FakeMemory:
    def list_memories(self, **kwargs):
        return ()


class FakeCustomActions:
    def __init__(self):
        self.calls = []

    def __getattr__(self, item):
        async def wrapper(**kwargs):
            self.calls.append((item, kwargs))
            return f"{item}_echo"

        return wrapper


class FakeConnection:
    def __init__(self):
        self.calls = []

    def http_send(self, endpoint, data, *, timeout_seconds, attempts):
        self.calls.append((endpoint, data))
        return {"status": "ok", "retcode": 0, "data": {"message_seq": 1}}


class FakeFeishuClient:
    def __init__(self):
        self.calls = []

    def _request(self, method, path, params=None, json_body=None):
        self.calls.append((method, path, params, json_body))
        return {"code": 0, "data": {"message_id": "om_1"}}


class FakeActions:
    def __init__(self, protocol, *, custom=None, connection=None, client=None):
        self.protocol = protocol
        self.capabilities = frozenset()
        self.custom = custom
        self.connection = connection
        self.client = client
        self.sent = []

    async def send(self, message, **target):
        self.sent.append((target, message))
        return SimpleNamespace(data=SimpleNamespace(message_id="sent-1"))


def _context(
    actions,
    *,
    protocol="onebot",
    kind=ConversationKind.GROUP,
    conversation_id="group-100",
    user_id="user-42",
    actor=None,
):
    event = SimpleNamespace(
        protocol=protocol,
        self_id="bot-1",
        user_id=user_id,
        group_id=conversation_id if kind is ConversationKind.GROUP else None,
    )
    key = ConversationKey(
        protocol=protocol,
        self_id="bot-1",
        kind=kind,
        conversation_id=conversation_id,
        preset="Normal",
    )
    return ToolContext(
        event=event,
        actions=actions,
        conversation=key,
        canonical_user_id="qq:user-42",
        runtime={},
        memory=FakeMemory(),
        actor=actor,
    )


def _registry(*, privileged=True):
    registry = ToolRegistry(
        allowed_risks=frozenset(
            {ToolRisk.READ_ONLY, ToolRisk.MUTATING, ToolRisk.PRIVILEGED}
        ),
    )
    register_platform_api_tools(registry)
    return registry


# ---------------------------------------------------------------------------
# Alconna command parsing
# ---------------------------------------------------------------------------


def test_platform_command_parser_handles_known_and_raw_commands():
    parsed = parse_platform_command("发送群消息 12345 你好呀 世界")
    assert parsed is not None
    assert parsed.action == "send_group_message"
    assert parsed.params == {"group_id": "12345", "text": "你好呀 世界"}

    ban = parse_platform_command("禁言成员 12345 678 60")
    assert ban is not None and ban.action == "set_group_ban"
    assert ban.params["duration"] == 60

    recall = parse_platform_command("撤回消息 987")
    assert recall is not None and recall.action == "delete_msg"

    raw = parse_platform_command('send_msg {"group_id": 1, "message": []}')
    assert raw is not None and raw.action == "send_msg"
    assert raw.params == {"group_id": 1, "message": []}

    assert parse_platform_command("这不是命令") is None


# ---------------------------------------------------------------------------
# Transport mapping per protocol
# ---------------------------------------------------------------------------


def test_platform_transport_maps_onebot_milky_and_lark():
    async def scenario():
        onebot_custom = FakeCustomActions()
        onebot_actions = FakeActions("onebot", custom=onebot_custom)
        transport = PlatformTransport(onebot_actions, timeout_seconds=0.05)
        result = await transport.call("get_status", {})
        assert onebot_custom.calls[0][0] == "get_status"
        assert isinstance(result, dict)

        milky_connection = FakeConnection()
        milky_actions = FakeActions("milky", connection=milky_connection)
        transport = PlatformTransport(milky_actions, timeout_seconds=0.05)
        await transport.call("send_group_message", {"group_id": 1})
        assert milky_connection.calls[0][0] == "send_group_message"

        client = FakeFeishuClient()
        lark_actions = FakeActions("feishu", client=client)
        transport = PlatformTransport(lark_actions)
        await transport.call(
            "/open-apis/im/v1/messages",
            {"method": "POST", "json": {"a": 1}},
        )
        assert client.calls[0][0] == "POST"
        assert client.calls[0][1] == "/open-apis/im/v1/messages"

    asyncio.run(scenario())


def test_platform_transport_rejects_unknown_protocol():
    async def scenario():
        transport = PlatformTransport(FakeActions("kritor"))
        with pytest.raises(Exception):
            await transport.call("whatever", {})

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Privilege gating
# ---------------------------------------------------------------------------


def test_privileged_tools_hidden_from_ordinary_members():
    registry = _registry()
    ordinary = _context(FakeActions("onebot"), actor=Actor(user_id="u"))
    names = {spec.name for spec in registry.available(ordinary)}
    assert "send_message" in names
    assert "call_platform_api" not in names
    assert "platform_command" not in names

    manager = _context(
        FakeActions("onebot"),
        actor=Actor(user_id="u", is_group_admin=True),
    )
    privileged_names = {spec.name for spec in registry.available(manager)}
    assert "call_platform_api" in privileged_names
    assert "platform_command" in privileged_names


def test_privileged_tool_execution_denied_for_ordinary_member():
    async def scenario():
        registry = _registry()
        context = _context(FakeActions("onebot"), actor=Actor(user_id="u"))
        result = await registry.execute(
            ToolCall("call", "call_platform_api", {"action": "get_status"}),
            context,
        )
        assert result.ok is False
        assert result.error_code == "tool_not_allowed"

    asyncio.run(scenario())


def test_platform_command_executes_for_group_manager():
    async def scenario():
        registry = _registry()
        custom = FakeCustomActions()
        actions = FakeActions("onebot", custom=custom)
        context = _context(actions, actor=Actor(user_id="u", is_group_admin=True))
        result = await registry.execute(
            ToolCall(
                "cmd",
                "platform_command",
                {"command": "查询登录信息"},
            ),
            context,
        )
        assert result.ok is True

    asyncio.run(scenario())


def test_send_message_tool_targets_current_conversation_and_cross_denied():
    async def scenario():
        registry = _registry()
        actions = FakeActions("onebot")
        context = _context(
            actions,
            actor=Actor(user_id="u", is_group_admin=True),
        )
        result = await registry.execute(
            ToolCall("send", "send_message", {"text": "你好", "at_user_ids": ["7"]}),
            context,
        )
        assert result.ok is True
        target, message = actions.sent[0]
        assert target == {"group_id": "group-100"}
        segments = list(message)
        assert segments[0].qq == "7"

        denied = await registry.execute(
            ToolCall("cross", "send_message", {"text": "hi", "group_id": "999"}),
            context,
        )
        assert denied.ok is False
        assert denied.error_code == "cross_conversation_denied"

        admin = _context(
            actions,
            actor=Actor(user_id="u", is_bot_admin=True),
        )
        allowed = await registry.execute(
            ToolCall("cross2", "send_message", {"text": "hi", "group_id": "999"}),
            admin,
        )
        assert allowed.ok is True
        assert actions.sent[-1][0] == {"group_id": "999"}

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Actor resolution
# ---------------------------------------------------------------------------


def test_actor_resolver_uses_sender_role_and_bot_admins():
    async def scenario():
        resolver = ActorResolver()
        event = SimpleNamespace(
            user_id="42",
            group_id="100",
            sender={"role": "admin"},
        )
        actor = await resolver.resolve(
            event,
            FakeActions("onebot"),
            {"admins": ["7"], "root_users": []},
        )
        assert actor.is_group_admin is True
        assert actor.is_privileged is True
        assert actor.is_bot_admin is False

        member = await resolver.resolve(
            SimpleNamespace(user_id="7", group_id="100", sender={"role": "member"}),
            FakeActions("onebot"),
            {"root_users": ["7"]},
        )
        assert member.is_bot_admin is True
        assert member.is_privileged is True

    asyncio.run(scenario())


def test_actor_resolver_falls_back_to_member_lookup_with_cache():
    class LookupActions:
        protocol = "onebot"
        capabilities = frozenset()

        def __init__(self):
            self.calls = 0

        async def get_group_member_info(self, group_id, user_id):
            self.calls += 1
            return SimpleNamespace(data=SimpleNamespace(role="owner"))

    async def scenario():
        actions = LookupActions()
        resolver = ActorResolver()
        event = SimpleNamespace(user_id="42", group_id="100")
        first = await resolver.resolve(event, actions, {})
        second = await resolver.resolve(event, actions, {})
        assert first.is_group_owner is True
        assert second.is_group_owner is True
        assert actions.calls == 1

    asyncio.run(scenario())
