import asyncio
import base64
import hashlib
import hmac
import json
import queue
import random
import threading
import time
from typing import Union, NoReturn

import flask
import httpx

from .. import common, configurator, events, hyperogger, segments
from ..events import Event, HyperNotify, HyperListenerStartNotify
from ..utils import errors
from ..utils.apiresponse import *
from .OneBotLib.Manager import reports

config = configurator.BotConfig.get("hyper-bot")
logger = hyperogger.Logger()
logger.set_level(config.log_level)
listener_ran = False
handler: callable = None

_event_queue = queue.Queue()
_running = False
_app = None
_app_thread = None
_listener_started = False
_id_lock = threading.Lock()
_user_map_raw_to_int: dict[str, int] = {}
_user_map_int_to_raw: dict[int, str] = {}
_user_name_map: dict[int, str] = {}
_chat_map_raw_to_int: dict[str, int] = {}
_chat_map_int_to_raw: dict[int, str] = {}
_message_map_int_to_raw: dict[int, str] = {}
_message_counter = 0
_token_cache = {"value": "", "expire_at": 0}
_ws_client = None
_ws_thread = None


def _mk_int_id(prefix: str, raw_id: str) -> int:
    digest = hashlib.md5(f"{prefix}:{raw_id}".encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def _map_user(raw_id: str, name: str = "") -> int:
    with _id_lock:
        if raw_id not in _user_map_raw_to_int:
            mapped = _mk_int_id("u", raw_id)
            _user_map_raw_to_int[raw_id] = mapped
            _user_map_int_to_raw[mapped] = raw_id
        mapped = _user_map_raw_to_int[raw_id]
        if name:
            _user_name_map[mapped] = str(name)
        return mapped


def _map_chat(raw_id: str) -> int:
    with _id_lock:
        if raw_id not in _chat_map_raw_to_int:
            mapped = _mk_int_id("c", raw_id)
            _chat_map_raw_to_int[raw_id] = mapped
            _chat_map_int_to_raw[mapped] = raw_id
        return _chat_map_raw_to_int[raw_id]


def _next_msg_id(raw_msg_id: str = "") -> int:
    global _message_counter
    with _id_lock:
        _message_counter += 1
        local_idx = _message_counter
    msg_id = int(time.time() * 1000) * 1000 + (local_idx % 1000)
    if raw_msg_id:
        _message_map_int_to_raw[msg_id] = raw_msg_id
    return msg_id


def _normalize_event_mode() -> str:
    mode = str(config.feishu.get("event_mode", "long_connection") or "long_connection").strip().lower()
    if mode not in ("long_connection", "webhook"):
        return "long_connection"
    return mode


def _put_ret(endpoint: str, data=None, status: str = "ok", retcode: int = 0, message: str = "") -> str:
    echo = f"{endpoint}_{random.randint(1000, 9999)}"
    reports.put(echo, {
        "status": status,
        "retcode": retcode,
        "data": data if data is not None else {},
        "message": message,
        "echo": echo
    })
    return echo


def _get_feishu_token() -> str:
    now = int(time.time())
    if _token_cache["value"] and _token_cache["expire_at"] > now + 30:
        return _token_cache["value"]
    body = {
        "app_id": config.feishu.get("app_id", ""),
        "app_secret": config.feishu.get("app_secret", "")
    }
    resp = httpx.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json=body,
        timeout=10.0
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取飞书 tenant_access_token 失败: {data.get('msg')}")
    token = data.get("tenant_access_token", "")
    expire = int(data.get("expire", 7200) or 7200)
    _token_cache["value"] = token
    _token_cache["expire_at"] = now + expire
    return token


def _build_text_from_message(message: Union[common.Message, str]) -> str:
    if isinstance(message, str):
        return message
    parts = []
    for seg in message:
        if isinstance(seg, segments.Text):
            parts.append(seg.text)
        elif isinstance(seg, segments.At):
            parts.append(f"@{seg.qq}")
        elif isinstance(seg, segments.Image):
            parts.append("[图片]")
        elif isinstance(seg, segments.Video):
            parts.append("[视频]")
        elif isinstance(seg, segments.Record):
            continue
        elif isinstance(seg, segments.Reply):
            continue
        else:
            parts.append(f"[{seg.__class__.__name__}]")
    return "".join(parts).strip()


def _segments_json_to_text(segments_json: list) -> str:
    if not isinstance(segments_json, list):
        return ""
    parts = []
    for item in segments_json:
        if not isinstance(item, dict):
            continue
        seg_type = str(item.get("type") or "")
        seg_data = item.get("data") or {}
        if seg_type == "text":
            parts.append(str(seg_data.get("text", "") or ""))
        elif seg_type == "at":
            parts.append(f"@{seg_data.get('qq', '')}")
        elif seg_type == "image":
            parts.append("[图片]")
        elif seg_type == "video":
            parts.append("[视频]")
    return "".join(parts).strip()


def _extract_forward_texts(message: common.Message) -> list[str]:
    texts = []
    for seg in message:
        try:
            seg_json = seg.to_json()
        except Exception:
            seg_json = None
        if isinstance(seg_json, dict) and seg_json.get("type") == "node":
            data = seg_json.get("data") or {}
            content = data.get("content")
            text = _segments_json_to_text(content)
            if text:
                texts.append(text)
            continue
        text = str(seg).strip()
        if text:
            texts.append(text)
    return texts


def _verify_token(payload: dict) -> bool:
    expected = str(config.feishu.get("verification_token", "") or "")
    if not expected:
        return True
    token = str(payload.get("token", "") or "")
    return token == expected


def _verify_signature(payload: dict, headers: dict) -> bool:
    encrypt_key = str(config.feishu.get("encrypt_key", "") or "")
    if not encrypt_key:
        return True
    encrypted = payload.get("encrypt")
    if not encrypted:
        return True
    timestamp = str(headers.get("X-Lark-Request-Timestamp", "") or headers.get("x-lark-request-timestamp", ""))
    nonce = str(headers.get("X-Lark-Request-Nonce", "") or headers.get("x-lark-request-nonce", ""))
    signature = str(headers.get("X-Lark-Signature", "") or headers.get("x-lark-signature", ""))
    if not timestamp or not nonce or not signature:
        return False
    sign_data = f"{timestamp}{nonce}{encrypted}".encode("utf-8")
    local_signature = base64.b64encode(
        hmac.new(encrypt_key.encode("utf-8"), sign_data, hashlib.sha256).digest()
    ).decode("utf-8")
    return hmac.compare_digest(local_signature, signature)


def _parse_message_text(message_obj: dict) -> str:
    content = message_obj.get("content")
    if isinstance(content, str):
        try:
            content_json = json.loads(content)
            if isinstance(content_json, dict):
                return str(content_json.get("text", "") or "")
        except Exception:
            return content
    if isinstance(content, dict):
        return str(content.get("text", "") or "")
    return ""


def _enqueue_message_event(raw_user_id: str, raw_chat_id: str, chat_type: str, user_name: str, text: str, raw_message_id: str = "") -> dict | None:
    raw_user_id = str(raw_user_id or "")
    if not raw_user_id:
        return None
    raw_chat_id = str(raw_chat_id or "")
    chat_type = str(chat_type or "group")
    user_name = str(user_name or raw_user_id)
    user_id = _map_user(raw_user_id, user_name)
    group_id = _map_chat(raw_chat_id) if raw_chat_id else None
    msg_id = _next_msg_id(str(raw_message_id or ""))
    return {
        "time": int(time.time()),
        "self_id": str(config.feishu.get("app_id", "")),
        "post_type": "message",
        "message_type": "private" if chat_type == "p2p" else "group",
        "sub_type": "normal",
        "message_id": msg_id,
        "user_id": user_id,
        "group_id": group_id,
        "message": [{"type": "text", "data": {"text": text or ""}}],
        "raw_message": text or "",
        "sender": {
            "user_id": user_id,
            "nickname": user_name,
            "card": user_name
        },
        "platform": "Feishu",
        "session_id": f"{'user' if chat_type == 'p2p' else 'chat'}:{raw_chat_id or raw_user_id}",
        "source_platform_user_id": raw_user_id,
        "source_platform_chat_id": raw_chat_id
    }


def _obj_to_dict(obj):
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return {k: _obj_to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_obj_to_dict(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)):
        return obj
    for meth in ("to_dict", "to_json"):
        if hasattr(obj, meth):
            try:
                return _obj_to_dict(getattr(obj, meth)())
            except Exception:
                pass
    if hasattr(obj, "__dict__"):
        return {k: _obj_to_dict(v) for k, v in vars(obj).items() if not str(k).startswith("_")}
    return obj


def _pick_first(data: dict, paths: list[tuple[str, ...]], default=""):
    for path in paths:
        cur = data
        ok = True
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                ok = False
                break
        if ok and cur not in (None, ""):
            return cur
    return default


def _handle_long_connection_event(event_payload) -> None:
    data = _obj_to_dict(event_payload)
    raw_chat_id = str(_pick_first(data, [
        ("event", "message", "chat_id"),
        ("message", "chat_id"),
        ("chat_id",),
    ], ""))
    chat_type = str(_pick_first(data, [
        ("event", "message", "chat_type"),
        ("message", "chat_type"),
        ("chat_type",),
    ], "group"))
    raw_message_id = str(_pick_first(data, [
        ("event", "message", "message_id"),
        ("message", "message_id"),
        ("message_id",),
    ], ""))
    raw_user_id = str(_pick_first(data, [
        ("event", "sender", "sender_id", "open_id"),
        ("event", "sender", "sender_id", "user_id"),
        ("event", "sender", "sender_id", "union_id"),
        ("sender", "sender_id", "open_id"),
        ("sender", "sender_id", "user_id"),
        ("sender", "sender_id", "union_id"),
        ("open_id",),
        ("user_id",),
        ("union_id",),
    ], ""))
    user_name = str(_pick_first(data, [
        ("event", "sender", "sender_name"),
        ("event", "sender", "name"),
        ("sender", "sender_name"),
        ("sender", "name"),
    ], raw_user_id))
    content = _pick_first(data, [
        ("event", "message", "content"),
        ("message", "content"),
        ("content",),
    ], "")
    text = ""
    if isinstance(content, str):
        try:
            text = str((json.loads(content) or {}).get("text", "") or "")
        except Exception:
            text = content
    elif isinstance(content, dict):
        text = str(content.get("text", "") or "")
    post_data = _enqueue_message_event(
        raw_user_id=raw_user_id,
        raw_chat_id=raw_chat_id,
        chat_type=chat_type,
        user_name=user_name,
        text=text,
        raw_message_id=raw_message_id
    )
    if post_data is not None:
        _event_queue.put(post_data)


def _start_long_connection_listener() -> bool:
    global _ws_client, _ws_thread
    try:
        import lark_oapi as lark
    except Exception as e:
        logger.warning(f"飞书长连接模式不可用，未安装 lark-oapi: {e}")
        return False

    app_id = str(config.feishu.get("app_id", "") or "")
    app_secret = str(config.feishu.get("app_secret", "") or "")
    if not app_id or not app_secret:
        logger.error("飞书长连接模式需要配置 feishu.app_id 和 feishu.app_secret")
        return False

    def do_p2_im_message_receive_v1(data) -> None:
        _handle_long_connection_event(data)

    try:
        event_handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(
            do_p2_im_message_receive_v1
        ).build()
        _ws_client = lark.ws.Client(
            app_id,
            app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO
        )
    except Exception as e:
        logger.error(f"初始化飞书长连接客户端失败: {e}")
        return False

    def runner():
        try:
            logger.info("飞书事件接收模式：long_connection")
            _ws_client.start()
        except Exception as e:
            logger.error(f"飞书长连接监听异常: {e}")

    _ws_thread = threading.Thread(target=runner, daemon=True)
    _ws_thread.start()
    return True


def _to_event_model(payload: dict) -> dict | None:
    schema = payload.get("schema")
    if schema == "2.0":
        event_body = payload.get("event") or {}
        msg = event_body.get("message") or {}
        if not msg:
            return None
        sender = event_body.get("sender") or {}
        sender_id = sender.get("sender_id") or {}
        raw_user_id = str(
            sender_id.get("open_id")
            or sender_id.get("user_id")
            or sender_id.get("union_id")
            or ""
        )
        if not raw_user_id:
            return None
        raw_chat_id = str(msg.get("chat_id") or "")
        chat_type = str(msg.get("chat_type") or "group")
        user_name = str(sender.get("sender_name") or sender.get("name") or raw_user_id)
        text = _parse_message_text(msg)
        return _enqueue_message_event(
            raw_user_id=raw_user_id,
            raw_chat_id=raw_chat_id,
            chat_type=chat_type,
            user_name=user_name,
            text=text,
            raw_message_id=str(msg.get("message_id") or "")
        )
    return None


def _start_http_listener() -> None:
    global _app, _app_thread, _listener_started
    if _listener_started:
        return
    _app = flask.Flask(__name__)

    event_path = str(config.feishu.get("event_path", "/feishu/events") or "/feishu/events")
    if not event_path.startswith("/"):
        event_path = "/" + event_path

    @_app.route(event_path, methods=["POST"])
    def feishu_events():
        payload = flask.request.get_json(silent=True) or {}
        if not _verify_token(payload):
            return flask.jsonify({"code": 403, "msg": "invalid token"}), 403
        if not _verify_signature(payload, dict(flask.request.headers)):
            return flask.jsonify({"code": 403, "msg": "invalid signature"}), 403
        if payload.get("type") == "url_verification":
            challenge = payload.get("challenge", "")
            logger.info("飞书 challenge 校验成功")
            return flask.jsonify({"challenge": challenge})
        event_data = _to_event_model(payload)
        if event_data is not None:
            _event_queue.put(event_data)
        return flask.jsonify({"code": 0})

    host = str(config.feishu.get("listener_host", "127.0.0.1") or "127.0.0.1")
    port = int(config.feishu.get("listener_port", 5003) or 5003)
    _app_thread = threading.Thread(target=lambda: _app.run(host=host, port=port), daemon=True)
    _app_thread.start()
    _listener_started = True


class Actions:
    def __init__(self, cnt=None):
        self.connection = cnt

        class CustomAction:
            def __init__(self, action_obj: "Actions"):
                self._actions = action_obj

            def __getattr__(self, item) -> callable:
                async def wrapper(**kwargs) -> str:
                    if hasattr(self._actions, item):
                        method = getattr(self._actions, item)
                        return await method(**kwargs)
                    return _put_ret(item, status="failed", retcode=10001, message=f"unsupported action: {item}")

                return wrapper

        self.custom = CustomAction(self)

    async def send(self, message: Union[common.Message, str], group_id: int = None, user_id: int = None) -> common.Ret[MsgSendRsp]:
        receive_type = None
        receive_id = None
        if group_id is not None:
            receive_type = "chat_id"
            receive_id = _chat_map_int_to_raw.get(int(group_id), "")
        elif user_id is not None:
            receive_type = "open_id"
            receive_id = _user_map_int_to_raw.get(int(user_id), "")
        if not receive_type or not receive_id:
            raise errors.ArgsInvalidError("'send' API requires mappable 'group_id' or 'user_id'.")

        text = _build_text_from_message(message)
        if not text:
            logger.info(f"飞书跳过发送：消息在当前模式下无可发送文本 ({receive_type}:{receive_id})")
            echo = _put_ret("send_msg", data={"message_id": 0})
            return common.Ret.fetch(echo, MsgSendRsp)
        payload = {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False)
        }
        last_error = ""
        for _ in range(3):
            try:
                token = _get_feishu_token()
                res = httpx.post(
                    f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_type}",
                    headers={"Authorization": f"Bearer {token}"},
                    json=payload,
                    timeout=10.0
                )
                data = res.json()
                if data.get("code") == 0:
                    raw_message_id = str(((data.get("data") or {}).get("message_id")) or "")
                    msg_id = _next_msg_id(raw_message_id)
                    echo = _put_ret("send_msg", data={"message_id": msg_id})
                    logger.info(f"向{('群 ' + str(group_id)) if group_id else ('用户 ' + str(user_id))} 发送：{text}")
                    return common.Ret.fetch(echo, MsgSendRsp)
                last_error = str(data.get("msg") or data)
                logger.warning(f"飞书发送失败({receive_type}:{receive_id}) code={data.get('code')} msg={last_error}")
            except Exception as e:
                last_error = str(e)
                logger.warning(f"飞书发送异常({receive_type}:{receive_id}) {last_error}")
            time.sleep(1)
        echo = _put_ret("send_msg", data={"message_id": 0}, status="failed", retcode=500, message=last_error)
        return common.Ret.fetch(echo, MsgSendRsp)

    async def del_message(self, message_id: int) -> None:
        raw_message_id = _message_map_int_to_raw.get(int(message_id), "")
        if not raw_message_id:
            return
        try:
            token = _get_feishu_token()
            httpx.delete(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{raw_message_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0
            )
        except Exception:
            return

    async def set_group_kick(self, group_id: int, user_id: int) -> None:
        return

    async def set_group_ban(self, group_id: int, user_id: int, duration: int = 60) -> None:
        return

    async def set_group_add_request(self, flag: str, sub_type: str, approve: bool, reason: str = "Not Mentioned") -> None:
        return

    async def set_friend_add_request(self, flag: str, approve: bool, remark: str = "") -> None:
        return

    async def get_login_info(self) -> common.Ret[GetLoginInfoRsp]:
        echo = _put_ret("get_login_info", data={"user_id": 0, "nickname": "FeishuBot"})
        return common.Ret.fetch(echo, GetLoginInfoRsp)

    async def get_version_info(self) -> common.Ret[GetVerInfoRsp]:
        echo = _put_ret("get_version_info", data={"app_name": "FeishuBot", "app_version": "1.0", "protocol_version": "feishu-v3"})
        return common.Ret.fetch(echo, GetVerInfoRsp)

    async def get_stranger_info(self, user_id: int, no_cache: bool = True) -> str:
        uid = int(user_id)
        nick = _user_name_map.get(uid, str(uid))
        return _put_ret("get_stranger_info", data={"user_id": uid, "nickname": nick, "sex": "unknown", "age": 0})

    async def get_group_member_info(self, group_id: int, user_id: int, no_cache: bool = True) -> str:
        uid = int(user_id)
        gid = int(group_id)
        nick = _user_name_map.get(uid, str(uid))
        return _put_ret(
            "get_group_member_info",
            data={
                "group_id": gid,
                "user_id": uid,
                "nickname": nick,
                "card": nick,
                "sex": "unknown",
                "age": 0,
                "area": "",
                "join_time": 0,
                "last_sent_time": 0,
                "level": "",
                "role": "member",
                "unfriendly": False,
                "title": "",
                "title_expire_time": 0,
                "card_changeable": False
            }
        )

    async def get_group_info(self, group_id: int) -> str:
        gid = int(group_id)
        return _put_ret(
            "get_group_info",
            data={
                "group_id": gid,
                "group_name": _chat_map_int_to_raw.get(gid, str(gid)),
                "member_count": 0,
                "max_member_count": 0
            }
        )

    async def get_group_list(self, no_cache: bool = True) -> str:
        groups = [{"group_id": gid, "group_name": raw} for gid, raw in _chat_map_int_to_raw.items()]
        return _put_ret("get_group_list", data=groups)

    async def get_msg(self, msg_id: int) -> common.Ret[GetMsgRsp]:
        echo = _put_ret(
            "get_msg",
            data={
                "time": int(time.time()),
                "message_type": "group",
                "message_id": int(msg_id),
                "real_id": int(msg_id),
                "sender": {"user_id": 0, "nickname": "Feishu"},
                "message": [{"type": "text", "data": {"text": ""}}]
            }
        )
        return common.Ret.fetch(echo, GetMsgRsp)

    async def send_forward_msg(self, message: common.Message) -> common.Ret[SendForwardRsp]:
        text = _build_text_from_message(message)
        if not text:
            text = " "
        echo = _put_ret("send_forward_msg", data=str(text))
        return common.Ret.fetch(echo, SendForwardRsp)

    async def send_group_forward_msg(self, group_id: int, message: common.Message) -> common.Ret[SendGrpForwardRsp]:
        receive_id = _chat_map_int_to_raw.get(int(group_id), "")
        if not receive_id:
            raise errors.ArgsInvalidError("send_group_forward_msg requires mapped group_id for Feishu")
        forward_texts = _extract_forward_texts(message)
        raw_message_ids = []
        for text in forward_texts:
            sent = await self.send(message=common.Message(segments.Text(text)), group_id=group_id)
            raw_id = _message_map_int_to_raw.get(int(sent.data.message_id), "")
            if raw_id:
                raw_message_ids.append(raw_id)
        if not raw_message_ids:
            sent = await self.send(message=common.Message(segments.Text(" ")), group_id=group_id)
            echo = _put_ret("send_group_forward_msg", data={"message_id": sent.data.message_id, "forward_id": ""})
            return common.Ret.fetch(echo, SendGrpForwardRsp)

        token = _get_feishu_token()
        merge_body = {
            "receive_id": receive_id,
            "message_id_list": raw_message_ids,
        }
        try:
            resp = httpx.post(
                "https://open.feishu.cn/open-apis/im/v1/messages/merge_forward?receive_id_type=chat_id",
                headers={"Authorization": f"Bearer {token}"},
                json=merge_body,
                timeout=10.0
            )
            data = resp.json()
            if data.get("code") == 0:
                merged_raw_id = str(((data.get("data") or {}).get("message_id")) or "")
                merged_msg_id = _next_msg_id(merged_raw_id)
                echo = _put_ret("send_group_forward_msg", data={"message_id": merged_msg_id, "forward_id": merged_raw_id})
                return common.Ret.fetch(echo, SendGrpForwardRsp)
            logger.warning(f"飞书合并转发失败(chat_id:{receive_id}) code={data.get('code')} msg={data.get('msg')}")
        except Exception as e:
            logger.warning(f"飞书合并转发异常(chat_id:{receive_id}) {e}")

        merged_text = "\n".join(forward_texts).strip() or " "
        fallback = await self.send(message=common.Message(segments.Text(merged_text)), group_id=group_id)
        echo = _put_ret("send_group_forward_msg", data={"message_id": fallback.data.message_id, "forward_id": ""})
        return common.Ret.fetch(echo, SendGrpForwardRsp)

    async def get_forward_msg(self, sid: str) -> common.Ret[common.Message]:
        echo = _put_ret("get_forward_msg", data=[])
        return common.Ret.fetch(echo, events.gen_message)

    async def forward_solve(self, message: common.Message) -> common.Message:
        return message

    async def get_status(self) -> common.Ret:
        echo = _put_ret("get_status", data={"online": _running, "good": True})
        return common.Ret.fetch(echo)

    async def set_essence_msg(self, message_id: int) -> None:
        return

    async def set_group_special_title(self, group_id: int, user_id: int, title: str) -> None:
        return

    async def send_callback(self, group_id: int, bot_id: int, data: dict) -> None:
        return


async def tester(message_data: Union[Event, HyperNotify], actions: Actions) -> None:
    return


handler = tester


def reg(func: callable) -> None:
    global handler
    handler = func


def __handler(data: Union[dict, HyperNotify], actions: Actions) -> None:
    if isinstance(data, dict):
        asyncio.run(handler(events.em.new(data), actions))
    else:
        asyncio.run(handler(data, actions))


def run() -> NoReturn:
    global listener_ran, _running
    listener_ran = True
    _running = True
    try:
        if handler is tester:
            raise errors.ListenerNotRegisteredError("No handler registered")
        mode = _normalize_event_mode()
        if mode == "webhook":
            logger.info("飞书事件接收模式：webhook")
            _start_http_listener()
        else:
            if not _start_long_connection_listener():
                logger.warning("飞书长连接启动失败，回退到 webhook")
                _start_http_listener()
        actions = Actions(None)
        start_notify = HyperListenerStartNotify(
            time_now=int(time.time()),
            notify_type="listener_start",
            connection=None
        )
        threading.Thread(target=lambda: __handler(start_notify, actions), daemon=True).start()
        while _running:
            data = _event_queue.get()
            if data is None:
                continue
            threading.Thread(target=lambda d=data: __handler(d, actions), daemon=True).start()
    except KeyboardInterrupt:
        stop()
    except Exception as e:
        logger.error(f"飞书监听器异常退出: {e}")


def stop() -> None:
    global _running
    _running = False
    try:
        _event_queue.put(None)
    except Exception:
        return
