import asyncio
from types import SimpleNamespace

from plugins import LikePlugin as setup


class RuntimeLikeManager:
    def __init__(self):
        self.count = 0

    def can_like_today(self, _user_id):
        return self.count < 10

    def record_like(self, _user_id, times):
        self.count += times

    def get_remaining_likes(self, _user_id):
        return max(0, 10 - self.count)


def test_profile_like_milky_uses_endpoint_and_count():
    calls = []

    class Actions:
        protocol = "milky"

        def __init__(self):
            self.connection = SimpleNamespace(http_send=self.http_send)

        def http_send(self, endpoint, params):
            calls.append((endpoint, params))
            return {"status": "ok", "retcode": 0, "data": {}}

    actions = Actions()

    result = asyncio.run(
        setup._send_profile_like(actions, "10001", 10)
    )

    assert result == {"status": "ok", "retcode": 0, "data": {}}
    assert calls == [("send_profile_like", {"user_id": 10001, "count": 10})]


def test_like_command_calls_milky_adapter(monkeypatch):
    manager = RuntimeLikeManager()
    calls = []

    class Actions:
        protocol = "milky"

        def __init__(self):
            self.connection = SimpleNamespace(http_send=self.http_send)

        def http_send(self, endpoint, params):
            calls.append((endpoint, params))
            return {"status": "ok", "retcode": 0, "data": {}}

        async def send(self, message, **_kwargs):
            return "receipt"

    event = SimpleNamespace(user_id=10001, group_id=None, protocol="milky")
    monkeypatch.setattr(setup, "like_manager", manager)

    result = asyncio.run(
        setup._send_like(event, Actions(), "简儿", action="赞")
    )

    assert result is True
    assert calls == [("send_profile_like", {"user_id": 10001, "count": 10})]
    assert manager.count == 10


def test_like_command_calls_onebot_adapter(monkeypatch):
    manager = RuntimeLikeManager()
    calls = []

    class Actions:
        protocol = "onebot"

        def __init__(self):
            self.custom = SimpleNamespace(send_like=self.send_like)

        async def send_like(self, **params):
            calls.append(params)
            return "echo"

        async def send(self, message, **_kwargs):
            return "receipt"

    class Ret:
        @staticmethod
        def fetch(echo, _serializer=None):
            assert echo == "echo"
            return SimpleNamespace(
                status="ok",
                ret_code=0,
                raw={"status": "ok", "retcode": 0},
            )

    event = SimpleNamespace(user_id=10001, group_id=None, protocol="onebot")
    monkeypatch.setattr(setup.Manager, "Ret", Ret)
    monkeypatch.setattr(setup, "like_manager", manager)

    result = asyncio.run(
        setup._send_like(event, Actions(), "简儿", action="赞")
    )

    assert result is True
    assert calls == [{"user_id": 10001, "times": 10}]
    assert manager.count == 10
