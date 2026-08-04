import asyncio
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import Response

from plugins.MaimaiDX.commands import base
from plugins.MaimaiDX.core import handler
from plugins.MaimaiDX.core.clients import http as http_client
from plugins.MaimaiDX.core.clients.lxns import client as lxns_client
from plugins.MaimaiDX.core.clients.lxns.exceptions import LXNSOAuthError
from plugins.MaimaiDX.core.clients.lxns.models import BaseToken, OAuth2Token
from plugins.MaimaiDX.core.lxns_oauth import (
    LXNS_OOB_REDIRECT_URI,
    PendingBindingStore,
    build_authorize_url,
    extract_authorization_response,
)


CODE = "abc_DEF-12345678"


def _event(message: str) -> SimpleNamespace:
    return SimpleNamespace(
        msg_str=message,
        message=[],
        message_id="oauth-security",
        group_id=None,
        user_id=456,
        self_id=789,
        protocol="onebot",
    )


def _token(access: str, refresh: str) -> OAuth2Token:
    return OAuth2Token(
        access_token=access,
        refresh_token=refresh,
        token_type="Bearer",
        expires_in=900,
        scope="read_player",
    )


def test_authorize_url_requests_only_the_player_scope():
    url = build_authorize_url(
        "client-id",
        "https://callback.invalid/",
        state="a" * 43,
    )
    query = parse_qs(urlparse(url).query)
    assert query["scope"] == ["read_player"]
    assert "read_user_profile" not in url


def test_authorize_url_supports_the_official_oob_redirect_uri():
    url = build_authorize_url(
        "client-id",
        LXNS_OOB_REDIRECT_URI,
        state="a" * 43,
    )
    query = parse_qs(urlparse(url).query)

    assert query["redirect_uri"] == [LXNS_OOB_REDIRECT_URI]
    assert query["scope"] == ["read_player"]
    assert query["state"] == ["a" * 43]


def test_oauth_token_response_prefers_standard_top_level_fields(monkeypatch):
    oauth = lxns_client.OAuth2()
    current = _token("top-access", "top-refresh")
    legacy = _token("legacy-access", "legacy-refresh")

    async def request_data(method, endpoint, **kwargs):
        assert method == "POST"
        assert endpoint == "/api/v0/oauth/token"
        return {
            **current.model_dump(),
            "data": legacy.model_dump(),
        }

    monkeypatch.setattr(oauth, "_request_data", request_data)
    result = asyncio.run(oauth.fetch_token(CODE))

    assert result == current
    assert result.access_token == "top-access"


def test_oauth_token_response_temporarily_accepts_legacy_data_wrapper(monkeypatch):
    oauth = lxns_client.OAuth2()
    legacy = _token("legacy-access", "legacy-refresh")

    async def request_data(method, endpoint, **kwargs):
        return {"success": True, "data": legacy.model_dump()}

    monkeypatch.setattr(oauth, "_request_data", request_data)
    result = asyncio.run(oauth.fetch_token(CODE))

    assert result == legacy


def test_oauth_refresh_reads_rotated_top_level_tokens(monkeypatch):
    oauth = lxns_client.OAuth2()
    oauth.token = BaseToken(
        access_token="expired-access",
        refresh_token="old-refresh",
    )
    rotated = _token("new-access", "new-refresh")

    async def request_data(method, endpoint, **kwargs):
        assert method == "POST"
        assert endpoint == "/api/v0/oauth/token"
        body = kwargs["json"]
        assert body["grant_type"] == "refresh_token"
        assert body["refresh_token"] == "old-refresh"
        assert "redirect_uri" not in body
        return rotated.model_dump()

    monkeypatch.setattr(oauth, "_request_data", request_data)
    result = asyncio.run(oauth.refresh_token())

    assert result == rotated
    assert oauth.token.refresh_token == "new-refresh"


def test_oauth_error_uses_standard_flat_error_fields():
    oauth = lxns_client.OAuth2()
    response = Response(
        400,
        json={
            "error": "invalid_grant",
            "error_description": "authorization code expired",
        },
    )

    with pytest.raises(LXNSOAuthError) as captured:
        oauth._handle_error(response)

    assert captured.value.error == "invalid_grant"
    assert captured.value.error_description == "authorization code expired"
    assert "invalid_grant" in str(captured.value)


def test_state_and_malformed_callback_input_are_total_functions():
    store = PendingBindingStore()
    state = store.start(1, 2)

    assert store.matches_state(1, 2, state)
    assert not store.matches_state(1, 2, "é")
    assert not store.matches_state(1, 2, "a" * 10_000)
    assert extract_authorization_response("http://[").code is None


def test_claim_is_atomic_and_old_generation_cannot_delete_new_session():
    store = PendingBindingStore()
    first_state = store.start(1, 2)
    first_claim = store.claim(1, 2, first_state)

    assert first_claim is not None
    assert store.claim(1, 2, first_state) is None

    second_state = store.start(1, 2)
    assert second_state != first_state
    assert not store.complete(first_claim)
    assert not store.release(first_claim)
    assert store.matches_state(1, 2, second_state)

    second_claim = store.claim(1, 2, second_state)
    assert second_claim is not None
    assert store.release(second_claim)
    retry_claim = store.claim(1, 2, second_state)
    assert retry_claim is not None
    assert store.complete(retry_claim)
    assert not store.is_active(1, 2)


def test_oob_claim_without_state_is_atomic_scoped_and_expires():
    now = [100.0]
    store = PendingBindingStore(ttl=10, clock=lambda: now[0])
    store.start(1, 2)

    claim = store.claim_without_state(1, 2)
    assert claim is not None
    assert store.is_in_flight(1, 2)
    assert store.claim_without_state(1, 2) is None
    assert store.claim_without_state(1, 3) is None
    assert store.release(claim)

    retry = store.claim_without_state(1, 2)
    assert retry is not None
    assert store.complete(retry)
    assert not store.is_active(1, 2)

    store.start(1, 2)
    now[0] += 11
    assert store.claim_without_state(1, 2) is None
    assert not store.is_active(1, 2)


def test_oob_bare_code_completes_only_the_private_callers_session(monkeypatch):
    store = PendingBindingStore()
    monkeypatch.setattr(base, "pending_bindings", store)
    monkeypatch.setattr(
        base,
        "lxnsconfig",
        replace(
            base.lxnsconfig,
            redirect_uri=LXNS_OOB_REDIRECT_URI,
            lxns_bind_private_only=True,
        ),
    )

    sent: list[str] = []
    exchanges: list[tuple[int, str]] = []

    async def fake_send(event, actions, message, **kwargs):
        sent.append(str(message))

    async def ready(event, actions):
        return True

    async def sender_user(event, actions, **kwargs):
        assert kwargs.get("allow_mention") is False
        return SimpleNamespace(qqid=event.user_id)

    async def exchange(user, code):
        exchanges.append((user.qqid, code))
        return "授权完成。", True

    monkeypatch.setattr(base.adapter, "send", fake_send)
    monkeypatch.setattr(base, "require_data", ready)
    monkeypatch.setattr(base, "require_user", sender_user)
    monkeypatch.setattr(base, "complete_lxns_binding", exchange)

    store.start(789, 456)
    assert asyncio.run(base.handle_pending_oauth(_event(CODE), object())) is True

    assert exchanges == [(456, CODE)]
    assert sent == ["授权完成。"]
    assert not store.is_active(789, 456)


def test_oob_bare_code_is_rejected_in_group_even_when_group_binding_enabled(
    monkeypatch,
):
    store = PendingBindingStore()
    monkeypatch.setattr(base, "pending_bindings", store)
    monkeypatch.setattr(
        base,
        "lxnsconfig",
        replace(
            base.lxnsconfig,
            redirect_uri=LXNS_OOB_REDIRECT_URI,
            lxns_bind_private_only=False,
        ),
    )
    sent: list[str] = []

    async def fake_send(event, actions, message, **kwargs):
        sent.append(str(message))

    monkeypatch.setattr(base.adapter, "send", fake_send)
    event = _event(CODE)
    event.group_id = 123
    store.start(event.self_id, event.user_id)

    assert asyncio.run(base.handle_pending_oauth(event, object())) is True
    assert sent == [base.OAUTH_OOB_PRIVATE_MSG]
    assert store.is_active(event.self_id, event.user_id)


def test_custom_callback_mode_still_requires_matching_state(monkeypatch):
    store = PendingBindingStore()
    monkeypatch.setattr(base, "pending_bindings", store)
    monkeypatch.setattr(
        base,
        "lxnsconfig",
        replace(base.lxnsconfig, redirect_uri="https://callback.invalid/"),
    )
    sent: list[str] = []

    async def fake_send(event, actions, message, **kwargs):
        sent.append(str(message))

    monkeypatch.setattr(base.adapter, "send", fake_send)
    store.start(789, 456)

    assert asyncio.run(base.handle_pending_oauth(_event(CODE), object())) is True
    assert sent == [base.OAUTH_STATE_MSG]
    assert store.is_active(789, 456)


def test_concurrent_oob_code_enters_token_exchange_only_once(monkeypatch):
    store = PendingBindingStore()
    monkeypatch.setattr(base, "pending_bindings", store)
    monkeypatch.setattr(
        base,
        "lxnsconfig",
        replace(base.lxnsconfig, redirect_uri=LXNS_OOB_REDIRECT_URI),
    )
    sent: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()
    exchange_count = 0

    async def fake_send(event, actions, message, **kwargs):
        sent.append(str(message))

    async def ready(event, actions):
        return True

    async def sender_user(event, actions, **kwargs):
        return SimpleNamespace(qqid=event.user_id)

    async def exchange(user, code):
        nonlocal exchange_count
        exchange_count += 1
        entered.set()
        await release.wait()
        return "授权完成。", True

    monkeypatch.setattr(base.adapter, "send", fake_send)
    monkeypatch.setattr(base, "require_data", ready)
    monkeypatch.setattr(base, "require_user", sender_user)
    monkeypatch.setattr(base, "complete_lxns_binding", exchange)

    async def scenario():
        store.start(789, 456)
        event = _event(CODE)
        first = asyncio.create_task(base.handle_pending_oauth(event, object()))
        await asyncio.wait_for(entered.wait(), timeout=1)
        second = asyncio.create_task(base.handle_pending_oauth(event, object()))
        await asyncio.wait_for(second, timeout=1)
        release.set()
        await asyncio.wait_for(first, timeout=1)

    asyncio.run(scenario())

    assert exchange_count == 1
    assert not store.is_active(789, 456)
    assert any("正在处理中" in message for message in sent)
    assert any("授权完成" in message for message in sent)


def test_concurrent_callback_enters_token_exchange_only_once(monkeypatch):
    store = PendingBindingStore()
    monkeypatch.setattr(base, "pending_bindings", store)

    sent: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()
    exchange_count = 0
    mention_flags: list[bool | None] = []

    async def fake_send(event, actions, message, **kwargs):
        sent.append(str(message))

    async def ready(event, actions):
        return True

    async def sender_user(event, actions, **kwargs):
        mention_flags.append(kwargs.get("allow_mention"))
        return SimpleNamespace(qqid=event.user_id)

    async def exchange(user, code):
        nonlocal exchange_count
        exchange_count += 1
        entered.set()
        await release.wait()
        return "授权完成。", True

    monkeypatch.setattr(base.adapter, "send", fake_send)
    monkeypatch.setattr(base, "require_data", ready)
    monkeypatch.setattr(base, "require_user", sender_user)
    monkeypatch.setattr(base, "complete_lxns_binding", exchange)

    async def scenario():
        state = store.start(789, 456)
        event = _event(
            f"https://callback.invalid/?code={CODE}&state={state}"
        )
        first = asyncio.create_task(base.handle_pending_oauth(event, object()))
        await asyncio.wait_for(entered.wait(), timeout=1)
        second = asyncio.create_task(base.handle_pending_oauth(event, object()))
        await asyncio.wait_for(second, timeout=1)
        release.set()
        await asyncio.wait_for(first, timeout=1)

    asyncio.run(scenario())

    assert exchange_count == 1
    assert mention_flags == [False]
    assert not store.is_active(789, 456)
    assert any("正在处理中" in message for message in sent)
    assert any("授权完成" in message for message in sent)


def test_binding_persists_token_before_optional_friend_code(monkeypatch):
    token = _token("new-access", "new-refresh")
    user = SimpleNamespace(qqid=456)
    order: list[str] = []

    class FakeOAuth:
        async def fetch_token(self, code):
            assert code == CODE
            return token

    async def persist(qqid, **changes):
        assert qqid == user.qqid
        assert changes == {"token": token}
        order.append("persist-token")
        return user

    async def fail_friend_code(qqid, received_token):
        assert qqid == user.qqid
        assert received_token is token
        order.append("fetch-friend-code")
        raise RuntimeError("temporary player endpoint failure")

    monkeypatch.setattr(handler, "OAuth2", FakeOAuth)
    monkeypatch.setattr(handler, "update_user", persist)
    monkeypatch.setattr(handler, "get_friend_code", fail_friend_code)
    monkeypatch.setattr(base, "bind_lxns", handler.bind_lxns)

    message, succeeded = asyncio.run(base.complete_lxns_binding(user, CODE))

    assert order == ["persist-token", "fetch-friend-code"]
    assert succeeded is True
    assert "令牌已保存" in message
    assert "无需重复授权" in message


def test_binding_storage_failure_requires_a_new_authorization_code(monkeypatch):
    token = _token("new-access", "new-refresh")
    user = SimpleNamespace(qqid=456)
    friend_code_called = False

    class FakeOAuth:
        async def fetch_token(self, code):
            return token

    async def fail_persist(qqid, **changes):
        raise RuntimeError("database unavailable")

    async def should_not_fetch_friend_code(*args, **kwargs):
        nonlocal friend_code_called
        friend_code_called = True

    monkeypatch.setattr(handler, "OAuth2", FakeOAuth)
    monkeypatch.setattr(handler, "update_user", fail_persist)
    monkeypatch.setattr(handler, "get_friend_code", should_not_fetch_friend_code)
    monkeypatch.setattr(base, "bind_lxns", handler.bind_lxns)

    message, succeeded = asyncio.run(base.complete_lxns_binding(user, CODE))

    assert succeeded is False
    assert friend_code_called is False
    assert "授权码已经失效" in message
    assert "新的授权码" in message


def test_refresh_is_serialized_and_reuses_the_rotated_database_token(monkeypatch):
    old = BaseToken(access_token="old-access", refresh_token="old-refresh")
    new = _token("new-access", "new-refresh")
    stored = SimpleNamespace(
        qqid=456,
        access_token=old.access_token,
        refresh_token=old.refresh_token,
    )
    refresh_count = 0
    update_count = 0

    async def get_user(qqid):
        assert qqid == stored.qqid
        return stored

    async def update_user(qqid, *, token):
        nonlocal update_count
        update_count += 1
        stored.access_token = token.access_token
        stored.refresh_token = token.refresh_token
        return stored

    class FakeOAuth:
        def __init__(self):
            self.token = None

        async def refresh_token(self):
            nonlocal refresh_count
            refresh_count += 1
            assert self.token.refresh_token == "old-refresh"
            await asyncio.sleep(0)
            return new

    monkeypatch.setattr(lxns_client, "get_user", get_user)
    monkeypatch.setattr(lxns_client, "update_user", update_user)
    monkeypatch.setattr(lxns_client, "OAuth2", FakeOAuth)

    async def scenario():
        first = lxns_client.LxnsClient(
            base_url="https://maimai.lxns.net/test",
            headers={"Authorization": "Bearer old-access"},
            user_id=456,
            token=old,
        )
        second = lxns_client.LxnsClient(
            base_url="https://maimai.lxns.net/test",
            headers={"Authorization": "Bearer old-access"},
            user_id=456,
            token=old,
        )

        results = await asyncio.gather(
            first._on_unauthorized(),
            second._on_unauthorized(),
        )
        return first, second, results

    first, second, results = asyncio.run(scenario())

    assert results == [True, True]
    assert refresh_count == 1
    assert update_count == 1
    assert first.headers["Authorization"] == "Bearer new-access"
    assert second.headers["Authorization"] == "Bearer new-access"


def test_persistent_unauthorized_refreshes_and_retries_only_once(monkeypatch):
    old = BaseToken(access_token="old-access", refresh_token="old-refresh")
    new = _token("new-access", "new-refresh")
    stored = SimpleNamespace(
        qqid=456,
        access_token=old.access_token,
        refresh_token=old.refresh_token,
    )
    request_authorizations: list[str | None] = []
    refresh_count = 0
    update_count = 0

    class FakeResponse:
        status_code = 401

        def json(self):
            return {}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def request(self, method, url, *, headers, **kwargs):
            request_authorizations.append(headers.get("Authorization"))
            return FakeResponse()

    async def get_user(qqid):
        assert qqid == stored.qqid
        return stored

    async def update_user(qqid, *, token):
        nonlocal update_count
        update_count += 1
        stored.access_token = token.access_token
        stored.refresh_token = token.refresh_token
        return stored

    class FakeOAuth:
        def __init__(self):
            self.token = None

        async def refresh_token(self):
            nonlocal refresh_count
            refresh_count += 1
            assert self.token.refresh_token == "old-refresh"
            return new

    monkeypatch.setattr(http_client.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(lxns_client, "get_user", get_user)
    monkeypatch.setattr(lxns_client, "update_user", update_user)
    monkeypatch.setattr(lxns_client, "OAuth2", FakeOAuth)

    async def scenario():
        client = lxns_client.LxnsClient(
            base_url="https://maimai.lxns.net/test",
            headers={"Authorization": "Bearer old-access"},
            user_id=456,
            token=old,
        )
        with pytest.raises(LXNSOAuthError):
            await client._request_base_data("GET", "")

    asyncio.run(scenario())

    assert request_authorizations == [
        "Bearer old-access",
        "Bearer new-access",
    ]
    assert refresh_count == 1
    assert update_count == 1


def test_same_client_concurrent_unauthorized_refreshes_only_once(monkeypatch):
    old = BaseToken(access_token="old-access", refresh_token="old-refresh")
    new = _token("new-access", "new-refresh")
    stored = SimpleNamespace(
        qqid=456,
        access_token=old.access_token,
        refresh_token=old.refresh_token,
    )
    state = SimpleNamespace(old_requests=0, both_old=None)
    request_authorizations: list[str | None] = []
    refresh_count = 0
    update_count = 0

    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code

        def json(self):
            return {}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def request(self, method, url, *, headers, **kwargs):
            authorization = headers.get("Authorization")
            request_authorizations.append(authorization)
            if authorization == "Bearer old-access":
                state.old_requests += 1
                if state.old_requests == 2:
                    state.both_old.set()
                await asyncio.wait_for(state.both_old.wait(), timeout=1)
                return FakeResponse(401)
            assert authorization == "Bearer new-access"
            return FakeResponse(200)

    async def get_user(qqid):
        assert qqid == stored.qqid
        return stored

    async def update_user(qqid, *, token):
        nonlocal update_count
        update_count += 1
        stored.access_token = token.access_token
        stored.refresh_token = token.refresh_token
        return stored

    class FakeOAuth:
        def __init__(self):
            self.token = None

        async def refresh_token(self):
            nonlocal refresh_count
            refresh_count += 1
            assert self.token.refresh_token == "old-refresh"
            await asyncio.sleep(0)
            return new

    monkeypatch.setattr(http_client.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(lxns_client, "get_user", get_user)
    monkeypatch.setattr(lxns_client, "update_user", update_user)
    monkeypatch.setattr(lxns_client, "OAuth2", FakeOAuth)

    async def scenario():
        state.both_old = asyncio.Event()
        client = lxns_client.LxnsClient(
            base_url="https://maimai.lxns.net/test",
            headers={"Authorization": "Bearer old-access"},
            user_id=456,
            token=old,
        )
        return await asyncio.gather(
            client._request_base_data("GET", ""),
            client._request_base_data("GET", ""),
        )

    assert asyncio.run(scenario()) == [{}, {}]
    assert request_authorizations.count("Bearer old-access") == 2
    assert request_authorizations.count("Bearer new-access") == 2
    assert refresh_count == 1
    assert update_count == 1
