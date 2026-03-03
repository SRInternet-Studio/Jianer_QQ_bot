from ..utils.hypetyping import Any, NoReturn, TypeVar, Callable
from ..utils.apiresponse import *
from ..events import *
from .. import events, common, segments
from ..utils import errors

from .MilkyLib.translator import MilkyHttpConnection, msg_deid, msg_enid, MilkyOutGoingSegBuilder
from .MilkyLib.Manager import Packet

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
                    endpoint = str(item)
                    payload = dict(kwargs)
                    adapt = None

                    if endpoint == "send_like":
                        endpoint = "send_profile_like"
                        payload = {
                            "user_id": int(payload.get("user_id")),
                            "count": int(payload.get("count") or payload.get("times") or 1),
                        }
                    elif endpoint == "set_group_special_title":
                        endpoint = "set_group_member_special_title"
                        payload = {
                            "group_id": int(payload.get("group_id")),
                            "user_id": int(payload.get("user_id")),
                            "special_title": payload.get("special_title") or payload.get("title") or "",
                        }
                    elif endpoint == "set_group_leave":
                        endpoint = "quit_group"
                        payload = {
                            "group_id": int(payload.get("group_id")),
                        }
                    elif endpoint == "get_stranger_info":
                        endpoint = "get_user_profile"
                        payload = {
                            "user_id": int(payload.get("user_id")),
                        }
                        adapt = "user_profile"

                    packet = Packet(endpoint, **payload)
                    res = packet.send_to(self.connection)
                    if adapt == "user_profile" and isinstance(res, dict) and isinstance(res.get("data"), dict):
                        profile = res["data"]
                        res["data"] = {
                            "user_id": int(payload.get("user_id")),
                            "nickname": profile.get("nickname") or "",
                            "sex": profile.get("sex") or "unknown",
                            "age": int(profile.get("age") or 0),
                        }
                    return packet.echo

                return wrapper

        self.custom = CustomAction(self.connection)

    async def send(
            self, message: Union[common.Message, str], group_id: int = None, user_id: int = None
    ) -> common.Ret[MsgSendRsp]:
        if isinstance(message, str):
            message = common.Message(segments.Text(message))
        outgoing: list[dict] = []
        for seg in message:
            if hasattr(seg, "milky_outgoing_seg"):
                outgoing.append(seg.milky_outgoing_seg())
            else:
                outgoing.append({"type": "text", "data": {"text": str(seg)}})
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
        Packet(
            "kick_group_member",
            group_id=group_id,
            user_id=user_id,
            reject_add_request=False,
        ).send_to(self.connection)
        logger.info(f"将用户 {user_id} 移出群 {group_id}")

    async def set_group_ban(self, group_id: int, user_id: int, duration: int = 60) -> None:
        Packet(
            "set_group_member_mute",
            group_id=group_id,
            user_id=user_id,
            duration=duration,
        ).send_to(self.connection)
        logger.info(f"在群 {group_id} 将用户 {user_id} 禁言 {duration}s")

    async def get_login_info(self) -> common.Ret[GetLoginInfoRsp]:
        packet = Packet("get_login_info")
        res = packet.send_to(self.connection)
        if isinstance(res, dict) and isinstance(res.get("data"), dict):
            data = res["data"]
            if "user_id" not in data and "uin" in data:
                data["user_id"] = data.get("uin")
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
        owner = int(config.owner[0]) if getattr(config, "owner", None) else None
        if owner is None:
            raise errors.ArgsInvalidError("'send_forward_msg' requires config.owner")

        forwarded = MilkyOutGoingSegBuilder.outgoing_forward(
            user_id=int(config.uin or owner),
            sender_name=str(getattr(config, "others", {}).get("bot_name", "bot")),
            segments=[segments.Text(str(message)).milky_outgoing_seg()],
        )
        forward_seg = MilkyOutGoingSegBuilder().forward([forwarded]).build()[0]

        packet = Packet("send_private_message", user_id=owner, message=[forward_seg])
        res = packet.send_to(self.connection)
        if isinstance(res, dict) and isinstance(res.get("data"), dict):
            data = res["data"]
            seq = data.get("message_seq")
            if seq is not None:
                res["data"] = str(seq)
        return common.Ret.fetch(packet.echo, SendForwardRsp)

    async def get_forward_msg(self, sid: str) -> common.Ret[common.Message]:
        ...

    async def forward_solve(self, message: common.Message) -> common.Message:
        ...

    async def send_group_forward_msg(self, group_id: int, message: common.Message) -> common.Ret[SendGrpForwardRsp]:
        def normalize_node(node_obj) -> dict | None:
            if not hasattr(node_obj, "to_json"):
                return None
            try:
                data = node_obj.to_json()
            except Exception:
                return None
            if not isinstance(data, dict) or data.get("type") != "node":
                return None
            node_data = data.get("data") or {}
            if not isinstance(node_data, dict):
                return None
            return node_data

        def build_outgoing_from_onebot_json(content: list[dict]) -> list[dict]:
            outgoing: list[dict] = []
            if not isinstance(content, list):
                return outgoing
            for s in content:
                if not isinstance(s, dict):
                    continue
                st = s.get("type")
                sd = s.get("data") or {}
                if st in segments.message_types and isinstance(sd, dict):
                    meta = segments.message_types[st]
                    cls = meta["type"]
                    args = [sd.get(k) for k in meta.get("args", [])]
                    try:
                        seg_obj = cls(*args)
                    except Exception:
                        seg_obj = segments.Text(str(s))
                    if hasattr(seg_obj, "milky_outgoing_seg"):
                        outgoing.append(seg_obj.milky_outgoing_seg())
                    else:
                        outgoing.append({"type": "text", "data": {"text": str(seg_obj)}})
                else:
                    outgoing.append({"type": "text", "data": {"text": str(s)}})
            return outgoing

        forwarded_messages: list[dict] = []
        for seg in message:
            node = normalize_node(seg)
            if node is None:
                continue
            user_id = int(node.get("user_id") or node.get("uin") or config.uin or 0)
            sender_name = str(node.get("sender_name") or node.get("nickname") or node.get("nick_name") or "")
            content = node.get("content")
            outgoing_segments = build_outgoing_from_onebot_json(content) if isinstance(content, list) else []
            if not outgoing_segments:
                outgoing_segments = [segments.Text(str(seg)).milky_outgoing_seg()]
            forwarded_messages.append(
                MilkyOutGoingSegBuilder.outgoing_forward(
                    user_id=user_id,
                    sender_name=sender_name,
                    segments=outgoing_segments,
                )
            )

        if not forwarded_messages:
            forwarded_messages = [
                MilkyOutGoingSegBuilder.outgoing_forward(
                    user_id=int(config.uin or 0),
                    sender_name=str(getattr(config, "others", {}).get("bot_name", "bot")),
                    segments=[segments.Text(str(message)).milky_outgoing_seg()],
                )
            ]

        forward_seg = MilkyOutGoingSegBuilder().forward(forwarded_messages).build()[0]
        packet = Packet("send_group_message", group_id=int(group_id), message=[forward_seg])
        res = packet.send_to(self.connection)
        if isinstance(res, dict) and isinstance(res.get("data"), dict):
            data = res["data"]
            seq = data.get("message_seq")
            if seq is not None:
                data["message_id"] = msg_enid(1, int(seq), int(group_id))
            if "forward_id" not in data:
                data["forward_id"] = ""
        return common.Ret.fetch(packet.echo, SendGrpForwardRsp)

    async def set_group_add_request(self, flag: str, sub_type: str, approve: bool,
                                    reason: str = "Not Mentioned") -> None:
        ...

    async def get_stranger_info(self, user_id: int) -> common.Ret[GetStrInfoRsp]:
        packet = Packet(
            "get_user_profile",
            user_id=user_id
        )
        res = packet.send_to(self.connection)
        if isinstance(res, dict) and isinstance(res.get("data"), dict):
            profile = res["data"]
            res["data"] = {
                "user_id": int(user_id),
                "nickname": profile.get("nickname") or "",
                "sex": profile.get("sex") or "unknown",
                "age": int(profile.get("age") or 0),
            }
        return common.Ret.fetch(packet.echo, GetStrInfoRsp)

    async def get_group_member_info(self, group_id: int, user_id: int) -> common.Ret[GetGrpMemInfoRsp]:
        packet = Packet(
            "get_group_member_info",
            group_id=group_id,
            user_id=user_id
        )
        res = packet.send_to(self.connection)
        if isinstance(res, dict) and isinstance(res.get("data"), dict):
            data = res["data"]
            member = data.get("member") if isinstance(data.get("member"), dict) else data
            res["data"] = {
                "group_id": int(group_id),
                "user_id": int(user_id),
                "nickname": member.get("nickname") or "",
                "card": member.get("card") or "",
                "sex": member.get("sex") or "unknown",
                "age": int(member.get("age") or 0),
                "area": member.get("area") or "",
                "join_time": int(member.get("join_time") or 0),
                "last_sent_time": int(member.get("last_sent_time") or 0),
                "level": str(member.get("level") or ""),
                "role": member.get("role") or "member",
                "unfriendly": bool(member.get("unfriendly") or False),
                "title": member.get("title") or "",
                "title_expire_time": int(member.get("title_expire_time") or 0),
                "card_changeable": bool(member.get("card_changeable") or False),
            }
        return common.Ret.fetch(packet.echo, GetGrpMemInfoRsp)

    async def get_group_info(self, group_id: int) -> common.Ret[GetGrpInfoRsp]:
        packet = Packet(
            "get_group_info",
            group_id=group_id
        )
        res = packet.send_to(self.connection)
        if isinstance(res, dict) and isinstance(res.get("data"), dict):
            data = res["data"]
            group = data.get("group") if isinstance(data.get("group"), dict) else data
            res["data"] = {
                "group_id": int(group.get("group_id") or group_id),
                "group_name": group.get("group_name") or "",
                "member_count": int(group.get("member_count") or 0),
                "max_member_count": int(group.get("max_member_count") or 0),
            }
        return common.Ret.fetch(packet.echo, GetGrpInfoRsp)

    async def get_status(self) -> common.Ret:
        packet = Packet("get_status")
        packet.send_to(self.connection)
        return common.Ret.fetch(packet.echo)

    async def set_essence_msg(self, message_id: int) -> None:
        Packet(
            "set_essence_msg",
            message_id=int(message_id)
        ).send_to(self.connection)

    async def set_group_special_title(self, group_id: int, user_id: int, title: str) -> None:
        Packet(
            "set_group_member_special_title",
            group_id=group_id,
            user_id=user_id,
            special_title=title
        ).send_to(self.connection)

    async def get_msg(self, msg_id: int) -> common.Ret[GetMsgRsp]:
        packet = Packet(
            "get_msg",
            message_id=int(msg_id)
        )
        packet.send_to(self.connection)
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
        if isinstance(config.connection, configurator.BotWSC):
            connection = MilkyHttpConnection(
                f"ws://{config.connection.host}:{config.connection.port}",
                auth=getattr(config.connection, "auth", None)
            )
        elif isinstance(config.connection, configurator.BotHTTPC):
            connection = MilkyHttpConnection(
                f"ws://{config.connection.host}:{config.connection.port}",
                auth=getattr(config.connection, "auth", None)
            )
        retried = 0

        while True:
            try:
                connection.connect()
            except (ConnectionRefusedError, TimeoutError):
                if retried >= config.connection.retries:
                    logger.critical(f"重试次数达到最大值({config.connection.retries})，退出")
                    break

                logger.warning(f"连接建立失败，3秒后重试({retried}/{config.connection.retries})")
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
