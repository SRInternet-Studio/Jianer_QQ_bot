import asyncio
import datetime as dt
import json
import logging
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
from jianer import common, events, network, segments

from bot import plugin_state
from plugins.MaimaiDX import adapter
from plugins.MaimaiDX.runtime import MaimaiRuntime


def _event(**overrides):
    values = {
        "group_id": 100,
        "user_id": 200,
        "self_id": 300,
        "message_id": None,
        "message": [],
        "protocol": "onebot",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_onebot_actions_use_uuid_echo_consume_responses_and_serialize_forward():
    from jianer.LecAdapters.OneBot import Actions
    from jianer.LecAdapters.OneBotLib.Manager import reports

    connection = network.WebsocketConnection("ws://unused")
    seen = []

    def send_packet(raw):
        packet = json.loads(raw)
        seen.append(packet)
        data = {
            "get_group_list": [{"group_id": 100, "group_name": "group"}],
            "send_msg": {"message_id": 10},
            "send_group_forward_msg": {
                "message_id": 11,
                "forward_id": "forward-11",
            },
        }[packet["action"]]
        reports.contents[packet["echo"]] = {
            "status": "ok",
            "retcode": 0,
            "data": data,
            "echo": packet["echo"],
        }

    connection.send = send_packet
    actions = Actions(connection)

    async def scenario():
        groups = await adapter.get_group_list(actions)
        sent = await adapter.send(_event(), actions, "hello", reply=False)
        forwarded = await adapter.send_group_forward(
            actions,
            100,
            ["node"],
            self_id=300,
            nickname="MaimaiDX",
        )
        return groups, sent, forwarded

    groups, sent, forwarded = asyncio.run(scenario())
    echoes = [packet["echo"] for packet in seen]
    assert groups == [{"group_id": 100, "group_name": "group"}]
    assert sent.data.message_id == "10"
    assert forwarded.data.forward_id == "forward-11"
    assert len(set(echoes)) == 3
    assert all(value.startswith("maimaidx:") for value in echoes)
    assert all(value not in reports.contents for value in echoes)


def test_onebot_missing_echo_has_bounded_timeout_and_no_stale_entry(monkeypatch):
    from jianer.LecAdapters.OneBot import Actions
    from jianer.LecAdapters.OneBotLib.Manager import reports

    connection = network.WebsocketConnection("ws://unused")
    sent_echoes = []

    def drop_response(raw):
        sent_echoes.append(json.loads(raw)["echo"])

    connection.send = drop_response
    monkeypatch.setattr(adapter, "ACTION_TIMEOUT_SECONDS", 0.03)
    with pytest.raises(TimeoutError):
        asyncio.run(adapter.get_group_list(Actions(connection)))
    assert sent_echoes
    assert all(value not in reports.contents for value in sent_echoes)


def test_milky_actions_consume_unique_responses_and_normalize_login():
    from jianer.LecAdapters.Milky import Actions
    from jianer.LecAdapters.MilkyLib.Manager import reports
    from jianer.LecAdapters.MilkyLib.translator import MilkyHttpConnection

    connection = MilkyHttpConnection("ws://unused")
    calls = []

    def http_send(endpoint, data, **kwargs):
        calls.append((endpoint, data, kwargs))
        if endpoint == "get_login_info":
            payload = {"uin": 300, "nickname": "bot"}
        elif endpoint == "get_group_list":
            payload = {"groups": [{"group_id": 100, "group_name": "group"}]}
        elif endpoint == "send_group_message":
            payload = {"message_seq": len(calls)}
        else:
            raise AssertionError(endpoint)
        return {"status": "ok", "retcode": 0, "data": payload}

    connection.http_send = http_send
    actions = Actions(connection)

    async def scenario():
        login = await adapter.get_login_info(actions)
        groups = await adapter.get_group_list(actions)
        sent = await adapter.send(_event(protocol="milky"), actions, "ok", reply=False)
        forwarded = await adapter.send_group_forward(
            actions,
            100,
            ["node"],
            self_id=300,
            nickname="MaimaiDX",
        )
        return login, groups, sent, forwarded

    login, groups, sent, forwarded = asyncio.run(scenario())
    assert login["user_id"] == 300
    assert groups[0]["group_id"] == 100
    assert sent.echo.startswith("maimaidx:send_group_message:")
    assert forwarded.echo.startswith("maimaidx:send_group_message:")
    forward_data = calls[-1][1]["message"][0]["data"]
    assert forward_data["title"] == "群聊的聊天记录"
    assert forward_data["preview"] == ["MaimaiDX: node"]
    assert forward_data["summary"] == "查看1条转发消息"
    assert not any(key.startswith("maimaidx:") for key in reports.contents)
    assert all(item[2]["attempts"] == 1 for item in calls)


def test_milky_forward_preview_uses_qq_style_labels_and_single_line_text():
    content = [
        {"type": "text", "data": {"text": "first\nsecond"}},
        {"type": "image", "data": {"file": "base64://ignored"}},
        {"type": "video", "data": {"file": "file://ignored"}},
    ]

    assert adapter._milky_forward_preview(content) == (
        "first second[图片][视频]"
    )


def test_milky_long_text_uses_actions_chunking_but_composite_stays_single():
    from jianer.LecAdapters.Milky import Actions
    from jianer.LecAdapters.MilkyLib.Manager import reports
    from jianer.LecAdapters.MilkyLib.translator import MilkyHttpConnection

    connection = MilkyHttpConnection("ws://unused")
    calls = []

    def http_send(endpoint, data, **kwargs):
        calls.append((endpoint, data, kwargs))
        return {
            "status": "ok",
            "retcode": 0,
            "data": {"message_seq": len(calls)},
        }

    connection.http_send = http_send
    actions = Actions(connection)

    async def scenario():
        await adapter.send(
            _event(protocol="milky", message_id="123"),
            actions,
            "x" * 1801,
        )
        long_text_calls = list(calls)
        calls.clear()
        composite = common.Message(
            segments.Text("y" * 2000),
            segments.Image("base64://aGVsbG8="),
        )
        await adapter.send(
            _event(protocol="milky"),
            actions,
            composite,
            reply=False,
        )
        return long_text_calls, list(calls)

    long_text_calls, composite_calls = asyncio.run(scenario())
    first_segments = long_text_calls[0][1]["message"]
    second_segments = long_text_calls[1][1]["message"]
    assert [item["type"] for item in first_segments] == ["reply", "text"]
    assert len(first_segments[1]["data"]["text"]) == 1800
    assert [item["type"] for item in second_segments] == ["text"]
    assert second_segments[0]["data"]["text"] == "x"
    assert len(composite_calls) == 1
    assert [
        item["type"] for item in composite_calls[0][1]["message"]
    ] == ["text", "image"]
    assert not any(key.startswith("maimaidx:") for key in reports.contents)


def test_milky_reply_payload_rejection_retries_without_reply():
    from jianer.LecAdapters.Milky import Actions
    from jianer.LecAdapters.MilkyLib.Manager import reports
    from jianer.LecAdapters.MilkyLib.translator import MilkyHttpConnection

    connection = MilkyHttpConnection("ws://unused")
    calls = []

    def http_send(endpoint, data, **kwargs):
        calls.append((endpoint, data, kwargs))
        if len(calls) == 1:
            return {"status": "failed", "retcode": 400, "data": None}
        return {
            "status": "ok",
            "retcode": 0,
            "data": {"message_seq": 2},
        }

    connection.http_send = http_send
    actions = Actions(connection)
    asyncio.run(
        adapter.send(
            _event(protocol="milky", message_id="123"),
            actions,
            "reply fallback",
        )
    )
    assert [item["type"] for item in calls[0][1]["message"]] == [
        "reply",
        "text",
    ]
    assert [item["type"] for item in calls[1][1]["message"]] == ["text"]
    assert not any(key.startswith("maimaidx:") for key in reports.contents)


def test_milky_failed_long_text_recovers_in_400_character_chunks():
    from jianer.LecAdapters.Milky import Actions
    from jianer.LecAdapters.MilkyLib.Manager import reports
    from jianer.LecAdapters.MilkyLib.translator import MilkyHttpConnection

    connection = MilkyHttpConnection("ws://unused")
    calls = []

    def http_send(endpoint, data, **kwargs):
        calls.append((endpoint, data, kwargs))
        if len(calls) == 1:
            return {"status": "failed", "retcode": -1, "data": None}
        return {
            "status": "ok",
            "retcode": 0,
            "data": {"message_seq": len(calls)},
        }

    connection.http_send = http_send
    actions = Actions(connection)
    asyncio.run(
        adapter.send(
            _event(protocol="milky"),
            actions,
            "z" * 1000,
            reply=False,
        )
    )
    lengths = [
        len(call[1]["message"][0]["data"]["text"])
        for call in calls
    ]
    assert lengths == [1000, 400, 400, 200]
    assert all(call[2]["attempts"] == 1 for call in calls)
    assert not any(key.startswith("maimaidx:") for key in reports.contents)


def test_milky_partial_chunk_failure_recovers_only_unsent_tail():
    from jianer.LecAdapters.Milky import Actions
    from jianer.LecAdapters.MilkyLib.Manager import reports
    from jianer.LecAdapters.MilkyLib.translator import MilkyHttpConnection

    connection = MilkyHttpConnection("ws://unused")
    calls = []
    prefix = "p" * 1800
    tail = "TAIL" * 50
    original = prefix + tail

    def http_send(endpoint, data, **kwargs):
        calls.append((endpoint, data, kwargs))
        if len(calls) == 2:
            return {"status": "failed", "retcode": 400, "data": None}
        return {
            "status": "ok",
            "retcode": 0,
            "data": {"message_seq": len(calls)},
        }

    connection.http_send = http_send
    actions = Actions(connection)
    asyncio.run(
        adapter.send(
            _event(protocol="milky", message_id="123"),
            actions,
            original,
        )
    )

    messages = [call[1]["message"] for call in calls]
    assert len(messages) == 3
    assert [item["type"] for item in messages[0]] == ["reply", "text"]
    assert [item["type"] for item in messages[1]] == ["text"]
    assert [item["type"] for item in messages[2]] == ["text"]
    assert messages[0][1]["data"]["text"] == prefix
    assert messages[1][0]["data"]["text"] == tail
    assert messages[2][0]["data"]["text"] == tail
    successful_text = (
        messages[0][1]["data"]["text"]
        + messages[2][0]["data"]["text"]
    )
    assert successful_text == original
    assert sum(
        item["type"] == "reply"
        for message in messages
        for item in message
    ) == 1
    assert not any(key.startswith("maimaidx:") for key in reports.contents)


def test_mentioned_user_ignores_all_and_self_then_returns_last_target():
    event = _event(
        message=[
            segments.At("all"),
            segments.At("300"),
            segments.At("400"),
            segments.At("invalid"),
            segments.At("500"),
        ]
    )
    assert adapter.mentioned_user_id(event) == 500


def test_start_resolves_login_identity_before_background_tasks(monkeypatch):
    runtime = MaimaiRuntime()
    actions = SimpleNamespace(protocol="milky")
    order = []

    async def login(_actions):
        order.append("login")
        return {"user_id": 300}

    async def initialize(*, force=False):
        order.append("initialize")
        runtime.data_ready = True
        return True

    def start_tasks():
        order.append(f"tasks:{runtime.self_id}")

    monkeypatch.setattr(adapter, "get_login_info", login)
    monkeypatch.setattr(runtime, "initialize", initialize)
    monkeypatch.setattr(runtime, "_start_background_tasks", start_tasks)
    asyncio.run(
        runtime.start(
            SimpleNamespace(protocol="milky", self_id=None), actions
        )
    )
    assert order == ["login", "initialize", "tasks:300"]


def test_refresh_callers_share_one_inflight_update(monkeypatch):
    runtime = MaimaiRuntime()

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def refresh_data():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            runtime.data_ready = True
            return True

        monkeypatch.setattr(runtime, "_refresh_data", refresh_data)
        first = asyncio.create_task(runtime.initialize(force=True))
        await started.wait()
        second = asyncio.create_task(runtime.initialize(force=True))
        await asyncio.sleep(0)
        release.set()
        assert await asyncio.gather(first, second) == [True, True]
        return calls

    assert asyncio.run(scenario()) == 1


def test_forced_refresh_restores_background_tasks(monkeypatch):
    runtime = MaimaiRuntime()
    runtime.actions = SimpleNamespace(protocol="onebot")
    started = []

    async def refresh_data():
        runtime.data_ready = True
        return True

    monkeypatch.setattr(runtime, "_refresh_data", refresh_data)
    monkeypatch.setattr(runtime, "_start_background_tasks", lambda: started.append(1))
    assert asyncio.run(runtime.initialize(force=True)) is True
    assert started == [1]


def test_readiness_error_does_not_expose_internal_details(monkeypatch):
    runtime = MaimaiRuntime()
    runtime.initialization_error = (
        "ConnectError: https://secret.invalid at D:\\private\\state.json"
    )
    messages = []

    async def initialize(*, force=False):
        return False

    async def capture(event, actions, message, **kwargs):
        messages.append(str(message))

    monkeypatch.setattr(runtime, "initialize", initialize)
    monkeypatch.setattr(adapter, "send", capture)
    assert (
        asyncio.run(
            runtime.ensure_data(
                _event(), SimpleNamespace(protocol="onebot")
            )
        )
        is False
    )
    assert messages == [
        "MaimaiDX 数据暂未就绪，请稍后重试；"
        "若持续失败，请联系管理员查看日志。"
    ]
    assert "secret.invalid" not in messages[0]
    assert "D:\\private" not in messages[0]


def test_resource_error_does_not_expose_absolute_static_path(monkeypatch):
    import importlib

    runtime_module = importlib.import_module("plugins.MaimaiDX.runtime")
    runtime = MaimaiRuntime()
    messages = []

    async def ready(event, actions):
        return True

    async def capture(event, actions, message, **kwargs):
        messages.append(str(message))

    monkeypatch.setattr(runtime, "ensure_data", ready)
    monkeypatch.setattr(
        runtime_module,
        "resource_issues",
        lambda: ("D:\\private\\maimaidx\\static\\missing.png",),
    )
    monkeypatch.setattr(adapter, "send", capture)
    assert (
        asyncio.run(
            runtime.ensure_resources(
                _event(), SimpleNamespace(protocol="onebot")
            )
        )
        is False
    )
    assert messages == [
        "MaimaiDX 静态资源不完整，请联系管理员查看日志并补齐资源。"
    ]
    assert "D:\\private" not in messages[0]


def test_refresh_resets_divingfish_origin_and_rejects_token_proxy(
    tmp_path, monkeypatch
):
    import importlib

    runtime_module = importlib.import_module("plugins.MaimaiDX.runtime")
    database = importlib.import_module("plugins.MaimaiDX.core.database.qq")
    service = importlib.import_module("plugins.MaimaiDX.core.service")
    client = importlib.import_module(
        "plugins.MaimaiDX.core.clients.divingfish.client"
    )
    runtime = MaimaiRuntime()
    order = []
    warnings = []

    async def create_database():
        order.append("database")

    async def music():
        order.append("music")

    async def aliases():
        order.append("aliases")

    async def plates():
        order.append("plates")

    def reset_origin(cls):
        order.append("origin")

    def reject_proxy(cls):
        order.append("proxy")
        return False

    monkeypatch.setattr(runtime_module, "ensure_runtime_dirs", lambda: None)
    monkeypatch.setattr(runtime_module, "resource_issues", lambda: ())
    monkeypatch.setattr(runtime_module, "rating_table_dir", tmp_path)
    monkeypatch.setattr(runtime_module, "plate_table_dir", tmp_path)
    monkeypatch.setattr(
        runtime_module,
        "dfconfig",
        SimpleNamespace(divingfish_prober_proxy=True),
    )
    monkeypatch.setattr(
        runtime_module,
        "maiconfig",
        SimpleNamespace(save_in_memory=False),
    )
    monkeypatch.setattr(
        runtime_module,
        "log",
        SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda message, *args, **kwargs: warnings.append(message),
            exception=lambda *args, **kwargs: None,
        ),
    )
    monkeypatch.setattr(database, "create_database", create_database)
    monkeypatch.setattr(service.mai, "get_music", music)
    monkeypatch.setattr(service.mai, "get_music_alias", aliases)
    monkeypatch.setattr(service.mai, "get_plate_json", plates)
    monkeypatch.setattr(service.guess, "guess", lambda: order.append("guess"))
    monkeypatch.setattr(
        client.DivingFishAPI,
        "reset_origin",
        classmethod(reset_origin),
    )
    monkeypatch.setattr(
        client.DivingFishAPI,
        "set_proxy",
        classmethod(reject_proxy),
    )

    assert asyncio.run(runtime.initialize()) is True
    assert order == [
        "database",
        "origin",
        "proxy",
        "music",
        "aliases",
        "plates",
        "guess",
    ]
    assert "存在水鱼查分器开发者 Token，已拒绝第三方代理并直连官方" in warnings


def test_next_daily_update_uses_same_day_before_four_and_tomorrow_after():
    timezone = dt.timezone(dt.timedelta(hours=8))
    before = dt.datetime(2026, 8, 4, 2, 0, tzinfo=timezone)
    after = dt.datetime(2026, 8, 4, 5, 0, tzinfo=timezone)
    assert MaimaiRuntime.next_daily_update(before) == dt.datetime(
        2026, 8, 4, 4, 0, tzinfo=timezone
    )
    assert MaimaiRuntime.next_daily_update(after) == dt.datetime(
        2026, 8, 5, 4, 0, tzinfo=timezone
    )


def test_hot_reload_replays_latest_listener_start(tmp_path, monkeypatch):
    asyncio.run(plugin_state.shutdown_plugins())
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    plugin_file = plugin_dir / "lifecycle.py"

    def write(marker):
        plugin_file.write_text(
            textwrap.dedent(
                f"""
                from jianer import events
                from jianer.plugins import PluginMetadata

                __plugin_meta__ = PluginMetadata(name="jianerbot-plugin-replay-test")

                async def started(event, actions):
                    actions.calls.append("{marker}")

                def setup(client, manager):
                    client.subscribe(started, events.HyperListenerStartNotify)
                """
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(plugin_state, "PLUGIN_FOLDER", str(plugin_dir))
    plugin_state.configure(
        config=SimpleNamespace(),
        logger=logging.getLogger("maimaidx-replay-test"),
        reminder="~",
        bot_name="bot",
        bot_name_en="bot",
        one_slogan="test",
        confused_word="test",
        root_users=[],
        cooldowns={},
        cooldowns1={},
    )
    try:
        write("old")
        plugin_state.reload_plugins()
        actions = SimpleNamespace(calls=[])
        event = events.HyperListenerStartNotify(0, "milky")
        asyncio.run(plugin_state.dispatch_subscriptions(event, actions))
        write("new-generation")
        asyncio.run(plugin_state.reload_plugins_async())
        assert actions.calls == ["old", "new-generation"]
    finally:
        asyncio.run(plugin_state.shutdown_plugins())


def test_alias_push_failure_does_not_reconnect_websocket(monkeypatch):
    from plugins.MaimaiDX.core import alias_ws_push

    stop_event = asyncio.Event()
    received = 0
    push_calls = 0
    info_logs = []
    payload = json.dumps(
        {
            "type": "Approved",
            "status": [
                {
                    "song_id": 1,
                    "apply_uid": 2,
                    "apply_alias": "alias",
                    "tag": "tag",
                    "name": "song",
                    "group_id": 100,
                }
            ],
        }
    )

    class Socket:
        async def receive_text(self):
            nonlocal received
            received += 1
            return payload

    class SocketContext:
        async def __aenter__(self):
            return Socket()

        async def __aexit__(self, *args):
            return False

    def connect(*args, **kwargs):
        return SocketContext()

    async def push(*args, **kwargs):
        nonlocal push_calls
        push_calls += 1
        if push_calls == 1:
            raise RuntimeError("one push failed")
        stop_event.set()

    monkeypatch.setattr(alias_ws_push, "aconnect_ws", connect)
    monkeypatch.setattr(alias_ws_push, "push_alias", push)
    monkeypatch.setattr(
        alias_ws_push,
        "log",
        SimpleNamespace(
            success=lambda *args, **kwargs: None,
            info=lambda message, *args, **kwargs: info_logs.append(message),
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
            exception=lambda *args, **kwargs: None,
        ),
    )
    asyncio.run(
        alias_ws_push.ws_alias_server(
            lambda: (SimpleNamespace(), 300), stop_event
        )
    )
    assert push_calls == 2
    assert info_logs.count(
        "收到Yuri-YuzuChaN别名实时事件：type=Approved, items=1"
    ) == 2
    assert received == 2


def test_alias_network_error_is_unwrapped_inside_websocket_context(monkeypatch):
    from httpx_ws import WebSocketNetworkError

    from plugins.MaimaiDX.core import alias_ws_push

    stop_event = asyncio.Event()
    context_exit_exceptions = []
    warnings = []
    info_logs = []

    class Socket:
        async def receive_text(self):
            raise WebSocketNetworkError

    class SocketContext:
        async def __aenter__(self):
            return Socket()

        async def __aexit__(self, exc_type, exc, traceback):
            context_exit_exceptions.append(exc)
            if exc is not None:
                raise ExceptionGroup("task group wrapper", [exc])
            return False

    async def stop_during_retry(event, seconds):
        assert seconds == alias_ws_push.INITIAL_RECONNECT_SECONDS
        event.set()
        return True

    monkeypatch.setattr(
        alias_ws_push,
        "aconnect_ws",
        lambda *args, **kwargs: SocketContext(),
    )
    monkeypatch.setattr(alias_ws_push, "_wait_or_stop", stop_during_retry)
    monkeypatch.setattr(
        alias_ws_push,
        "log",
        SimpleNamespace(
            success=lambda *args, **kwargs: None,
            info=lambda message, *args, **kwargs: info_logs.append(message),
            warning=lambda message, *args, **kwargs: warnings.append(message),
            error=lambda *args, **kwargs: None,
            exception=lambda *args, **kwargs: None,
        ),
    )

    asyncio.run(
        alias_ws_push.ws_alias_server(
            lambda: (SimpleNamespace(), 300), stop_event
        )
    )

    assert context_exit_exceptions == [None]
    assert warnings == ["别名推送连接断开: WebSocketNetworkError"]
    assert "ExceptionGroup" not in "".join(warnings)
    assert info_logs[-1] == "别名推送将在 5 秒后重连"


def test_alias_exception_group_logs_leaf_cause_and_backs_off(monkeypatch):
    from httpx_ws import WebSocketNetworkError

    from plugins.MaimaiDX.core import alias_ws_push

    stop_event = asyncio.Event()
    delays = []
    warnings = []

    class FailingContext:
        async def __aenter__(self):
            raise ExceptionGroup(
                "unhandled errors in a TaskGroup",
                [WebSocketNetworkError()],
            )

        async def __aexit__(self, *args):
            return False

    async def record_retry(event, seconds):
        delays.append(seconds)
        if len(delays) == 3:
            event.set()
            return True
        return False

    monkeypatch.setattr(
        alias_ws_push,
        "aconnect_ws",
        lambda *args, **kwargs: FailingContext(),
    )
    monkeypatch.setattr(alias_ws_push, "_wait_or_stop", record_retry)
    monkeypatch.setattr(
        alias_ws_push,
        "log",
        SimpleNamespace(
            success=lambda *args, **kwargs: None,
            info=lambda *args, **kwargs: None,
            warning=lambda message, *args, **kwargs: warnings.append(message),
            error=lambda *args, **kwargs: None,
            exception=lambda *args, **kwargs: None,
        ),
    )

    asyncio.run(
        alias_ws_push.ws_alias_server(
            lambda: (SimpleNamespace(), 300), stop_event
        )
    )

    assert delays == [5.0, 10.0, 20.0]
    assert warnings == [
        "别名推送连接断开: WebSocketNetworkError",
        "别名推送连接断开: WebSocketNetworkError",
        "别名推送连接断开: WebSocketNetworkError",
    ]
    assert all("ExceptionGroup" not in item for item in warnings)


def test_alias_push_rejects_empty_batch_before_group_lookup(monkeypatch):
    from plugins.MaimaiDX.core import alias_ws_push

    warnings = []

    async def should_not_get_groups(actions):
        raise AssertionError("empty alias event must not query or send to groups")

    monkeypatch.setattr(alias_ws_push.adapter, "get_group_list", should_not_get_groups)
    monkeypatch.setattr(
        alias_ws_push,
        "log",
        SimpleNamespace(warning=lambda message: warnings.append(message)),
    )
    asyncio.run(
        alias_ws_push.push_alias(
            SimpleNamespace(type="Unknown", status=[]),
            SimpleNamespace(),
            self_id=300,
        )
    )

    assert warnings == [
        "Yuri-YuzuChaN别名实时事件状态为空：type=Unknown"
    ]


def test_alias_push_rejects_empty_direct_event_before_indexing(monkeypatch):
    from plugins.MaimaiDX.core import alias_ws_push

    warnings = []

    async def should_not_send(*args, **kwargs):
        raise AssertionError("empty direct alias event must not send")

    monkeypatch.setattr(alias_ws_push.adapter, "send", should_not_send)
    monkeypatch.setattr(
        alias_ws_push,
        "log",
        SimpleNamespace(warning=lambda message: warnings.append(message)),
    )
    asyncio.run(
        alias_ws_push.push_alias(
            SimpleNamespace(type="Approved", status=[]),
            SimpleNamespace(),
            self_id=300,
        )
    )

    assert warnings == [
        "Yuri-YuzuChaN别名实时事件状态为空：type=Approved"
    ]
