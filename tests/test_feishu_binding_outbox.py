import asyncio
import json
from types import SimpleNamespace

import pytest

from bot import feishu_bindings, plugin_state


@pytest.fixture(autouse=True)
def _isolated_binding_file(tmp_path, monkeypatch):
    binding_file = tmp_path / "feishu_bindings.json"
    monkeypatch.setattr(
        feishu_bindings,
        "FEISHU_BIND_FILE",
        str(binding_file),
    )
    with feishu_bindings._INFLIGHT_LOCK:
        feishu_bindings._INFLIGHT_OPERATIONS.clear()
    yield binding_file
    with feishu_bindings._INFLIGHT_LOCK:
        feishu_bindings._INFLIGHT_OPERATIONS.clear()


def test_legacy_json_is_upgraded_with_binding_and_full_outbox_parameters(
    _isolated_binding_file,
):
    _isolated_binding_file.write_text(
        json.dumps({"legacy-open": "10001"}),
        encoding="utf-8",
    )

    assert feishu_bindings.load_feishu_bindings() == {
        "legacy-open": "10001"
    }
    assert feishu_bindings.bind_feishu_user(
        "new-open",
        "20002",
        self_id="feishu-bot",
    )

    state = json.loads(_isolated_binding_file.read_text(encoding="utf-8"))
    assert state["version"] == 2
    assert state["bindings"] == {
        "legacy-open": "10001",
        "new-open": "20002",
    }
    assert state["revisions"] == {
        "legacy-open": 1,
        "new-open": 1,
    }
    assert {
        (item["open_id"], item["qq_id"])
        for item in state["outbox"]
    } == {
        ("legacy-open", "10001"),
        ("new-open", "20002"),
    }
    operation = next(
        item for item in state["outbox"] if item["open_id"] == "new-open"
    )
    assert operation["authorize"] == {
        "protocol": "feishu",
        "self_id": "feishu-bot",
        "external_id": "new-open",
        "canonical_user_id": "qq:20002",
        "reason": "feishu_binding",
    }
    assert operation["merge_identity"] == {
        "source_protocol": "feishu",
        "source_self_id": "feishu-bot",
        "source_external_id": "new-open",
        "target_protocol": "qq",
        "target_self_id": "",
        "target_external_id": "20002",
        "reason": "feishu_binding",
    }


def test_atomic_replace_failure_preserves_previous_authority(
    _isolated_binding_file,
    monkeypatch,
):
    assert feishu_bindings.bind_feishu_user("open", "10001")
    original_bytes = _isolated_binding_file.read_bytes()

    def fail_replace(source, target):
        raise OSError("replace denied")

    monkeypatch.setattr(feishu_bindings.os, "replace", fail_replace)

    assert not feishu_bindings.bind_feishu_user("open", "20002")
    assert _isolated_binding_file.read_bytes() == original_bytes
    assert not list(_isolated_binding_file.parent.glob(".*.tmp"))


def test_malformed_versioned_state_is_not_overwritten(
    _isolated_binding_file,
):
    malformed = b'{"version": 2, "bindings": [], "outbox": []}\n'
    _isolated_binding_file.write_bytes(malformed)

    assert feishu_bindings.load_feishu_bindings() == {}
    assert not feishu_bindings.bind_feishu_user("open", "12345")
    assert _isolated_binding_file.read_bytes() == malformed


def test_pending_reconcile_covers_legacy_authoritative_bindings(
    _isolated_binding_file,
    monkeypatch,
):
    _isolated_binding_file.write_text(
        json.dumps({"legacy-open": "10001"}),
        encoding="utf-8",
    )
    calls = []

    def authorize(**kwargs):
        calls.append(("authorize", kwargs))

    def merge_identity(**kwargs):
        calls.append(("merge_identity", kwargs))

    monkeypatch.setattr(
        plugin_state,
        "get_plugin_module",
        lambda plugin_id: SimpleNamespace(
            authorize=authorize,
            merge_identity=merge_identity,
        ),
    )

    report = asyncio.run(feishu_bindings.reconcile_pending_bindings())

    assert report.attempted == 1
    assert report.succeeded == 1
    assert report.pending == 0
    assert [name for name, _kwargs in calls] == [
        "authorize",
        "merge_identity",
    ]
    state = json.loads(_isolated_binding_file.read_text(encoding="utf-8"))
    assert state["version"] == 2
    assert state["bindings"] == {"legacy-open": "10001"}
    assert state["outbox"] == []


def test_missing_plugin_keeps_successful_binding_in_durable_outbox(
    _isolated_binding_file,
    monkeypatch,
):
    monkeypatch.setattr(plugin_state, "get_plugin_module", lambda plugin_id: None)

    assert feishu_bindings.bind_feishu_user(
        "open",
        "12345",
        self_id="bot",
    )
    assert asyncio.run(
        feishu_bindings.reconcile_feishu_binding(
            "open",
            "12345",
            self_id="bot",
        )
    ) is False

    assert feishu_bindings.get_bound_qq("open") == "12345"
    pending = feishu_bindings.pending_binding_operations()
    assert len(pending) == 1
    assert pending[0]["attempts"] == 1
    assert "_PluginUnavailable" in pending[0]["last_error"]


def test_reconcile_calls_fixed_callbacks_and_acknowledges_outbox(
    _isolated_binding_file,
    monkeypatch,
):
    calls = []

    async def authorize(**kwargs):
        calls.append(("authorize", kwargs))

    async def merge_identity(**kwargs):
        calls.append(("merge_identity", kwargs))

    module = SimpleNamespace(
        authorize=authorize,
        merge_identity=merge_identity,
    )
    monkeypatch.setattr(
        plugin_state,
        "get_plugin_module",
        lambda plugin_id: module,
    )

    assert feishu_bindings.bind_feishu_user(
        "open-id",
        "987654",
        self_id="app-id",
    )
    assert asyncio.run(
        feishu_bindings.reconcile_feishu_binding(
            "open-id",
        )
    )

    assert calls == [
        (
            "authorize",
            {
                "protocol": "feishu",
                "self_id": "app-id",
                "external_id": "open-id",
                "canonical_user_id": "qq:987654",
                "reason": "feishu_binding",
            },
        ),
        (
            "merge_identity",
            {
                "source_protocol": "feishu",
                "source_self_id": "app-id",
                "source_external_id": "open-id",
                "target_protocol": "qq",
                "target_self_id": "",
                "target_external_id": "987654",
                "reason": "feishu_binding",
            },
        ),
    ]
    assert feishu_bindings.pending_binding_operations() == []
    assert feishu_bindings.get_bound_qq("open-id") == "987654"


def test_success_receipt_makes_exact_rebind_and_reconcile_idempotent(
    _isolated_binding_file,
    monkeypatch,
):
    calls = []

    def authorize(**kwargs):
        calls.append(("authorize", kwargs))

    def merge_identity(**kwargs):
        calls.append(("merge_identity", kwargs))

    monkeypatch.setattr(
        plugin_state,
        "get_plugin_module",
        lambda plugin_id: SimpleNamespace(
            authorize=authorize,
            merge_identity=merge_identity,
        ),
    )

    assert feishu_bindings.bind_feishu_user(
        "open",
        "12345",
        self_id="bot",
    )
    assert asyncio.run(feishu_bindings.reconcile_feishu_binding("open"))
    original_state = _isolated_binding_file.read_bytes()

    assert feishu_bindings.bind_feishu_user(
        "open",
        "12345",
        self_id="bot",
    )
    assert asyncio.run(
        feishu_bindings.reconcile_feishu_binding(
            "open",
            "12345",
            self_id="bot",
        )
    )

    assert _isolated_binding_file.read_bytes() == original_state
    assert [name for name, _kwargs in calls] == [
        "authorize",
        "merge_identity",
    ]
    state = json.loads(original_state)
    assert state["outbox"] == []
    assert state["receipts"]["open"]["qq_id"] == "12345"
    assert state["receipts"]["open"]["self_id"] == "bot"


def test_failed_callback_is_retried_without_rolling_back_binding(
    _isolated_binding_file,
    monkeypatch,
):
    attempts = {"authorize": 0, "merge": 0}

    def authorize(**kwargs):
        attempts["authorize"] += 1

    async def merge_identity(**kwargs):
        attempts["merge"] += 1
        if attempts["merge"] == 1:
            raise RuntimeError("database temporarily unavailable")

    monkeypatch.setattr(
        plugin_state,
        "get_plugin_module",
        lambda plugin_id: SimpleNamespace(
            authorize=authorize,
            merge_identity=merge_identity,
        ),
    )

    assert feishu_bindings.bind_feishu_user("open", "12345")
    assert asyncio.run(
        feishu_bindings.reconcile_feishu_binding("open")
    ) is False
    assert feishu_bindings.get_bound_qq("open") == "12345"
    pending = feishu_bindings.pending_binding_operations()
    assert len(pending) == 1
    assert pending[0]["attempts"] == 1

    report = asyncio.run(feishu_bindings.reconcile_pending_bindings())
    assert report.attempted == 1
    assert report.succeeded == 1
    assert report.pending == 0
    assert attempts == {"authorize": 2, "merge": 2}


def test_rebind_while_authorize_is_awaiting_never_merges_old_qq(
    _isolated_binding_file,
    monkeypatch,
):
    old_authorize_started = asyncio.Event()
    allow_old_authorize = asyncio.Event()
    calls = []

    async def authorize(**kwargs):
        calls.append(("authorize", kwargs["canonical_user_id"]))
        if kwargs["canonical_user_id"] == "qq:11111":
            old_authorize_started.set()
            await allow_old_authorize.wait()

    async def merge_identity(**kwargs):
        calls.append(("merge", kwargs["target_external_id"]))

    monkeypatch.setattr(
        plugin_state,
        "get_plugin_module",
        lambda plugin_id: SimpleNamespace(
            authorize=authorize,
            merge_identity=merge_identity,
        ),
    )
    assert feishu_bindings.bind_feishu_user("open", "11111")

    async def scenario():
        old_reconcile = asyncio.create_task(
            feishu_bindings.reconcile_feishu_binding("open", "11111")
        )
        await asyncio.wait_for(old_authorize_started.wait(), timeout=2)
        assert feishu_bindings.bind_feishu_user("open", "22222")
        allow_old_authorize.set()
        assert await old_reconcile is False
        report = await feishu_bindings.reconcile_pending_bindings()
        return report

    report = asyncio.run(scenario())
    assert report.succeeded == 1
    assert calls == [
        ("authorize", "qq:11111"),
        ("authorize", "qq:22222"),
        ("merge", "22222"),
    ]
    assert feishu_bindings.get_bound_qq("open") == "22222"
    assert feishu_bindings.pending_binding_operations() == []


def test_revision_prevents_aba_rebind_from_replaying_old_operation(
    _isolated_binding_file,
    monkeypatch,
):
    calls = []

    def authorize(**kwargs):
        calls.append(("authorize", kwargs["canonical_user_id"]))

    def merge_identity(**kwargs):
        calls.append(("merge", kwargs["target_external_id"]))

    monkeypatch.setattr(
        plugin_state,
        "get_plugin_module",
        lambda plugin_id: SimpleNamespace(
            authorize=authorize,
            merge_identity=merge_identity,
        ),
    )

    assert feishu_bindings.bind_feishu_user("open", "11111")
    old_operation = feishu_bindings.pending_binding_operations()[0]
    assert old_operation["revision"] == 1
    assert feishu_bindings.bind_feishu_user("open", "22222")
    assert feishu_bindings.bind_feishu_user("open", "11111")
    current_operation = feishu_bindings.pending_binding_operations()[0]
    assert current_operation["revision"] == 3
    assert current_operation["operation_id"] != old_operation["operation_id"]

    assert asyncio.run(
        feishu_bindings._reconcile_operation(old_operation)
    ) is feishu_bindings._ReconcileOutcome.SUPERSEDED
    assert calls == []
    report = asyncio.run(feishu_bindings.reconcile_pending_bindings())
    assert report.succeeded == 1
    assert calls == [
        ("authorize", "qq:11111"),
        ("merge", "11111"),
    ]


def test_concurrent_reconcile_is_single_flight(
    _isolated_binding_file,
    monkeypatch,
):
    authorize_started = asyncio.Event()
    allow_authorize = asyncio.Event()
    calls = {"authorize": 0, "merge": 0}

    async def authorize(**kwargs):
        calls["authorize"] += 1
        authorize_started.set()
        await allow_authorize.wait()

    async def merge_identity(**kwargs):
        calls["merge"] += 1

    monkeypatch.setattr(
        plugin_state,
        "get_plugin_module",
        lambda plugin_id: SimpleNamespace(
            authorize=authorize,
            merge_identity=merge_identity,
        ),
    )
    assert feishu_bindings.bind_feishu_user("open", "12345")

    async def scenario():
        first = asyncio.create_task(
            feishu_bindings.reconcile_feishu_binding("open")
        )
        await asyncio.wait_for(authorize_started.wait(), timeout=2)
        report = await feishu_bindings.reconcile_pending_bindings()
        allow_authorize.set()
        return await first, report

    first_result, report = asyncio.run(scenario())
    assert first_result is True
    assert report.attempted == 1
    assert report.succeeded == 0
    assert report.inflight == 1
    assert report.errors == ()
    assert calls == {"authorize": 1, "merge": 1}
    assert feishu_bindings.pending_binding_operations() == []
