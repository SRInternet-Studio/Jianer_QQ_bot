import asyncio
from types import SimpleNamespace

from Tools import tools


def test_user_info_awaits_and_preserves_successful_result(monkeypatch):
    expected = {"nickname": "协议昵称"}

    async def query_user_info(*_args):
        return True, expected

    monkeypatch.setattr(tools, "get_user_info_from_websocket", query_user_info)

    assert asyncio.run(tools.user_info(10001, None, None)) == (True, expected)


def test_get_user_info_rejects_failed_or_invalid_results(monkeypatch):
    async def failed_query(*_args):
        return False, None

    monkeypatch.setattr(tools, "get_user_info_from_websocket", failed_query)
    assert asyncio.run(tools.get_user_info(10001, None, None)) == (
        False,
        "无法获取用户 10001 的信息",
    )

    async def invalid_query(*_args):
        return True, "not-a-dict"

    monkeypatch.setattr(tools, "get_user_info_from_websocket", invalid_query)
    assert asyncio.run(tools.get_user_info(10001, None, None)) == (
        False,
        "无法获取用户 10001 的信息",
    )


def test_get_user_nickname_accepts_sender_and_prefers_protocol_result(monkeypatch):
    async def query_nickname(*_args):
        return "协议昵称"

    monkeypatch.setattr(tools, "get_nickname_by_userid", query_nickname)

    result = asyncio.run(
        tools.get_user_nickname(
            10001,
            None,
            None,
            sender={"nickname": "事件昵称"},
        )
    )

    assert result == "协议昵称"


def test_get_user_nickname_falls_back_to_sender_fields(monkeypatch):
    async def unknown_nickname(*_args):
        return "未知用户"

    monkeypatch.setattr(tools, "get_nickname_by_userid", unknown_nickname)

    assert asyncio.run(
        tools.get_user_nickname(
            10001,
            None,
            None,
            sender={"nickname": "事件昵称", "card": "群名片"},
        )
    ) == "事件昵称"
    assert asyncio.run(
        tools.get_user_nickname(
            10001,
            None,
            None,
            sender=SimpleNamespace(nickname="", card="群名片"),
        )
    ) == "群名片"
    assert asyncio.run(
        tools.get_user_nickname(
            10001,
            None,
            None,
            sender={"nickname": "", "card": "", "user_id": 20002},
        )
    ) == "20002"


def test_get_user_nickname_falls_back_to_uid_on_query_error(monkeypatch):
    async def failed_query(*_args):
        raise RuntimeError("lookup failed")

    monkeypatch.setattr(tools, "get_nickname_by_userid", failed_query)

    assert asyncio.run(
        tools.get_user_nickname(10001, None, None, sender=None)
    ) == "10001"
