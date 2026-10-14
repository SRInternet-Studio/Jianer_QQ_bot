import asyncio
from types import SimpleNamespace

from plugins.MaimaiDX.core.divingfish_oauth import extract_confirmation_code
from plugins.MaimaiDX.core.merge.models import ServiceName
from plugins.MaimaiDX.core.pending_binding import PendingBindingStore


def test_confirmation_code_normalizes_copied_variants():
    assert extract_confirmation_code("bcdfghjklmnp") == "BCDF-GHJK-LMNP"
    assert extract_confirmation_code("确认码：BCDF-GHJK-LMNP") == "BCDF-GHJK-LMNP"
    assert extract_confirmation_code("这是一句普通聊天") is None
    assert extract_confirmation_code("ABCD-EFGH-IJKL") is None


def test_pending_binding_store_separates_lxns_and_divingfish():
    store = PendingBindingStore()
    store.start(1, 2, ServiceName.LXNS)
    assert store.is_active(1, 2, ServiceName.LXNS)
    assert not store.is_active(1, 2, ServiceName.DIVINGFISH)

    store.start(1, 2, ServiceName.DIVINGFISH)
    assert store.is_active(1, 2, ServiceName.DIVINGFISH)
    assert not store.is_active(1, 2, ServiceName.LXNS)
    assert store.consume(1, 2)
    assert not store.is_active(1, 2)


def test_pending_binding_claim_is_single_flight_and_restorable():
    store = PendingBindingStore()
    state = store.start(1, 2, ServiceName.LXNS)
    claim = store.claim(1, 2, state)
    assert claim is not None
    assert store.is_in_flight(1, 2)
    assert store.claim(1, 2, state) is None
    assert store.release(claim)
    assert not store.is_in_flight(1, 2)
    assert store.claim(1, 2, state) is not None


def test_bind_divingfish_command_returns_authorize_prompt(monkeypatch):
    from plugins.MaimaiDX.commands import base

    sent = []

    monkeypatch.setattr(
        base,
        "dfconfig",
        SimpleNamespace(
            oauth_enabled=True,
            divingfish_auth_url="https://auth.example.invalid",
        ),
    )

    async def fake_send(event, actions, message, **kwargs):
        sent.append(str(message))

    async def fake_user(event, actions, **kwargs):
        return SimpleNamespace(qqid=event.user_id)

    async def fake_bind(qqid):
        assert qqid == 456
        return SimpleNamespace(
            verification_uri_complete="https://auth.example.invalid/device",
            expires_in=1200,
        )

    monkeypatch.setattr(base.adapter, "send", fake_send)
    monkeypatch.setattr(base, "require_user", fake_user)
    monkeypatch.setattr(base, "bind_divingfish", fake_bind)

    event = SimpleNamespace(
        msg_str="绑定水鱼",
        message=[],
        self_id=789,
        user_id=456,
        group_id=None,
    )
    assert asyncio.run(
        base._bind_divingfish("", event, object(), "绑定水鱼")
    ) is True
    assert "https://auth.example.invalid/device" in sent[0]
    assert "QQ 456" in sent[0]
    assert base.pending_bindings.is_active(
        789, 456, ServiceName.DIVINGFISH
    )

