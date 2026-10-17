"""JianerCore-action facade exposing the OneBot calls the port relies on."""

from __future__ import annotations

from typing import Any, Mapping

from jianer import common

from .messages import Message

API_TIMEOUT_SECONDS = 30.0

_bots: dict[str, "Bot"] = {}


class ActionFailed(Exception):
    """Raised when the adapter rejects or does not implement an API call."""

    def __init__(self, info: Any = None, **kwargs: Any) -> None:
        payload: dict[str, Any] = {}
        if isinstance(info, dict):
            payload.update(info)
        elif info is not None:
            payload["info"] = info
        payload.update(kwargs)
        self.info = payload
        detail = payload.get("message") or payload.get("msg") or "ActionFailed"
        super().__init__(str(detail))

    @property
    def retcode(self) -> Any:
        return self.info.get("retcode")


def _payload(response: Any) -> Any:
    raw = getattr(response, "raw", response)
    if isinstance(raw, Mapping) and "data" in raw:
        return raw["data"]
    return raw


def _status(response: Any) -> tuple[str, Any, str]:
    raw = getattr(response, "raw", response)
    if isinstance(raw, Mapping):
        return (
            str(raw.get("status") or ""),
            raw.get("retcode"),
            str(raw.get("message") or raw.get("msg") or ""),
        )
    return "", None, ""


def _message_id(response: Any) -> Any:
    raw = getattr(response, "raw", response)
    if isinstance(raw, Mapping):
        data = raw.get("data")
        if isinstance(data, Mapping) and data.get("message_id") is not None:
            return data.get("message_id")
        if raw.get("message_id") is not None:
            return raw.get("message_id")
    data = getattr(response, "data", None)
    return getattr(data, "message_id", None)


def _reports_queue(protocol: str) -> Any:
    if protocol == "milky":
        from jianer.LecAdapters.MilkyLib.Manager import reports

        return reports
    from jianer.LecAdapters.OneBotLib.Manager import reports

    return reports


class Bot:
    """Thin wrapper over one JianerCore connection's ``actions`` object."""

    def __init__(self, self_id: Any, actions: Any = None, *, protocol: str = "onebot") -> None:
        self.self_id = str(self_id)
        self.actions = actions
        self.protocol = protocol

    def attach(self, actions: Any, protocol: str | None = None) -> "Bot":
        self.actions = actions
        if protocol:
            self.protocol = protocol
        return self

    async def call_api(self, api: str, **params: Any) -> Any:
        actions = self.actions
        if actions is None:
            raise ActionFailed({"message": "当前 Bot 没有可用的连接"})

        if api in {"send_group_msg", "send_private_msg", "send_msg"}:
            message = params.get("message")
            if not isinstance(message, common.Message):
                message = Message(message)
            group_id = params.get("group_id")
            user_id = params.get("user_id")
            if api == "send_private_msg" or (group_id is None and user_id is not None):
                response = await actions.send(message=message, user_id=user_id)
            else:
                response = await actions.send(message=message, group_id=group_id)
            return {"message_id": _message_id(response)}

        if api == "send_group_forward_msg":
            nodes = Message(
                [
                    {"type": "node", "data": dict(item.get("data") or item)}
                    for item in (params.get("messages") or [])
                    if isinstance(item, Mapping)
                ]
            )
            response = await actions.send_group_forward_msg(params.get("group_id"), nodes)
            return {"message_id": _message_id(response)}

        if api == "delete_msg":
            await actions.del_message(params.get("message_id"))
            return {}

        if api == "get_group_member_info":
            response = await actions.get_group_member_info(
                params.get("group_id"), params.get("user_id")
            )
            data = _payload(response)
            return data if isinstance(data, Mapping) else {}

        if api == "get_group_info":
            response = await actions.get_group_info(params.get("group_id"))
            data = _payload(response)
            return data if isinstance(data, Mapping) else {}

        custom = getattr(actions, "custom", None)
        caller = getattr(custom, api, None) if custom is not None else None
        if not callable(caller):
            raise ActionFailed(
                {"message": f"当前适配器不支持 {api}", "retcode": 1404}
            )
        echo = await caller(**params)
        raw = await _reports_queue(self.protocol).get_async(echo, API_TIMEOUT_SECONDS)
        status, retcode, message = _status(raw)
        if status and status != "ok":
            raise ActionFailed({"status": status, "retcode": retcode, "message": message})
        payload = _payload(raw)
        return payload

    async def send_group_msg(self, *, group_id: Any, message: Any, **kwargs: Any) -> Any:
        return await self.call_api("send_group_msg", group_id=group_id, message=message)

    async def send_private_msg(self, *, user_id: Any, message: Any, **kwargs: Any) -> Any:
        return await self.call_api("send_private_msg", user_id=user_id, message=message)

    async def send_group_forward_msg(
        self, *, group_id: Any, messages: Any, **kwargs: Any
    ) -> Any:
        return await self.call_api(
            "send_group_forward_msg", group_id=group_id, messages=list(messages)
        )

    async def get_group_member_info(self, *, group_id: Any, user_id: Any, **kwargs: Any) -> Any:
        return await self.call_api(
            "get_group_member_info", group_id=group_id, user_id=user_id
        )

    async def get_group_member_list(self, *, group_id: Any, **kwargs: Any) -> Any:
        return await self.call_api("get_group_member_list", group_id=group_id)

    async def get_group_list(self, **kwargs: Any) -> Any:
        return await self.call_api("get_group_list")

    async def get_msg(self, message_id: Any) -> Any:
        return await self.call_api("get_msg", message_id=message_id)


def protocol_name(actions: Any = None, event: Any = None) -> str:
    for value in (
        getattr(actions, "protocol", None),
        getattr(event, "protocol", None),
    ):
        if value:
            return str(value).strip().lower()
    return ""


def register_bot(bot: Bot) -> Bot:
    _bots[str(bot.self_id)] = bot
    return bot


def get_bot(self_id: Any = None) -> Bot:
    if self_id is None:
        if not _bots:
            raise KeyError("没有已连接的 Bot")
        return next(iter(_bots.values()))
    key = str(self_id)
    if key not in _bots:
        raise KeyError(f"Bot {key} 未连接")
    return _bots[key]


def get_bots() -> dict[str, Bot]:
    return dict(_bots)


def bot_for(actions: Any, self_id: Any) -> Bot:
    """Return (or create) the Bot wrapper for one live connection."""

    protocol = protocol_name(actions=actions) or "onebot"
    key = str(self_id)
    bot = _bots.get(key)
    if bot is None:
        bot = Bot(key, actions, protocol=protocol)
        _bots[key] = bot
    return bot.attach(actions, protocol)


def reset_bots() -> None:
    """Test helper: forget every registered connection."""

    _bots.clear()


__all__ = [
    "ActionFailed",
    "Bot",
    "bot_for",
    "get_bot",
    "get_bots",
    "protocol_name",
    "register_bot",
    "reset_bots",
]
