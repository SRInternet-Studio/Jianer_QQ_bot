"""Bounded JianerCore adapter operations used by the MaimaiDX plugin."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any, Iterable

from jianer import common, segments

from bot import plugin_state


SUPPORTED_PROTOCOLS = frozenset({"onebot", "milky"})
ACTION_TIMEOUT_SECONDS = 15.0
_QUEUE_POLL_SECONDS = 0.01
_MISSING = object()


def protocol_name(event: Any = None, actions: Any = None) -> str:
    for value in (
        getattr(actions, "protocol", None),
        getattr(event, "protocol", None),
        getattr(plugin_state.get_config(), "protocol", None),
    ):
        if value:
            return str(value).strip().lower()
    return ""


def is_supported(event: Any = None, actions: Any = None) -> bool:
    return protocol_name(event, actions) in SUPPORTED_PROTOCOLS


def prepare_response_queue(actions: Any) -> None:
    """Keep compatibility for host code that still uses ``common.Ret.fetch``."""

    if protocol_name(actions=actions) != "onebot":
        return
    from jianer.LecAdapters.OneBotLib.Manager import reports as onebot_reports

    if common.reports is not onebot_reports:
        common.reports = onebot_reports


def as_message(value: Any) -> common.Message:
    if isinstance(value, common.Message):
        return common.Message(*value.contents)
    if isinstance(value, str):
        return common.Message(segments.Text(value))
    if value is None:
        return common.Message()
    if hasattr(value, "to_message"):
        converted = value.to_message()
        if isinstance(converted, common.Message):
            return common.Message(*converted.contents)
    if hasattr(value, "to_json"):
        return common.Message(value)
    return common.Message(segments.Text(str(value)))


def _numeric_id(actions: Any, value: int | str, field: str) -> int:
    converter = getattr(actions, "_numeric_id", None)
    if callable(converter):
        return int(converter(value, field))
    return int(value)


def _queue_for(protocol: str) -> Any:
    if protocol == "onebot":
        from jianer.LecAdapters.OneBotLib.Manager import reports

        return reports
    if protocol == "milky":
        from jianer.LecAdapters.MilkyLib.Manager import reports

        return reports
    raise RuntimeError(f"unsupported protocol queue: {protocol or 'unknown'}")


def _pop_response(reports: Any, echo: str) -> Any:
    contents = getattr(reports, "contents", None)
    if not isinstance(contents, dict):
        raise RuntimeError("adapter response queue is not consumable")
    return contents.pop(echo, _MISSING)


async def _wait_response(reports: Any, echo: str, timeout: float) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        payload = _pop_response(reports, echo)
        if payload is not _MISSING:
            if not isinstance(payload, dict):
                raise RuntimeError(f"adapter response for {echo} is not an object")
            return payload
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"adapter action timed out waiting for echo {echo}")
        await asyncio.sleep(min(_QUEUE_POLL_SECONDS, remaining))


def _cleanup_late_packet(
    task: asyncio.Task[Any], reports: Any, echo: str
) -> None:
    _pop_response(reports, echo)
    with contextlib.suppress(BaseException):
        task.result()


async def _packet_call(
    actions: Any,
    endpoint: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Send one uniquely correlated action and consume exactly one response."""

    protocol = protocol_name(actions=actions)
    connection = getattr(actions, "connection", None)
    if connection is None:
        raise RuntimeError(f"{protocol or 'adapter'} actions have no connection")
    timeout = ACTION_TIMEOUT_SECONDS if timeout is None else timeout
    if timeout <= 0:
        raise ValueError("action timeout must be positive")

    if protocol == "onebot":
        from jianer.LecAdapters.OneBotLib.Manager import Packet
    elif protocol == "milky":
        from jianer.LecAdapters.MilkyLib.Manager import Packet
    else:
        raise RuntimeError(f"unsupported action protocol: {protocol or 'unknown'}")

    reports = _queue_for(protocol)
    packet = Packet(endpoint, **(params or {}))
    packet.echo = f"maimaidx:{endpoint}:{uuid.uuid4().hex}"
    _pop_response(reports, packet.echo)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    if protocol == "milky":
        send = asyncio.to_thread(
            packet.send_to,
            connection,
            timeout_seconds=max(0.01, timeout * 0.8),
            attempts=1,
        )
    else:
        send = asyncio.to_thread(packet.send_to, connection)
    send_task = asyncio.create_task(send)
    try:
        await asyncio.wait_for(
            asyncio.shield(send_task),
            timeout=max(0.001, deadline - loop.time()),
        )
        payload = await _wait_response(
            reports,
            packet.echo,
            max(0.001, deadline - loop.time()),
        )
    except BaseException:
        if not send_task.done():
            send_task.add_done_callback(
                lambda completed: _cleanup_late_packet(
                    completed, reports, packet.echo
                )
            )
        else:
            _pop_response(reports, packet.echo)
        raise
    finally:
        if send_task.done():
            _pop_response(reports, packet.echo)
    return payload


def _response_data(payload: Any) -> Any:
    if isinstance(payload, common.Ret):
        raw = payload.raw
    else:
        raw = getattr(payload, "raw", payload)
    if isinstance(raw, dict) and "data" in raw:
        return raw["data"]
    return raw


def _require_success(payload: dict[str, Any], endpoint: str) -> None:
    status = payload.get("status")
    retcode = payload.get("retcode")
    if status not in (None, "ok") or retcode not in (None, 0):
        detail = payload.get("message") or payload.get("msg") or payload.get("data")
        raise RuntimeError(f"adapter action {endpoint} failed: {detail!r}")


def _ret(payload: dict[str, Any], serializer: Any) -> common.Ret:
    return common.Ret(payload, serializer)


async def _onebot_send(
    actions: Any,
    outgoing: common.Message,
    *,
    group_id: int | str | None,
    user_id: int | str | None,
) -> Any:
    from jianer.utils.apiresponse import MsgSendRsp

    params: dict[str, Any] = {"message": await outgoing.get()}
    if group_id is not None:
        params["group_id"] = _numeric_id(actions, group_id, "group_id")
    elif user_id is not None:
        params["user_id"] = _numeric_id(actions, user_id, "user_id")
    payload = await _packet_call(actions, "send_msg", params)
    _require_success(payload, "send_msg")
    return _ret(payload, MsgSendRsp)


def _milky_success(actions: Any, payload: Any) -> bool:
    checker = getattr(actions, "_is_successful_response", None)
    if callable(checker):
        return bool(checker(payload))
    return (
        isinstance(payload, dict)
        and payload.get("status") == "ok"
        and payload.get("retcode") == 0
    )


def _milky_payload_rejection(actions: Any, payload: Any) -> bool:
    checker = getattr(actions, "_is_payload_rejection", None)
    if callable(checker):
        return bool(checker(payload))
    return isinstance(payload, dict) and payload.get("retcode") in {
        -500,
        -400,
        400,
        500,
    }


def _milky_text_chunks(
    actions: Any,
    payload: dict[str, Any],
    *,
    keep_reply: bool = True,
    limit: int | None = None,
) -> list[dict[str, Any]] | None:
    splitter = getattr(actions, "_text_chunk_payloads", None)
    if not callable(splitter):
        return None
    kwargs: dict[str, Any] = {"keep_reply": keep_reply}
    if limit is not None:
        kwargs["limit"] = limit
    return splitter(payload, **kwargs)


async def _send_milky_payloads(
    actions: Any,
    endpoint: str,
    payloads: Iterable[dict[str, Any]],
    *,
    interval: float = 0,
) -> tuple[dict[str, Any], int | None]:
    response: dict[str, Any] | None = None
    for index, payload in enumerate(payloads):
        if interval > 0 and index > 0:
            await asyncio.sleep(interval)
        response = await _packet_call(actions, endpoint, payload)
        if not _milky_success(actions, response):
            return response, index
    if response is None:
        raise ValueError("Milky payload list must not be empty")
    return response, None


async def _send_milky_payload(
    actions: Any,
    endpoint: str,
    payload: dict[str, Any],
    *,
    keep_reply: bool = True,
    limit: int | None = None,
    interval: float = 0,
) -> tuple[dict[str, Any], list[dict[str, Any]], int | None]:
    chunks = _milky_text_chunks(
        actions,
        payload,
        keep_reply=keep_reply,
        limit=limit,
    )
    payloads = chunks if chunks is not None else [payload]
    response, failed_index = await _send_milky_payloads(
        actions,
        endpoint,
        payloads,
        interval=interval,
    )
    return response, payloads, failed_index


def _milky_without_reply(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.copy()
    result["message"] = [
        item
        for item in payload.get("message", ())
        if isinstance(item, dict) and item.get("type") != "reply"
    ]
    return result


def _milky_pending_text_payload(
    payloads: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    payload_list = list(payloads)
    if not payload_list:
        return None
    text_parts: list[str] = []
    for payload in payload_list:
        message = payload.get("message")
        if not isinstance(message, list):
            return None
        for item in message:
            if not isinstance(item, dict):
                return None
            if item.get("type") == "reply":
                continue
            data = item.get("data")
            if item.get("type") != "text" or not isinstance(data, dict):
                return None
            text_parts.append(str(data.get("text", "")))
    if not text_parts:
        return None
    result = payload_list[0].copy()
    result["message"] = [
        {"type": "text", "data": {"text": "".join(text_parts)}}
    ]
    return result


async def _milky_send(
    actions: Any,
    outgoing: common.Message,
    *,
    group_id: int | str | None,
    user_id: int | str | None,
) -> Any:
    from jianer.LecAdapters.MilkyLib.translator import msg_enid
    from jianer.utils.apiresponse import MsgSendRsp

    converter = getattr(actions, "_segment_to_outgoing", None)
    if not callable(converter):
        raise RuntimeError("Milky actions do not expose segment conversion")
    milky_message = [converter(item) for item in outgoing]
    if not milky_message:
        raise ValueError("Milky message must not be empty")
    if group_id is not None:
        scene = 1
        peer_id = _numeric_id(actions, group_id, "group_id")
        endpoint = "send_group_message"
        payload = {"group_id": peer_id, "message": milky_message}
    elif user_id is not None:
        scene = 0
        peer_id = _numeric_id(actions, user_id, "user_id")
        endpoint = "send_private_message"
        payload = {"user_id": peer_id, "message": milky_message}
    else:
        raise ValueError("message target is missing")

    response, initial_payloads, failed_index = await _send_milky_payload(
        actions, endpoint, payload
    )
    pending_payloads = (
        []
        if failed_index is None
        else initial_payloads[failed_index:]
    )
    if (
        _milky_payload_rejection(actions, response)
        and pending_payloads
        and any(
            isinstance(item, dict) and item.get("type") == "reply"
            for item in pending_payloads[0].get("message", ())
        )
    ):
        fallback_payloads = [
            _milky_without_reply(item) for item in pending_payloads
        ]
        fallback_response, fallback_failed_index = await _send_milky_payloads(
            actions,
            endpoint,
            fallback_payloads,
        )
        if _milky_success(actions, fallback_response):
            response = fallback_response
            pending_payloads = []
        elif fallback_failed_index is not None:
            pending_payloads = fallback_payloads[fallback_failed_index:]

    if not _milky_success(actions, response):
        full_text_payload = _milky_pending_text_payload(
            [_milky_without_reply(payload)]
        )
        recovery_enabled = (
            full_text_payload is not None
            and _milky_text_chunks(
                actions,
                full_text_payload,
                keep_reply=False,
                limit=400,
            )
            is not None
        )
        pending_text_payload = _milky_pending_text_payload(pending_payloads)
        if recovery_enabled and pending_text_payload is not None:
            recovery_chunks = _milky_text_chunks(
                actions,
                pending_text_payload,
                keep_reply=False,
                limit=400,
            )
            recovery_response, _ = await _send_milky_payloads(
                actions,
                endpoint,
                recovery_chunks or [pending_text_payload],
                interval=0.2,
            )
            if _milky_success(actions, recovery_response):
                response = recovery_response
    _require_success(response, endpoint)
    data = response.get("data")
    if not isinstance(data, dict) or data.get("message_seq") is None:
        raise RuntimeError(f"Milky {endpoint} returned no message_seq")
    data["message_id"] = msg_enid(scene, int(data["message_seq"]), peer_id)
    return _ret(response, MsgSendRsp)


async def send(
    event: Any,
    actions: Any,
    message: Any,
    *,
    reply: bool = True,
    group_id: int | str | None = None,
    user_id: int | str | None = None,
) -> Any:
    outgoing = as_message(message)
    message_id = getattr(event, "message_id", None)
    if reply and message_id is not None:
        outgoing = common.Message(segments.Reply(str(message_id)), *outgoing.contents)

    if group_id is None and user_id is None:
        group_id = getattr(event, "group_id", None)
        if group_id is None:
            user_id = getattr(event, "user_id", None)
    if group_id is None and user_id is None:
        raise ValueError("message event has no group_id or user_id")

    protocol = protocol_name(event, actions)
    if getattr(actions, "connection", None) is not None:
        if protocol == "onebot":
            return await _onebot_send(
                actions, outgoing, group_id=group_id, user_id=user_id
            )
        if protocol == "milky" and callable(
            getattr(actions, "_segment_to_outgoing", None)
        ):
            return await _milky_send(
                actions, outgoing, group_id=group_id, user_id=user_id
            )

    # Lightweight action doubles and future adapters may not expose a native
    # connection. Production OneBot/Milky actions take the bounded path above.
    if group_id is not None:
        return await actions.send(outgoing, group_id=group_id)
    return await actions.send(outgoing, user_id=user_id)


def mentioned_user_id(event: Any) -> int | None:
    target: int | None = None
    self_id = str(getattr(event, "self_id", ""))
    for item in getattr(event, "message", ()) or ():
        if not isinstance(item, segments.At):
            continue
        value = str(item.qq)
        if value.lower() == "all" or value == self_id:
            continue
        try:
            target = int(value)
        except (TypeError, ValueError):
            continue
    return target


def is_superuser(event: Any) -> bool:
    runtime = plugin_state.get_runtime()
    allowed = {
        str(value)
        for key in ("root_users", "super_users")
        for value in runtime.get(key, ())
    }
    return str(getattr(event, "user_id", "")) in allowed


def _member_data(payload: Any) -> dict[str, Any]:
    data = _response_data(payload)
    if not isinstance(data, dict):
        return {}
    member = data.get("member")
    return member if isinstance(member, dict) else data


async def is_group_admin(event: Any, actions: Any) -> bool:
    if is_superuser(event):
        return True
    if getattr(event, "group_id", None) is None:
        return False
    role = str(getattr(getattr(event, "sender", None), "role", "") or "").lower()
    if role in {"owner", "admin"}:
        return True
    try:
        if getattr(actions, "connection", None) is not None and is_supported(
            event, actions
        ):
            payload = await _packet_call(
                actions,
                "get_group_member_info",
                {
                    "group_id": _numeric_id(actions, event.group_id, "group_id"),
                    "user_id": _numeric_id(actions, event.user_id, "user_id"),
                    **(
                        {"no_cache": True}
                        if protocol_name(actions=actions) == "onebot"
                        else {}
                    ),
                },
            )
            _require_success(payload, "get_group_member_info")
        else:
            getter = getattr(actions, "get_group_member_info", None)
            if not callable(getter):
                return False
            payload = await getter(event.group_id, event.user_id)
        return str(_member_data(payload).get("role", "")).lower() in {
            "owner",
            "admin",
        }
    except Exception:
        return False


def _extract_groups(payload: Any) -> list[dict[str, Any]]:
    payload = _response_data(payload)
    if isinstance(payload, dict):
        payload = payload.get("groups", payload.get("group_list", []))
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict) and "group_id" in item]


def _extract_group_members(payload: Any) -> list[dict[str, Any]]:
    payload = _response_data(payload)
    if isinstance(payload, dict):
        payload = payload.get("members", payload.get("member_list", []))
    if not isinstance(payload, list):
        return []
    members: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        member = dict(item)
        if member.get("user_id") is None and member.get("uin") is not None:
            member["user_id"] = member["uin"]
        if member.get("user_id") is not None:
            members.append(member)
    return members


async def get_group_list(actions: Any) -> list[dict[str, Any]]:
    protocol = protocol_name(actions=actions)
    if getattr(actions, "connection", None) is not None and protocol in SUPPORTED_PROTOCOLS:
        payload = await _packet_call(actions, "get_group_list")
        _require_success(payload, "get_group_list")
        return _extract_groups(payload)

    direct = getattr(actions, "get_group_list", None)
    if callable(direct):
        return _extract_groups(await direct())
    custom = getattr(getattr(actions, "custom", None), "get_group_list", None)
    if not callable(custom):
        raise RuntimeError("adapter does not expose get_group_list")
    result = await custom()
    if not isinstance(result, str):
        return _extract_groups(result)
    payload = await _wait_response(
        _queue_for(protocol), result, ACTION_TIMEOUT_SECONDS
    )
    return _extract_groups(payload)


async def get_group_member_list(event: Any, actions: Any) -> list[dict[str, Any]]:
    group_id = getattr(event, "group_id", None)
    if group_id is None:
        return []
    protocol = protocol_name(event, actions)
    if getattr(actions, "connection", None) is not None and protocol in SUPPORTED_PROTOCOLS:
        payload = await _packet_call(
            actions,
            "get_group_member_list",
            {"group_id": _numeric_id(actions, group_id, "group_id")},
        )
        _require_success(payload, "get_group_member_list")
        return _extract_group_members(payload)

    direct = getattr(actions, "get_group_member_list", None)
    if not callable(direct):
        return []
    try:
        payload = await direct(group_id)
    except TypeError:
        payload = await direct(group_id=group_id)
    return _extract_group_members(payload)


async def get_login_info(actions: Any) -> dict[str, Any]:
    protocol = protocol_name(actions=actions)
    if getattr(actions, "connection", None) is not None and protocol in SUPPORTED_PROTOCOLS:
        payload = await _packet_call(actions, "get_login_info")
        _require_success(payload, "get_login_info")
        data = _response_data(payload)
    else:
        getter = getattr(actions, "get_login_info", None)
        if not callable(getter):
            raise RuntimeError("adapter does not expose get_login_info")
        data = _response_data(await getter())
    if not isinstance(data, dict):
        data = getattr(data, "raw", None)
    if not isinstance(data, dict):
        raise RuntimeError("get_login_info returned no object")
    normalized = dict(data)
    if normalized.get("user_id") is None and normalized.get("uin") is not None:
        normalized["user_id"] = normalized["uin"]
    if normalized.get("user_id") is None:
        raise RuntimeError("get_login_info returned no user_id")
    return normalized


async def _milky_forward(
    actions: Any,
    group_id: int | str,
    nodes: list[Any],
) -> Any:
    from jianer.LecAdapters.MilkyLib.translator import (
        MilkyOutGoingSegBuilder,
        msg_enid,
    )
    from jianer.utils.apiresponse import SendGrpForwardRsp

    converter = getattr(actions, "_json_segment_to_outgoing", None)
    if not callable(converter):
        raise RuntimeError("Milky actions do not expose forward conversion")
    outgoing_nodes = []
    for node in nodes:
        data = node.to_json().get("data", {})
        content = data.get("content", [])
        outgoing_nodes.append(
            MilkyOutGoingSegBuilder.outgoing_forward(
                int(data.get("user_id")),
                str(
                    data.get("sender_name")
                    or data.get("nickname")
                    or data.get("nick_name")
                    or data.get("user_id")
                ),
                [converter(item) for item in content],
            )
        )
    numeric_group_id = _numeric_id(actions, group_id, "group_id")
    outgoing = MilkyOutGoingSegBuilder().forward(outgoing_nodes).build()
    payload = await _packet_call(
        actions,
        "send_group_message",
        {"group_id": numeric_group_id, "message": outgoing},
    )
    _require_success(payload, "send_group_message")
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("message_seq") is None:
        raise RuntimeError("Milky forward send returned no message_seq")
    data["message_id"] = msg_enid(1, int(data["message_seq"]), numeric_group_id)
    data.setdefault("forward_id", "")
    return _ret(payload, SendGrpForwardRsp)


async def send_group_forward(
    actions: Any,
    group_id: int | str,
    messages: Iterable[Any],
    *,
    self_id: int | str,
    nickname: str,
) -> Any:
    nodes = [
        segments.CustomNode(str(self_id), nickname, as_message(message))
        for message in messages
    ]
    if not nodes:
        return None
    protocol = protocol_name(actions=actions)
    if getattr(actions, "connection", None) is not None:
        if protocol == "onebot":
            from jianer.utils.apiresponse import SendGrpForwardRsp

            payload = await _packet_call(
                actions,
                "send_group_forward_msg",
                {
                    "group_id": _numeric_id(actions, group_id, "group_id"),
                    "messages": await common.Message(*nodes).get(),
                },
            )
            _require_success(payload, "send_group_forward_msg")
            return _ret(payload, SendGrpForwardRsp)
        if protocol == "milky" and callable(
            getattr(actions, "_json_segment_to_outgoing", None)
        ):
            return await _milky_forward(actions, group_id, nodes)

    sender = getattr(actions, "send_group_forward_msg", None)
    if not callable(sender):
        raise RuntimeError("adapter does not support group forward messages")
    return await sender(group_id, common.Message(*nodes))
