import asyncio
import json
import importlib
import queue
import random
import threading
import time
import logging

from flask import Flask, jsonify, request

from ..utils.hypetyping import Any, NoReturn, Union
from ..utils.apiresponse import *
from ..events import *
from .. import events, common, segments
from ..utils import errors
from .FeishuLib.client import FeishuClient
from .FeishuLib.translator import build_hyper_event, stringify_feishu_message
from .FeishuLib.Manager import reports as feishu_reports

config = configurator.BotConfig.get("hyper-bot")
logger = hyperogger.Logger()
logger.set_level(config.log_level)
listener_ran = False
_lark_logger_bound = False


class LarkToHyperHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = str(record.msg)
        ignored = [
            "processor not found, type: p2p_chat_create",
            "processor not found, type: im.chat.access_event.bot_p2p_chat_entered_v1",
        ]
        if any(x in msg for x in ignored):
            logger.debug(msg)
            return
        if record.levelno >= logging.ERROR:
            logger.error(msg)
        elif record.levelno >= logging.WARNING:
            logger.warning(msg)
        elif record.levelno >= logging.INFO:
            logger.info(msg)
        else:
            logger.debug(msg)


def setup_lark_log_bridge() -> None:
    global _lark_logger_bound
    if _lark_logger_bound:
        return
    target_names = ["lark_oapi", "lark_oapi.ws", "lark_oapi.ws.client", "Lark"]
    bridge = LarkToHyperHandler()
    bridge.setFormatter(logging.Formatter("[LarkSDK] %(message)s"))
    for name in target_names:
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        lg.handlers.clear()
        lg.propagate = False
        lg.addHandler(bridge)
    _lark_logger_bound = True


class Actions:
    def __init__(self, client: FeishuClient):
        self.client = client

        class CustomAction:
            def __init__(self, outer):
                self.outer = outer

            def __getattr__(self, item) -> callable:
                async def wrapper(**kwargs) -> str:
                    return self.outer._custom_call(item, **kwargs)

                return wrapper

        self.custom = CustomAction(self)

    @staticmethod
    def _make_echo(prefix: str) -> str:
        return f"{prefix}_{random.randint(1000, 9999)}"

    @staticmethod
    def _put_result(echo: str, data: dict = None, status: str = "ok", retcode: int = 0) -> None:
        payload = {
            "status": status,
            "retcode": retcode,
            "data": data or {},
            "echo": echo,
        }
        feishu_reports.put(echo, payload)

    def _custom_call(self, endpoint: str, **kwargs) -> str:
        echo = self._make_echo(endpoint)
        try:
            if endpoint == "get_group_list":
                data = {"raw": []}
            elif endpoint == "group_poke":
                data = {"status": "unsupported"}
            elif endpoint == "friend_poke":
                data = {"status": "unsupported"}
            elif endpoint == "set_group_whole_ban":
                data = {"status": "unsupported"}
            elif endpoint == "set_group_leave":
                data = {"status": "unsupported"}
            elif endpoint == "set_group_special_title":
                data = {"status": "unsupported"}
            elif endpoint == "send_like":
                data = {"status": "unsupported"}
            elif endpoint == "get_stranger_info":
                user_id = kwargs.get("user_id")
                user = self.client.get_user(str(user_id))
                data = {
                    "user_id": str(user_id),
                    "nickname": user.get("name") or str(user_id),
                    "sex": "unknown",
                    "age": 0,
                }
            elif endpoint == "get_forward_msg":
                data = {"message": []}
            else:
                data = {"status": "unsupported"}
            self._put_result(echo, data=data)
        except Exception as e:
            self._put_result(echo, data={"message": str(e)}, status="failed", retcode=1)
        return echo

    async def send(
        self, message: Union[common.Message, str], group_id: int = None, user_id: int = None
    ) -> common.Ret[MsgSendRsp]:
        if isinstance(message, str):
            message = common.Message(segments.Text(message))
        if group_id is None and user_id is None:
            raise errors.ArgsInvalidError("'send' API requires 'group_id' or 'user_id' but none of them are provided.")

        receive_id_type = "chat_id" if group_id is not None else "open_id"
        receive_id = str(group_id) if group_id is not None else str(user_id)

        reply_to_id = None
        text_parts = []
        sent_message_ids = []
        for seg in message:
            if isinstance(seg, segments.Reply):
                reply_to_id = str(seg.id)
            elif isinstance(seg, segments.Text):
                text_parts.append(seg.text)
            elif isinstance(seg, segments.At):
                text_parts.append(f'<at user_id="{seg.qq}">@{seg.qq}</at>')
            elif isinstance(seg, segments.Image):
                try:
                    image_key = str(seg.file)
                    if not image_key.startswith("img_"):
                        image_key = self.client.upload_image(image_key)
                    image_data = self.client.send_message(
                        receive_id_type=receive_id_type,
                        receive_id=receive_id,
                        msg_type="image",
                        content={"image_key": image_key},
                    )
                    sent_message_ids.append(str(image_data.get("message_id", "")))
                except Exception as e:
                    logger.warning(f"Feishu 图片发送失败，降级为文本提示: {e}")
                    text_parts.append("[图片发送失败：请检查应用权限 im:resource:upload / im:resource]")
            else:
                text_parts.append(str(seg))

        text_content = "".join(text_parts).strip()
        if text_content:
            if reply_to_id:
                text_data = self.client.reply_message(
                    message_id=reply_to_id,
                    msg_type="text",
                    content={"text": text_content},
                )
            else:
                text_data = self.client.send_message(
                    receive_id_type=receive_id_type,
                    receive_id=receive_id,
                    msg_type="text",
                    content={"text": text_content},
                )
            sent_message_ids.append(str(text_data.get("message_id", "")))

        message_id = next((mid for mid in reversed(sent_message_ids) if mid), "")
        echo = self._make_echo("send_msg")
        self._put_result(echo, data={"message_id": message_id})
        logger.info(f"向{(('群 ' + str(group_id)) if group_id else ('用户' + str(user_id))) + ' '}发送：{str(message)}")
        return common.Ret.fetch(echo, MsgSendRsp)

    async def del_message(self, message_id: int) -> None:
        try:
            self.client.delete_message(str(message_id))
        except Exception:
            pass
        logger.info(f"撤回 {message_id}")

    async def set_group_kick(self, group_id: int, user_id: int) -> None:
        logger.warning("Feishu 当前不支持 set_group_kick，已忽略")

    async def set_group_ban(self, group_id: int, user_id: int, duration: int = 60) -> None:
        logger.warning("Feishu 当前不支持 set_group_ban，已忽略")

    async def get_login_info(self) -> common.Ret[GetLoginInfoRsp]:
        echo = self._make_echo("get_login_info")
        self._put_result(echo, data={"user_id": self.client.bot_open_id, "nickname": "FeishuBot"})
        return common.Ret.fetch(echo, GetLoginInfoRsp)

    async def get_version_info(self) -> common.Ret[GetVerInfoRsp]:
        echo = self._make_echo("get_version_info")
        self._put_result(
            echo,
            data={
                "app_name": "FeishuBot",
                "app_version": "1.0",
                "protocol_version": "im.message.receive_v1",
            },
        )
        return common.Ret.fetch(echo, GetVerInfoRsp)

    async def send_forward_msg(self, message: common.Message) -> common.Ret[SendForwardRsp]:
        text = "\n".join(str(seg) for seg in message)
        echo = self._make_echo("send_forward_msg")
        self._put_result(echo, data={"res_id": text})
        return common.Ret.fetch(echo, SendForwardRsp)

    async def get_forward_msg(self, sid: str) -> common.Ret[common.Message]:
        echo = self._make_echo("get_forward_msg")
        self._put_result(echo, data={"message": []})
        return common.Ret.fetch(echo, events.gen_message)

    async def forward_solve(self, message: common.Message) -> common.Message:
        return common.Message(segments.Text(str(message)))

    async def send_group_forward_msg(self, group_id: int, message: common.Message) -> common.Ret[SendGrpForwardRsp]:
        sent = await self.send(message=str(message), group_id=group_id)
        message_id = sent.data.message_id
        echo = self._make_echo("send_group_forward_msg")
        self._put_result(echo, data={"message_id": message_id, "forward_id": str(message_id)})
        return common.Ret.fetch(echo, SendGrpForwardRsp)

    async def set_group_add_request(self, flag: str, sub_type: str, approve: bool, reason: str = "Not Mentioned") -> None:
        logger.warning("Feishu 当前不支持 set_group_add_request，已忽略")

    async def get_stranger_info(self, user_id: int) -> common.Ret[GetStrInfoRsp]:
        user = self.client.get_user(str(user_id))
        echo = self._make_echo("get_stranger_info")
        self._put_result(
            echo,
            data={
                "user_id": str(user_id),
                "nickname": user.get("name") or str(user_id),
                "sex": "unknown",
                "age": 0,
            },
        )
        return common.Ret.fetch(echo, GetStrInfoRsp)

    async def get_group_member_info(self, group_id: int, user_id: int) -> common.Ret[GetGrpMemInfoRsp]:
        echo = self._make_echo("get_group_member_info")
        self._put_result(
            echo,
            data={
                "group_id": str(group_id),
                "user_id": str(user_id),
                "nickname": str(user_id),
                "card": "",
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
                "card_changeable": False,
            },
        )
        return common.Ret.fetch(echo, GetGrpMemInfoRsp)

    async def get_group_info(self, group_id: int) -> common.Ret[GetGrpInfoRsp]:
        group = self.client.get_chat(str(group_id))
        echo = self._make_echo("get_group_info")
        self._put_result(
            echo,
            data={
                "group_id": str(group_id),
                "group_name": group.get("name") or str(group_id),
                "member_count": int(group.get("member_count") or 0),
                "max_member_count": int(group.get("max_member_count") or 0),
            },
        )
        return common.Ret.fetch(echo, GetGrpInfoRsp)

    async def get_status(self) -> common.Ret:
        echo = self._make_echo("get_status")
        self._put_result(echo, data={"online": True})
        return common.Ret.fetch(echo)

    async def set_essence_msg(self, message_id: int) -> None:
        logger.warning("Feishu 当前不支持 set_essence_msg，已忽略")

    async def set_group_special_title(self, group_id: int, user_id: int, title: str) -> None:
        logger.warning("Feishu 当前不支持 set_group_special_title，已忽略")

    async def get_msg(self, msg_id: int) -> common.Ret[GetMsgRsp]:
        msg = self.client.get_message(str(msg_id))
        chat_type = str(msg.get("chat_type") or "group").lower()
        message_type = "private" if chat_type == "p2p" else "group"
        chat_id = msg.get("chat_id")
        sender_id = msg.get("sender", {}).get("id") or msg.get("sender", {}).get("sender_id", {}).get("open_id") or ""
        content_text = stringify_feishu_message(msg)
        echo = self._make_echo("get_msg")
        sender = {
            "user_id": str(sender_id),
            "nickname": str(sender_id or "未知用户"),
            "sex": "unknown",
            "age": 0,
        }
        if message_type == "group":
            sender.update({"card": "", "area": "", "level": "", "role": "member", "title": ""})
        self._put_result(
            echo,
            data={
                "time": int(time.time()),
                "message_type": message_type,
                "message_id": str(msg_id),
                "real_id": str(msg_id),
                "sender": sender,
                "message": [{"type": "text", "data": {"text": content_text}}],
                "group_id": str(chat_id) if message_type == "group" else None,
                "user_id": str(sender_id),
            },
        )
        return common.Ret.fetch(echo, GetMsgRsp)

    async def send_callback(self, group_id: int, bot_id: int, data: dict) -> None:
        logger.warning("Feishu 当前不支持 send_callback，已忽略")


async def tester(message_data: Union[Event, HyperNotify], actions: Actions) -> None:
    ...


def __handler(data: Union[dict, HyperNotify], actions: Actions) -> None:
    if isinstance(data, dict):
        asyncio.run(handler(events.em.new(data), actions))
    else:
        asyncio.run(handler(data, actions))


handler: callable = tester


def reg(func: callable) -> None:
    global handler
    handler = func


class FeishuEventServer:
    def __init__(self, client: FeishuClient):
        self.client = client
        self.queue = queue.Queue()
        self.event_cache = {}
        self.app = Flask(__name__)
        self.app.config["JSON_AS_ASCII"] = False
        self._register_routes()

    def _register_routes(self):
        callback_path = self.client.callback_path

        @self.app.route(callback_path, methods=["POST"])
        def callback():
            payload = request.get_json(silent=True) or {}
            if payload.get("type") == "url_verification":
                token = payload.get("token", "")
                if self.client.verification_token and token != self.client.verification_token:
                    return jsonify({"code": 1, "msg": "token mismatch"}), 403
                return jsonify({"challenge": payload.get("challenge", "")})

            header = payload.get("header", {})
            token = header.get("token")
            if self.client.verification_token and token and token != self.client.verification_token:
                return jsonify({"code": 1, "msg": "token mismatch"}), 403

            event_id = header.get("event_id")
            if event_id:
                now = int(time.time())
                stale_keys = [k for k, v in self.event_cache.items() if now - v > 8 * 3600]
                for k in stale_keys:
                    self.event_cache.pop(k, None)
                if event_id in self.event_cache:
                    return jsonify({})
                self.event_cache[event_id] = now

            self.queue.put(payload)
            return jsonify({})

    def run(self, host: str, port: int):
        self.app.run(host=host, port=port)


class FeishuLongConnectionWorker:
    def __init__(self, client: FeishuClient, event_queue: queue.Queue):
        self.client = client
        self.event_queue = event_queue
        self.ws_client = None

    def _register_event_handlers(self):
        lark = importlib.import_module("lark_oapi")
        dispatcher_module = importlib.import_module("lark_oapi.event.dispatcher_handler")
        builder = dispatcher_module.EventDispatcherHandlerBuilder(
            self.client.encrypt_key,
            self.client.verification_token
        )

        def _push_event(data):
            try:
                payload_json = lark.JSON.marshal(data)
                if payload_json:
                    self.event_queue.put(json.loads(payload_json))
            except Exception as e:
                logger.error(f"Feishu 长连接事件解析失败: {e}")

        if hasattr(builder, "register_p2_im_message_receive_v1"):
            builder.register_p2_im_message_receive_v1(_push_event)
        if hasattr(builder, "register_p2_application_bot_menu_v6"):
            builder.register_p2_application_bot_menu_v6(_push_event)
        if hasattr(builder, "register_p2_im_chat_access_event_bot_p2p_chat_entered_v1"):
            builder.register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(lambda _data: None)
        return builder.build()

    def run(self):
        lark = importlib.import_module("lark_oapi")
        setup_lark_log_bridge()
        app_id = str(self.client.app_id or "")
        app_secret = str(self.client.app_secret or "")
        if not app_id or not app_secret:
            raise RuntimeError("Feishu 长连接模式需要配置 Connections.Feishu.app_id / app_secret（兼容 Others.feishu）")
        event_handler = self._register_event_handlers()
        self.ws_client = lark.ws.Client(
            app_id,
            app_secret,
            log_level=lark.LogLevel.INFO,
            event_handler=event_handler
        )
        self.ws_client.start()


event_server: FeishuEventServer | None = None


def run() -> NoReturn:
    global listener_ran, event_server
    listener_ran = True
    if handler is tester:
        raise errors.ListenerNotRegisteredError("No handler registered")

    client = FeishuClient(config)
    actions = Actions(client)
    event_queue = queue.Queue()

    event_mode = client.event_mode
    if event_mode in {"long_connection", "long", "ws", "websocket"}:
        worker = FeishuLongConnectionWorker(client, event_queue)
        threading.Thread(target=worker.run, daemon=True).start()
        logger.info("Feishu 长连接模式已启动（Lark OAPI）")
    else:
        event_server = FeishuEventServer(client)

        listener_host = "0.0.0.0"
        listener_port = 8081
        conn_config = config.get_connection("Feishu")
        if isinstance(conn_config, configurator.BotHTTPC):
            listener_host = conn_config.listener_host
            listener_port = conn_config.listener_port
        elif isinstance(conn_config, configurator.BotWSC):
            listener_host = conn_config.host
            listener_port = conn_config.port

        threading.Thread(
            target=lambda: event_server.run(listener_host, listener_port),
            daemon=True
        ).start()
        logger.info(f"Feishu 回调监听已启动 http://{listener_host}:{listener_port}{client.callback_path}")

    start_notify = HyperListenerStartNotify(
        time_now=int(time.time()),
        notify_type="listener_start",
        connection=None
    )
    threading.Thread(target=lambda: __handler(start_notify, actions), daemon=True).start()

    while listener_ran:
        payload = event_queue.get() if event_mode in {"long_connection", "long", "ws", "websocket"} else event_server.queue.get()
        event_data = build_hyper_event(payload, client.bot_open_id)
        if event_data is None:
            continue
        threading.Thread(target=lambda: __handler(event_data, actions), daemon=True).start()


def stop() -> None:
    global listener_ran
    listener_ran = False
