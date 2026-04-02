from ..utils.hypetyping import Any, NoReturn, TypeVar, Callable
from ..utils.apiresponse import *
from ..events import *
from .. import events, common, segments
from ..utils import errors

from .MilkyLib.translator import MilkyHttpConnection, msg_deid, msg_enid, message_translator
from .MilkyLib.Manager import Packet
from .MilkyLib.types import MilkyOutgoingSegment, consume_segment, consume_segments, make_text_segment

import time
import threading
import asyncio
import json
import sys

config = configurator.BotConfig.get("hyper-bot")
logger = hyperogger.Logger()
logger.set_level(config.log_level)
listener_ran = False


class Actions:
    def __init__(self, cnt: MilkyHttpConnection):
        self.connection = cnt

        class CustomAction:
            def __init__(self, cnt_i: MilkyHttpConnection):
                self.connection = cnt_i

            def __getattr__(self, item) -> callable:
                async def wrapper(**kwargs) -> str:
                    packet = Packet(
                        str(item),
                        **kwargs
                    )
                    packet.send_to(self.connection)
                    return packet.echo

                return wrapper

        self.custom = CustomAction(self.connection)

    @staticmethod
    def _is_successful_response(res: Any) -> bool:
        if not isinstance(res, dict):
            return False
        if res.get("status") == "ok":
            return True
        if res.get("retcode") == 0:
            return True
        if res.get("code") == 0:
            return True
        return False

    def _send_with_endpoint_fallback(self, endpoints: list[str], **payload) -> tuple[Packet, dict]:
        last_packet: Packet = None
        last_res: dict = {}
        for endpoint in endpoints:
            packet = Packet(endpoint, **payload)
            res = packet.send_to(self.connection)
            last_packet = packet
            if self._is_successful_response(res):
                return packet, res
            last_res = res if isinstance(res, dict) else {}
        return last_packet, last_res

    def _send_with_payload_fallback(self, calls: list[tuple[str, dict]]) -> tuple[Packet, dict]:
        last_packet: Packet = None
        last_res: dict = {}
        for endpoint, payload in calls:
            packet = Packet(endpoint, **payload)
            res = packet.send_to(self.connection)
            last_packet = packet
            if self._is_successful_response(res):
                return packet, res
            last_res = res if isinstance(res, dict) else {}
        return last_packet, last_res

    async def send(
            self, message: Union[common.Message, str], group_id: int = None, user_id: int = None
    ) -> common.Ret[MsgSendRsp]:
        if isinstance(message, str):
            message = common.Message(segments.Text(message))
        outgoing: list[MilkyOutgoingSegment] = []
        for seg in message:
            if hasattr(seg, "milky_outgoing_seg"):
                outgoing_seg = consume_segment(seg.milky_outgoing_seg())
                if outgoing_seg is not None:
                    outgoing.append(outgoing_seg)
                else:
                    outgoing.append(make_text_segment(str(seg)))
            else:
                outgoing.append(make_text_segment(str(seg)))
        if group_id is not None:
            scene = 1
            peer_id = int(group_id)
            endpoint = "send_group_message"
            payload = {"group_id": peer_id, "message": outgoing}
        elif user_id is not None:
            scene = 0
            peer_id = int(user_id)
            endpoint = "send_private_message"
            payload = {"user_id": peer_id, "message": outgoing}
        else:
            raise errors.ArgsInvalidError("'send' API requires 'group_id' or 'user_id' but none of them are provided.")

        packet = Packet(endpoint, **payload)
        res = packet.send_to(self.connection)
        if (not self._is_successful_response(res)) and any(isinstance(i, dict) and i.get("type") == "reply" for i in outgoing):
            fallback_outgoing = [i for i in outgoing if not (isinstance(i, dict) and i.get("type") == "reply")]
            if len(fallback_outgoing) == 0:
                fallback_outgoing = [make_text_segment("")]
            fallback_payload = payload.copy()
            fallback_payload["message"] = fallback_outgoing
            fallback_packet = Packet(endpoint, **fallback_payload)
            fallback_res = fallback_packet.send_to(self.connection)
            if self._is_successful_response(fallback_res):
                packet = fallback_packet
                res = fallback_res
        if isinstance(res, dict) and isinstance(res.get("data"), dict):
            data = res["data"]
            if "message_id" not in data:
                seq = data.get("message_seq")
                if seq is not None:
                    data["message_id"] = msg_enid(scene, int(seq), peer_id)
        logger.info(f"向{(('群 ' + str(group_id)) if group_id else ('用户' + str(user_id))) + ' '}发送：{str(message)}")
        return common.Ret.fetch(packet.echo, MsgSendRsp)

    async def del_message(self, message_id: int) -> None:
        enid = int(message_id)
        if enid < (1 << 64):
            Packet(
                "delete_msg",
                message_id=enid,
            ).send_to(self.connection)
            logger.info(f"撤回 {message_id}")
            return

        scene, seq, peer_id = msg_deid(enid)
        if scene == 1:
            Packet(
                "recall_group_message",
                group_id=peer_id,
                message_seq=seq
            ).send_to(self.connection)
        else:
            Packet(
                "recall_private_message",
                user_id=peer_id,
                message_seq=seq
            ).send_to(self.connection)
        logger.info(f"撤回 {message_id}")

    async def set_group_kick(self, group_id: int, user_id: int) -> None:
        self._send_with_payload_fallback([
            ("kick_group_member", {
                "group_id": group_id,
                "user_id": user_id,
                "reject_add_request": True
            }),
            ("kick", {
                "group_id": group_id,
                "user_id": user_id,
                "reject_add_request": True
            }),
            ("set_group_kick", {
                "group_id": group_id,
                "user_id": user_id,
                "reject_add_request": True
            }),
            ("set_group_kick", {
                "group_id": group_id,
                "user_id": user_id
            }),
        ])
        logger.info(f"将用户 {user_id} 移出群 {group_id}")

    async def set_group_ban(self, group_id: int, user_id: int, duration: int = 60) -> None:
        self._send_with_payload_fallback([
            ("set_group_member_mute", {
                "group_id": group_id,
                "user_id": user_id,
                "duration": duration
            }),
            ("mute", {
                "group_id": group_id,
                "user_id": user_id,
                "duration": duration
            }),
            ("set_group_ban", {
                "group_id": group_id,
                "user_id": user_id,
                "duration": duration
            }),
        ])
        logger.info(f"在群 {group_id} 将用户 {user_id} 禁言 {duration}s")

    async def get_login_info(self) -> common.Ret[GetLoginInfoRsp]:
        packet = Packet("get_login_info")
        packet.send_to(self.connection)
        return common.Ret.fetch(packet.echo, GetLoginInfoRsp)

    async def get_version_info(self) -> common.Ret[GetVerInfoRsp]:
        packet = Packet("get_impl_info")
        res = packet.send_to(self.connection)
        if isinstance(res, dict) and isinstance(res.get("data"), dict):
            data = res["data"]
            data["app_name"] = data.get("impl_name", "")
            data["app_version"] = data.get("impl_version", "")
            data["protocol_version"] = data.get("milky_version", "")
        return common.Ret.fetch(packet.echo, GetVerInfoRsp)

    async def send_forward_msg(self, message: common.Message) -> common.Ret[SendForwardRsp]:
        ...

    async def get_forward_msg(self, sid: str) -> common.Ret[common.Message]:
        ...

    async def forward_solve(self, message: common.Message) -> common.Message:
        ...

    async def send_group_forward_msg(self, group_id: int, message: common.Message) -> common.Ret[SendGrpForwardRsp]:
        ...

    async def set_group_add_request(self, flag: str, sub_type: str, approve: bool,
                                    reason: str = "Not Mentioned") -> None:
        ...

    async def get_stranger_info(self, user_id: int) -> common.Ret[GetStrInfoRsp]:
        packet, res = self._send_with_endpoint_fallback(
            ["get_user_profile", "profile", "get_stranger_info"],
            user_id=user_id
        )
        if isinstance(res, dict) and isinstance(res.get("data"), dict):
            data = res["data"]
            data["user_id"] = data.get("user_id") or data.get("userId") or data.get("uid") or int(user_id)
            data["nickname"] = data.get("nickname") or data.get("nick") or data.get("name") or ""
            data["sex"] = data.get("sex") or "unknown"
            try:
                data["age"] = int(data.get("age") or data.get("qage") or data.get("qq_age") or 0)
            except (TypeError, ValueError):
                data["age"] = 0
        return common.Ret.fetch(packet.echo, GetStrInfoRsp)

    async def get_group_member_info(self, group_id: int, user_id: int) -> common.Ret[GetGrpMemInfoRsp]:
        packet = Packet(
            "get_group_member_info",
            group_id=group_id,
            user_id=user_id
        )
        packet.send_to(self.connection)
        return common.Ret.fetch(packet.echo, GetGrpMemInfoRsp)

    async def get_group_info(self, group_id: int) -> common.Ret[GetGrpInfoRsp]:
        packet = Packet(
            "get_group_info",
            group_id=group_id
        )
        packet.send_to(self.connection)
        return common.Ret.fetch(packet.echo, GetGrpInfoRsp)

    async def get_status(self) -> common.Ret:
        packet = Packet("get_status")
        packet.send_to(self.connection)
        return common.Ret.fetch(packet.echo)

    async def set_essence_msg(self, message_id: int) -> None:
        enid = int(message_id)
        calls: list[tuple[str, dict]] = []
        if enid >= (1 << 64):
            scene, seq, peer_id = msg_deid(enid)
            if scene == 1:
                calls.extend([
                    ("set_group_essence_message", {
                        "group_id": int(peer_id),
                        "message_seq": int(seq),
                        "is_set": True
                    }),
                    ("set_group_essence_message", {
                        "group_id": int(peer_id),
                        "message_seq": int(seq)
                    }),
                ])
        calls.extend([
            ("set_essence_msg", {"message_id": enid}),
        ])
        self._send_with_payload_fallback(calls)

    async def set_group_special_title(self, group_id: int, user_id: int, title: str) -> None:
        self._send_with_payload_fallback([
            ("set_group_member_special_title", {
                "group_id": group_id,
                "user_id": user_id,
                "special_title": title
            }),
            ("set_group_special_title", {
                "group_id": group_id,
                "user_id": user_id,
                "special_title": title
            }),
            ("set_group_special_title", {
                "group_id": group_id,
                "user_id": user_id,
                "title": title
            }),
        ])

    async def get_msg(self, msg_id: int) -> common.Ret[GetMsgRsp]:
        req_calls: list[tuple[str, dict]] = []
        enid = int(msg_id)
        if enid >= (1 << 64):
            scene, seq, peer_id = msg_deid(enid)
            if scene == 1:
                req_calls.append(("get_message", {
                    "message_scene": "group",
                    "peer_id": int(peer_id),
                    "message_seq": int(seq)
                }))
            else:
                req_calls.extend([
                    ("get_message", {
                        "message_scene": "friend",
                        "peer_id": int(peer_id),
                        "message_seq": int(seq)
                    }),
                    ("get_message", {
                        "message_scene": "private",
                        "peer_id": int(peer_id),
                        "message_seq": int(seq)
                    }),
                ])
        req_calls.extend([
            ("get_message", {"message_id": enid}),
            ("get_msg", {"message_id": enid}),
        ])
        packet, res = self._send_with_payload_fallback(req_calls)
        if isinstance(res, dict) and isinstance(res.get("data"), dict):
            data = res["data"]
            if isinstance(data.get("message"), dict):
                message_data = data["message"]
                for key in ("time", "message_scene", "peer_id", "message_seq", "sender_id", "segments", "friend", "group_member"):
                    if data.get(key) is None and message_data.get(key) is not None:
                        data[key] = message_data.get(key)
            scene_value = data.get("message_scene") or data.get("scene") or data.get("message_type")
            if scene_value in ("friend", "private", 0, "0"):
                scene = 0
                message_type = "private"
            elif scene_value in ("group", 1, "1"):
                scene = 1
                message_type = "group"
            else:
                scene = None
                message_type = data.get("message_type")

            peer_id = data.get("peer_id") or data.get("group_id") or data.get("user_id")
            message_seq = data.get("message_seq") or data.get("seq") or data.get("real_id")
            if data.get("message_id") is None and scene is not None and message_seq is not None and peer_id is not None:
                data["message_id"] = int(msg_enid(scene, int(message_seq), int(peer_id)))

            data["real_id"] = data.get("real_id") or int(message_seq or 0)
            data["time"] = data.get("time") or data.get("timestamp") or int(time.time())
            if message_type is not None:
                data["message_type"] = message_type

            if data.get("sender") is None:
                if message_type == "group":
                    sender = data.get("group_member") or data.get("member") or {}
                    data["sender"] = {
                        "user_id": int(data.get("sender_id") or data.get("user_id") or 0),
                        "nickname": sender.get("nickname") or sender.get("name") or "",
                        "card": sender.get("card") or "",
                        "sex": sender.get("sex") or "unknown",
                        "age": 0,
                        "area": "",
                        "level": str(sender.get("level") or ""),
                        "role": sender.get("role") or "member",
                        "title": sender.get("title") or ""
                    }
                else:
                    sender = data.get("friend") or data.get("sender") or {}
                    data["sender"] = {
                        "user_id": int(data.get("sender_id") or data.get("user_id") or 0),
                        "nickname": sender.get("nickname") or sender.get("name") or "",
                        "sex": sender.get("sex") or "unknown",
                        "age": 0
                    }

            if data.get("message") is None:
                segments_data = data.get("segments")
                milky_segments = consume_segments(segments_data)
                if len(milky_segments) > 0:
                    if scene is None:
                        scene = 1 if message_type == "group" else 0
                    data["message"] = message_translator(milky_segments, int(peer_id or 0), int(scene))
        return common.Ret.fetch(packet.echo, GetMsgRsp)

    async def send_callback(self, group_id: int, bot_id: int, data: dict) -> None:
        ...


async def tester(
        message_data: Union[Event, HyperNotify], actions: Actions
) -> None:
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


connection: MilkyHttpConnection


def run() -> NoReturn:
    global connection, listener_ran
    listener_ran = True
    try:
        if handler is tester:
            raise errors.ListenerNotRegisteredError("No handler registered")
        conn_config = config.get_connection("Milky")
        if isinstance(conn_config, configurator.BotWSC):
            connection = MilkyHttpConnection(
                f"ws://{conn_config.host}:{conn_config.port}",
                auth=getattr(conn_config, "auth", None)
            )
        elif isinstance(conn_config, configurator.BotHTTPC):
            connection = MilkyHttpConnection(
                f"ws://{conn_config.host}:{conn_config.port}",
                auth=getattr(conn_config, "auth", None)
            )
        retried = 0

        while True:
            try:
                connection.connect()
            except (ConnectionRefusedError, TimeoutError):
                if retried >= conn_config.retries:
                    logger.critical(f"重试次数达到最大值({conn_config.retries})，退出")
                    break

                logger.warning(f"连接建立失败，3秒后重试({retried}/{conn_config.retries})")
                retried += 1
                time.sleep(3)
                continue
            retried = 0
            logger.info(f"成功在 {connection.url} 建立连接")
            actions = Actions(connection)
            data = HyperListenerStartNotify(
                time_now=int(time.time()),
                notify_type="listener_start",
                connection=connection
            )
            threading.Thread(target=lambda: __handler(data, actions), daemon=True).start()
            while True:
                try:
                    data = connection.recv()
                except ConnectionResetError:
                    logger.error("连接断开")
                    break
                except json.decoder.JSONDecodeError:
                    logger.error("收到错误的JSON内容")
                    continue
                threading.Thread(target=lambda: __handler(data, actions), daemon=True).start()
    except KeyboardInterrupt:
        logger.warning("正在退出(Ctrl+C)")
        try:
            connection.close()
        except:
            pass
        sys.exit()


def stop() -> None:
    ...
