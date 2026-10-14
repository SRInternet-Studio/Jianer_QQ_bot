import asyncio
from types import SimpleNamespace

from jianer import common, segments

from bot import protocol


def _event(text: str, *, message=None, **overrides):
    values = {
        "msg_str": text,
        "message": message or common.Message(segments.Text(text)),
        "message_id": "fidelity-message",
        "group_id": "123",
        "user_id": "111",
        "self_id": "789",
        "protocol": "milky",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_qq_plugin_text_projects_only_text_and_retains_mentions():
    message = common.Message(
        segments.At("789"),
        segments.Text(" 今日舞萌 "),
        segments.At("456"),
    )

    projected = protocol.plugin_message_text(
        SimpleNamespace(protocol="milky"),
        message,
        str(message),
        text_segment_type=segments.Text,
    )

    assert projected == "今日舞萌"
    assert [item.qq for item in message if isinstance(item, segments.At)] == [
        "789",
        "456",
    ]


def test_mentioned_lxns_user_becomes_non_persistent_public_query(monkeypatch):
    from plugins.MaimaiDX.commands import common as command_common
    from plugins.MaimaiDX.core.database.qq import User
    from plugins.MaimaiDX.core.merge.models import ServiceName

    stored = User(
        qqid=456,
        friend_code=123456789,
        access_token="private-access-token",
        refresh_token="private-refresh-token",
        service=ServiceName.LXNS,
    )
    requested = []

    async def fake_get_user(user_id):
        requested.append(user_id)
        return stored

    monkeypatch.setattr(command_common, "get_user", fake_get_user)
    event = _event(
        "b50",
        message=common.Message(
            segments.At("789"),
            segments.Text(" b50 "),
            segments.At("123"),
            segments.At("456"),
        ),
    )

    resolution = asyncio.run(command_common.resolve_user(event, None, check_auth=True))

    assert requested == [456]
    assert resolution.error is None
    assert resolution.user is not stored
    assert resolution.user.qqid == 456
    assert resolution.user.service == ServiceName.DIVINGFISH
    assert resolution.user.friend_code is None
    assert resolution.user.access_token is None
    assert resolution.user.refresh_token is None
    assert stored.service == ServiceName.LXNS
    assert stored.access_token == "private-access-token"


def test_bot_mention_alone_falls_back_to_sender_without_downgrade(monkeypatch):
    from plugins.MaimaiDX.commands import common as command_common
    from plugins.MaimaiDX.core.database.qq import User
    from plugins.MaimaiDX.core.merge.models import ServiceName

    stored = User(
        qqid=111,
        access_token="own-access-token",
        refresh_token="own-refresh-token",
        service=ServiceName.LXNS,
    )

    async def fake_get_user(user_id):
        assert user_id == 111
        return stored

    monkeypatch.setattr(command_common, "get_user", fake_get_user)
    event = _event(
        "b50",
        message=common.Message(segments.At("789"), segments.Text(" b50")),
    )

    resolution = asyncio.run(command_common.resolve_user(event, None, check_auth=True))

    assert resolution.user is stored
    assert resolution.user.service == ServiceName.LXNS
    assert resolution.user.access_token == "own-access-token"


def test_self_mention_may_use_the_senders_own_lxns_token(monkeypatch):
    from plugins.MaimaiDX.commands import common as command_common
    from plugins.MaimaiDX.core.database.qq import User
    from plugins.MaimaiDX.core.merge.models import ServiceName

    stored = User(
        qqid=111,
        access_token="own-access-token",
        refresh_token="own-refresh-token",
        service=ServiceName.LXNS,
    )

    async def fake_get_user(user_id):
        assert user_id == 111
        return stored

    monkeypatch.setattr(command_common, "get_user", fake_get_user)
    event = _event(
        "b50",
        message=common.Message(segments.Text("b50 "), segments.At("111")),
    )

    resolution = asyncio.run(command_common.resolve_user(event, None, check_auth=True))

    assert resolution.user is stored
    assert resolution.user.service == ServiceName.LXNS
    assert resolution.user.access_token == "own-access-token"


def test_command_argument_preserves_digits_that_match_a_mention():
    from plugins.MaimaiDX.commands.common import command_argument

    event = _event(
        "b50 user123456",
        message=common.Message(
            segments.Text("b50 "),
            segments.At("123456"),
            segments.Text("user123456"),
        ),
    )

    assert command_argument(event, "b50") == "user123456"


def test_ap50_rejects_username_before_drawing(monkeypatch):
    from plugins.MaimaiDX.commands import score
    from plugins.MaimaiDX.core.database.qq import User
    from plugins.MaimaiDX.core.merge.models import ServiceName

    sent = []
    drawn = False

    async def ready(*args, **kwargs):
        return True

    async def current_user(*args, **kwargs):
        return User(
            qqid=111,
            access_token="access-token",
            refresh_token="refresh-token",
            service=ServiceName.LXNS,
        )

    async def should_not_draw(*args, **kwargs):
        nonlocal drawn
        drawn = True
        raise AssertionError("AP50 with a username must not query DivingFish B50")

    async def capture_send(event, actions, message, **kwargs):
        sent.append(str(message))

    monkeypatch.setattr(score, "require_resources", ready)
    monkeypatch.setattr(score, "require_user", current_user)
    monkeypatch.setattr(score, "draw_best50", should_not_draw)
    monkeypatch.setattr(score.adapter, "send", capture_send)

    handled = asyncio.run(
        score._best50(
            "public-user",
            _event("ap50 public-user"),
            None,
            all_perfect=True,
        )
    )

    assert handled is True
    assert drawn is False
    assert sent == ["AP50 不支持指定用户名，请直接使用「ap50」查询本人。"]


def test_b50_numeric_argument_queries_that_qq_as_public_waterfish(monkeypatch):
    from plugins.MaimaiDX.commands import score
    from plugins.MaimaiDX.core.database.qq import User
    from plugins.MaimaiDX.core.merge.models import ServiceName

    captured = {}

    async def ready(*args, **kwargs):
        return True

    async def public_user(qqid):
        captured["target"] = qqid
        return User(qqid=qqid, service=ServiceName.DIVINGFISH)

    async def must_not_resolve_current(*args, **kwargs):
        raise AssertionError("an explicit QQ must not load the sender's private binding")

    async def draw(user, *, username=None, all_perfect=False):
        captured["draw"] = (user, username, all_perfect)
        return common.Message(segments.Image("base64://aW1hZ2U="))

    async def send(*args, **kwargs):
        return None

    monkeypatch.setattr(score, "require_resources", ready)
    monkeypatch.setattr(score, "public_divingfish_user", public_user)
    monkeypatch.setattr(score, "require_user", must_not_resolve_current)
    monkeypatch.setattr(score, "draw_best50", draw)
    monkeypatch.setattr(score.adapter, "send", send)

    handled = asyncio.run(
        score._best50(
            "2468013579",
            _event("b50 2468013579"),
            None,
            all_perfect=False,
        )
    )

    assert handled is True
    assert captured["target"] == 2468013579
    user, username, all_perfect = captured["draw"]
    assert user.qqid == 2468013579
    assert username is None
    assert all_perfect is False


def test_b50_mention_takes_precedence_over_public_username_text(monkeypatch):
    from plugins.MaimaiDX.commands import score
    from plugins.MaimaiDX.core.database.qq import User

    captured = {}

    async def ready(*args, **kwargs):
        return True

    async def public_user(qqid):
        captured["target"] = qqid
        return User(qqid=qqid)

    async def draw(user, *, username=None, all_perfect=False):
        captured["username"] = username
        return common.Message(segments.Image("base64://aW1hZ2U="))

    async def send(*args, **kwargs):
        return None

    monkeypatch.setattr(score, "require_resources", ready)
    monkeypatch.setattr(score, "public_divingfish_user", public_user)
    monkeypatch.setattr(score, "draw_best50", draw)
    monkeypatch.setattr(score.adapter, "send", send)

    event = _event(
        "b50 waterfish-name",
        message=common.Message(
            segments.Text("b50 waterfish-name "),
            segments.At("2468013579"),
        ),
    )
    assert asyncio.run(score._best50("waterfish-name", event, None, all_perfect=False))
    assert captured["target"] == 2468013579
    assert captured["username"] is None


def test_rating_table_keeps_upstream_regex_search_semantics(monkeypatch):
    from plugins.MaimaiDX.commands import table

    ratings = []

    async def ready(*args, **kwargs):
        return True

    async def capture_rating(event, actions, rating):
        ratings.append(rating)

    monkeypatch.setattr(table, "require_resources", ready)
    monkeypatch.setattr(table, "_rating_table", capture_rating)

    handled = asyncio.run(
        table.handle_table_patterns(_event("请看13+定数表谢谢"), None)
    )

    assert handled is True
    assert ratings == ["13+"]


def test_score_provider_names_are_user_facing_and_unambiguous():
    from plugins.MaimaiDX.core.merge.models import ServiceName

    assert ServiceName.DIVINGFISH.display_name == "水鱼查分器"
    assert ServiceName.LXNS.display_name == "落雪查分器"
    assert ServiceName.get_help() == "「0」：水鱼查分器\n「1」：落雪查分器"
