"""飞书账号与 QQ 身份绑定，以及 JianerAI 合并 outbox。"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

FEISHU_BIND_FILE = "feishu_bindings.json"
JIANER_AI_PLUGIN_ID = "jianerbot-plugin-jianer-ai"

_STATE_VERSION = 2
_STATE_LOCK = threading.RLock()
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT_OPERATIONS: set[str] = set()


class _ReconcileOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    SUPERSEDED = "superseded"
    INFLIGHT = "inflight"
    FAILED = "failed"


@dataclass(frozen=True)
class BindingReconcileReport:
    attempted: int
    succeeded: int
    pending: int
    inflight: int = 0
    superseded: int = 0
    errors: tuple[str, ...] = ()


class _PluginUnavailable(RuntimeError):
    pass


class _CallbackRejected(RuntimeError):
    pass


def load_feishu_bindings() -> dict[str, str]:
    with _STATE_LOCK:
        try:
            return dict(_read_state_unlocked(strict=True)["bindings"])
        except Exception:
            _logger.exception("load feishu bindings failed")
            return {}


def save_feishu_bindings(bindings: dict) -> bool:
    if not isinstance(bindings, dict):
        return False
    normalized = {
        str(key).strip(): str(value).strip()
        for key, value in bindings.items()
        if str(key).strip() and str(value).strip()
    }
    with _STATE_LOCK:
        try:
            state = _read_state_unlocked(strict=True)
            previous = state["bindings"]
            previous_revisions = state["revisions"]
            state["bindings"] = normalized
            next_revisions = dict(previous_revisions)
            for open_id, qq_id in normalized.items():
                next_revisions[open_id] = (
                    max(1, int(previous_revisions.get(open_id, 1)))
                    if previous.get(open_id) == qq_id
                    else max(0, int(previous_revisions.get(open_id, 0))) + 1
                )
            state["revisions"] = next_revisions

            valid_operations = []
            for operation in state["outbox"]:
                open_id = operation["open_id"]
                if (
                    normalized.get(open_id) == operation["qq_id"]
                    and state["revisions"].get(open_id)
                    == operation["revision"]
                ):
                    valid_operations.append(operation)
            state["outbox"] = valid_operations
            state["receipts"] = {
                open_id: receipt
                for open_id, receipt in state["receipts"].items()
                if normalized.get(open_id) == receipt["qq_id"]
                and state["revisions"].get(open_id) == receipt["revision"]
            }

            for open_id, qq_id in normalized.items():
                if previous.get(open_id) != qq_id:
                    state["receipts"].pop(open_id, None)
                    _enqueue_operation_unlocked(
                        state,
                        _make_operation(
                            open_id,
                            qq_id,
                            self_id="",
                            revision=state["revisions"][open_id],
                        ),
                    )
            _write_state_unlocked(state)
            return True
        except Exception:
            _logger.exception("save feishu bindings failed")
            return False


def bind_feishu_user(
    open_id: str,
    qq_id: str,
    self_id: str | int | None = None,
) -> bool:
    """Persist the non-AI binding and its merge operation in one atomic write.

    Binding success never depends on JianerAI availability. Call
    :func:`reconcile_feishu_binding` asynchronously after this function
    returns; failures remain durable in the same JSON authority file.
    """

    open_id = str(open_id or "").strip()
    qq_id = str(qq_id or "").strip()
    if not open_id or not qq_id:
        return False

    with _STATE_LOCK:
        try:
            state = _read_state_unlocked(strict=True)
            previous_qq = state["bindings"].get(open_id)
            previous_revision = int(state["revisions"].get(open_id, 0))
            self_id_text = str(self_id or "")
            if previous_qq == qq_id and _receipt_matches_unlocked(
                state,
                open_id,
                qq_id,
                self_id=self_id_text,
            ):
                return True
            same_pending_operation = any(
                item["open_id"] == open_id
                and item["qq_id"] == qq_id
                and item["revision"] == previous_revision
                and item["authorize"]["self_id"] == self_id_text
                for item in state["outbox"]
            )
            revision = (
                max(1, previous_revision)
                if previous_qq == qq_id and same_pending_operation
                else previous_revision + 1
            )
            state["bindings"][open_id] = qq_id
            state["revisions"][open_id] = revision
            state["receipts"].pop(open_id, None)
            operation = _make_operation(
                open_id,
                qq_id,
                self_id=self_id,
                revision=revision,
            )
            state["outbox"] = [
                item
                for item in state["outbox"]
                if item["open_id"] != open_id
                or item["operation_id"] == operation["operation_id"]
            ]
            _enqueue_operation_unlocked(state, operation)
            _write_state_unlocked(state)
            return True
        except Exception:
            _logger.exception("save feishu binding failed")
            return False


def get_bound_qq(open_id: str) -> str | None:
    bindings = load_feishu_bindings()
    return bindings.get(str(open_id))


def resolve_bound_user_id(protocol: str, user_id: str | int | None) -> str:
    resolved = str(user_id or "").strip()
    if not resolved:
        return ""
    if str(protocol).lower() != "feishu":
        return resolved
    return get_bound_qq(resolved) or resolved


def pending_binding_operations() -> list[dict[str, Any]]:
    with _STATE_LOCK:
        try:
            state = _read_state_unlocked(strict=True)
        except Exception:
            _logger.exception("load feishu binding outbox failed")
            return []
        return copy.deepcopy(state["outbox"])


async def reconcile_feishu_binding(
    open_id: str,
    qq_id: str | None = None,
    *,
    self_id: str | int | None = None,
) -> bool:
    """Apply one authoritative binding to JianerAI, if the plugin is active."""

    open_id = str(open_id or "").strip()
    requested_qq = str(qq_id or "").strip()
    if not open_id:
        return False

    with _STATE_LOCK:
        try:
            state = _read_state_unlocked(strict=True)
            authoritative_qq = state["bindings"].get(open_id, "")
            if not authoritative_qq:
                return False
            if requested_qq and requested_qq != authoritative_qq:
                return False
            matching = [
                item
                for item in state["outbox"]
                if item["open_id"] == open_id
                and item["qq_id"] == authoritative_qq
            ]
            if not matching:
                receipt = state["receipts"].get(open_id)
                if receipt is not None and (
                    self_id is None
                    or receipt["self_id"] == str(self_id or "")
                ):
                    return True
            operation = _make_operation(
                open_id,
                authoritative_qq,
                self_id=self_id,
                revision=state["revisions"][open_id],
            )
            if self_id is None:
                if matching:
                    operation = matching[-1]
            else:
                state["receipts"].pop(open_id, None)
                state["outbox"] = [
                    item
                    for item in state["outbox"]
                    if item["open_id"] != open_id
                    or item["qq_id"] != authoritative_qq
                    or item["operation_id"] == operation["operation_id"]
                ]
            _enqueue_operation_unlocked(state, operation)
            _write_state_unlocked(state)
        except Exception:
            _logger.exception("prepare feishu binding reconciliation failed")
            return False

    return (
        await _reconcile_operation(operation)
        is _ReconcileOutcome.SUCCEEDED
    )


async def reconcile_pending_bindings(
    *,
    limit: int | None = None,
) -> BindingReconcileReport:
    operations = pending_binding_operations()
    if limit is not None:
        operations = operations[: max(0, int(limit))]

    succeeded = 0
    inflight = 0
    superseded = 0
    errors: list[str] = []
    for operation in operations:
        outcome = await _reconcile_operation(operation)
        if outcome is _ReconcileOutcome.SUCCEEDED:
            succeeded += 1
        elif outcome is _ReconcileOutcome.INFLIGHT:
            inflight += 1
        elif outcome is _ReconcileOutcome.SUPERSEDED:
            superseded += 1
        else:
            errors.append(operation["operation_id"])

    return BindingReconcileReport(
        attempted=len(operations),
        succeeded=succeeded,
        pending=len(pending_binding_operations()),
        inflight=inflight,
        superseded=superseded,
        errors=tuple(errors),
    )


async def _reconcile_operation(
    operation: dict[str, Any],
) -> _ReconcileOutcome:
    operation_id = operation["operation_id"]
    with _INFLIGHT_LOCK:
        if operation_id in _INFLIGHT_OPERATIONS:
            return _ReconcileOutcome.INFLIGHT
        _INFLIGHT_OPERATIONS.add(operation_id)

    try:
        try:
            from . import plugin_state

            if not _operation_is_current(operation):
                acknowledged = _acknowledge_operation(
                    operation_id,
                    applied=False,
                )
                return (
                    _ReconcileOutcome.SUPERSEDED
                    if acknowledged
                    else _ReconcileOutcome.FAILED
                )

            plugin = plugin_state.get_plugin_module(JIANER_AI_PLUGIN_ID)
            if plugin is None:
                raise _PluginUnavailable("JianerAI plugin is not active")

            authorize = getattr(plugin, "authorize", None)
            merge_identity = getattr(plugin, "merge_identity", None)
            if not callable(authorize) or not callable(merge_identity):
                raise _PluginUnavailable(
                    "JianerAI binding callbacks are unavailable"
                )

            await _invoke_callback(
                authorize,
                operation["authorize"],
                callback_name="authorize",
            )
            if not _operation_is_current(operation):
                acknowledged = _acknowledge_operation(
                    operation_id,
                    applied=False,
                )
                return (
                    _ReconcileOutcome.SUPERSEDED
                    if acknowledged
                    else _ReconcileOutcome.FAILED
                )
            await _invoke_callback(
                merge_identity,
                operation["merge_identity"],
                callback_name="merge_identity",
            )
            if not _operation_is_current(operation):
                acknowledged = _acknowledge_operation(
                    operation_id,
                    applied=False,
                )
                return (
                    _ReconcileOutcome.SUPERSEDED
                    if acknowledged
                    else _ReconcileOutcome.FAILED
                )
        except Exception as exc:
            _mark_operation_failed(operation_id, exc)
            if not isinstance(exc, _PluginUnavailable):
                _logger.exception(
                    "reconcile feishu binding failed: %s",
                    operation_id,
                )
            return _ReconcileOutcome.FAILED

        acknowledged = _acknowledge_operation(operation_id, applied=True)
        return (
            _ReconcileOutcome.SUCCEEDED
            if acknowledged
            else _ReconcileOutcome.FAILED
        )
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT_OPERATIONS.discard(operation_id)


async def _invoke_callback(
    callback: Any,
    kwargs: dict[str, str],
    *,
    callback_name: str,
) -> None:
    result = callback(**kwargs)
    if inspect.isawaitable(result):
        result = await result
    if result is False:
        raise _CallbackRejected(f"{callback_name} rejected the binding")


def _acknowledge_operation(
    operation_id: str,
    *,
    applied: bool,
) -> bool:
    with _STATE_LOCK:
        try:
            state = _read_state_unlocked(strict=True)
            operation = next(
                (
                    item
                    for item in state["outbox"]
                    if item["operation_id"] == operation_id
                ),
                None,
            )
            if operation is None:
                return not applied
            if applied:
                if not _operation_matches_authority_unlocked(state, operation):
                    return False
                state["receipts"][operation["open_id"]] = _make_receipt(
                    operation
                )
            state["outbox"] = [
                item
                for item in state["outbox"]
                if item["operation_id"] != operation_id
            ]
            _write_state_unlocked(state)
            return True
        except Exception:
            _logger.exception(
                "acknowledge feishu binding outbox failed: %s",
                operation_id,
            )
            return False


def _operation_is_current(operation: dict[str, Any]) -> bool:
    with _STATE_LOCK:
        state = _read_state_unlocked(strict=True)
        if not _operation_matches_authority_unlocked(state, operation):
            return False
        return any(
            item["operation_id"] == operation["operation_id"]
            for item in state["outbox"]
        )


def _operation_matches_authority_unlocked(
    state: dict[str, Any],
    operation: dict[str, Any],
) -> bool:
    return (
        state["bindings"].get(operation["open_id"]) == operation["qq_id"]
        and state["revisions"].get(operation["open_id"])
        == operation["revision"]
    )


def _receipt_matches_unlocked(
    state: dict[str, Any],
    open_id: str,
    qq_id: str,
    *,
    self_id: str,
) -> bool:
    receipt = state["receipts"].get(open_id)
    return bool(
        receipt is not None
        and receipt["qq_id"] == qq_id
        and receipt["self_id"] == self_id
        and receipt["revision"] == state["revisions"].get(open_id)
    )


def _mark_operation_failed(operation_id: str, error: Exception) -> None:
    with _STATE_LOCK:
        try:
            state = _read_state_unlocked(strict=True)
            for operation in state["outbox"]:
                if operation["operation_id"] != operation_id:
                    continue
                operation["attempts"] = int(operation.get("attempts", 0)) + 1
                operation["last_error"] = (
                    f"{type(error).__name__}: {error}"[:500]
                )
                operation["updated_at"] = _utc_now()
                break
            _write_state_unlocked(state)
        except Exception:
            _logger.exception(
                "update feishu binding outbox failed: %s",
                operation_id,
            )


def _make_operation(
    open_id: str,
    qq_id: str,
    *,
    self_id: str | int | None,
    revision: int,
) -> dict[str, Any]:
    self_id_text = str(self_id or "")
    revision_value = max(1, int(revision))
    authorize = {
        "protocol": "feishu",
        "self_id": self_id_text,
        "external_id": open_id,
        "canonical_user_id": f"qq:{qq_id}",
        "reason": "feishu_binding",
    }
    merge_identity = {
        "source_protocol": "feishu",
        "source_self_id": self_id_text,
        "source_external_id": open_id,
        "target_protocol": "qq",
        "target_self_id": "",
        "target_external_id": qq_id,
        "reason": "feishu_binding",
    }
    operation_payload = {
        "revision": revision_value,
        "authorize": authorize,
        "merge_identity": merge_identity,
    }
    operation_id = hashlib.sha256(
        json.dumps(
            operation_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    now = _utc_now()
    return {
        "operation_id": operation_id,
        "open_id": open_id,
        "qq_id": qq_id,
        "revision": revision_value,
        "authorize": authorize,
        "merge_identity": merge_identity,
        "attempts": 0,
        "last_error": "",
        "created_at": now,
        "updated_at": now,
    }


def _make_receipt(
    operation: dict[str, Any],
    *,
    applied_at: str | None = None,
) -> dict[str, Any]:
    return {
        "operation_id": operation["operation_id"],
        "qq_id": operation["qq_id"],
        "self_id": operation["authorize"]["self_id"],
        "revision": operation["revision"],
        "applied_at": applied_at or _utc_now(),
    }


def _enqueue_operation_unlocked(
    state: dict[str, Any],
    operation: dict[str, Any],
) -> None:
    for existing in state["outbox"]:
        if existing["operation_id"] == operation["operation_id"]:
            return
    state["outbox"].append(operation)


def _read_state_unlocked(*, strict: bool) -> dict[str, Any]:
    path = Path(FEISHU_BIND_FILE)
    if not path.exists():
        return _empty_state()
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        return _normalize_state(data)
    except Exception:
        if strict:
            raise
        return _empty_state()


def _normalize_state(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("feishu binding file must contain a JSON object")

    is_current_state = (
        data.get("version") == _STATE_VERSION
        and isinstance(data.get("bindings"), dict)
        and isinstance(data.get("outbox"), list)
    )
    if is_current_state:
        raw_bindings = data["bindings"]
        raw_outbox = data["outbox"]
        raw_revisions = data.get("revisions", {})
        raw_receipts = data.get("receipts", {})
        if not isinstance(raw_revisions, dict):
            raise ValueError("feishu binding revisions must be a JSON object")
        if not isinstance(raw_receipts, dict):
            raise ValueError("feishu binding receipts must be a JSON object")
    else:
        if (
            "version" in data
            or "bindings" in data
            or "revisions" in data
            or "outbox" in data
            or "receipts" in data
        ):
            raise ValueError("unsupported feishu binding state version")
        raw_bindings = data
        raw_outbox = []
        raw_revisions = {}
        raw_receipts = {}

    bindings = {
        str(key).strip(): str(value).strip()
        for key, value in raw_bindings.items()
        if str(key).strip() and str(value).strip()
    }
    revisions: dict[str, int] = {}
    for key, value in raw_revisions.items():
        open_id = str(key).strip()
        if not open_id:
            continue
        revision = int(value)
        if revision < 1:
            raise ValueError("feishu binding revision must be positive")
        revisions[open_id] = revision
    for open_id in bindings:
        revisions.setdefault(open_id, 1)

    if not is_current_state:
        raw_outbox = [
            _make_operation(
                open_id,
                qq_id,
                self_id="",
                revision=revisions[open_id],
            )
            for open_id, qq_id in bindings.items()
        ]
    outbox = []
    seen_operation_ids: set[str] = set()
    for operation in raw_outbox:
        if not isinstance(operation, dict):
            continue
        operation_id = str(operation.get("operation_id", "")).strip()
        open_id = str(operation.get("open_id", "")).strip()
        qq_id = str(operation.get("qq_id", "")).strip()
        authorize = operation.get("authorize")
        merge_identity = operation.get("merge_identity")
        has_revision = "revision" in operation
        operation_revision = int(
            operation.get("revision", revisions.get(open_id, 1))
        )
        if (
            not operation_id
            or not open_id
            or not qq_id
            or not isinstance(authorize, dict)
            or not isinstance(merge_identity, dict)
        ):
            continue
        self_id = str(authorize.get("self_id", ""))
        expected = _make_operation(
            open_id,
            qq_id,
            self_id=self_id,
            revision=operation_revision,
        )
        if (
            (has_revision and operation_id != expected["operation_id"])
            or authorize != expected["authorize"]
            or merge_identity != expected["merge_identity"]
            or bindings.get(open_id) != qq_id
            or revisions.get(open_id) != operation_revision
            or expected["operation_id"] in seen_operation_ids
        ):
            continue
        normalized = expected
        normalized["attempts"] = max(0, int(operation.get("attempts", 0)))
        normalized["last_error"] = str(operation.get("last_error", ""))[:500]
        normalized["created_at"] = str(
            operation.get("created_at", expected["created_at"])
        )
        normalized["updated_at"] = str(
            operation.get("updated_at", expected["updated_at"])
        )
        outbox.append(normalized)
        seen_operation_ids.add(expected["operation_id"])

    receipts: dict[str, dict[str, Any]] = {}
    for key, receipt in raw_receipts.items():
        open_id = str(key).strip()
        if not open_id or not isinstance(receipt, dict):
            continue
        qq_id = str(receipt.get("qq_id", "")).strip()
        self_id = str(receipt.get("self_id", ""))
        try:
            revision = int(receipt.get("revision", 0))
        except (TypeError, ValueError):
            continue
        if (
            not qq_id
            or bindings.get(open_id) != qq_id
            or revisions.get(open_id) != revision
        ):
            continue
        expected = _make_operation(
            open_id,
            qq_id,
            self_id=self_id,
            revision=revision,
        )
        if str(receipt.get("operation_id", "")) != expected["operation_id"]:
            continue
        receipts[open_id] = _make_receipt(
            expected,
            applied_at=str(receipt.get("applied_at", "")) or None,
        )
    return {
        "version": _STATE_VERSION,
        "bindings": bindings,
        "revisions": revisions,
        "outbox": outbox,
        "receipts": receipts,
    }


def _write_state_unlocked(state: dict[str, Any]) -> None:
    path = Path(FEISHU_BIND_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary_path = file.name
            json.dump(
                state,
                file,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


def _empty_state() -> dict[str, Any]:
    return {
        "version": _STATE_VERSION,
        "bindings": {},
        "revisions": {},
        "outbox": [],
        "receipts": {},
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
