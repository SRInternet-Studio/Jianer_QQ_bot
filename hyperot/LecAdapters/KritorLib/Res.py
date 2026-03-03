from ...LecAdapters.KritorLib.protos.event import (
    EventServiceStub,
    RequestPushEvent,
    EventType,
    NoticeEvent as NoticeProto,
    NoticeEventNoticeType,
    GroupMemberDecreasedNoticeGroupMemberDecreasedType,
    GroupMemberIncreasedNoticeGroupMemberIncreasedType
)
from ...LecAdapters.KritorLib.protos.common import (
    ElementElementType,
    PushMessageBody,
    Scene, Element,
    TextElement,
    ReplyElement,
    AtElement,
    VideoElement,
    VoiceElement,
    ImageElement
)
from ...configurator import BotConfig
from ...utils.logic import SimpleQueue

import traceback
import asyncio
from typing import NoReturn, Self
from abc import ABC

message_types = {}

event_queue = SimpleQueue()

message_ids = {}


def to_ob_msg(msg_body: PushMessageBody) -> list:
    msg = []
    for i in msg_body.elements:
        if i.type == ElementElementType.TEXT:
            msg.append(
                {
                    "type": "text",
                    "data": {
                        "text": i.text.text
                    }
                }
            )
        elif i.type == ElementElementType.IMAGE:
            msg.append(
                {
                    "type": "image",
                    "data": {
                        "url": i.image.file_url,
                        "file": i.image.file_url
                    }
                }
            )
        elif i.type == ElementElementType.AT:
            msg.append(
                {
                    "type": "at",
                    "data": {
                        "qq": i.at.uin
                    }
                }
            )
        elif i.type == ElementElementType.REPLY:
            try:
                msg.append(
                    {
                        "type": "reply",
                        "data": {
                            "id": message_ids[i.reply.message_id]
                        }
                    }
                )
            except KeyError:
                pass
        elif i.type == ElementElementType.VIDEO:
            msg.append(
                {
                    "type": "video",
                    "data": {
                        "file": i.video.file_url,
                        "url": i.video.file_url,
                    }
                }
            )
        elif i.type == ElementElementType.VOICE:
            msg.append(
                {
                    "type": "record",
                    "data": {
                        "file": i.voice.file_url,
                        "url": i.voice.file_url,
                    }
                }
            )
        elif i.type == ElementElementType.JSON:
            msg.append(
                {
                    "type": "json",
                    "data": {
                        "data": i.json.json
                    }
                }
            )

    return msg


def to_protos(body: list) -> list:
    elems = []

    for i in body:
        if i["type"] == "text":
            elems.append(
                Element(
                    type=ElementElementType.TEXT,
                    text=TextElement(
                        text=i["data"]["text"]
                    )
                )
            )
        elif i["type"] == "image":
            elems.append(
                Element(
                    type=ElementElementType.IMAGE,
                    image=ImageElement(
                        file_url=i["data"]["file"]
                    )
                )
            )
        elif i["type"] == "video":
            elems.append(
                Element(
                    type=ElementElementType.VIDEO,
                    video=VideoElement(
                        file_url=i["data"]["file"]
                    )
                )
            )
        elif i["type"] == "record":
            elems.append(
                Element(
                    type=ElementElementType.VOICE,
                    voice=VoiceElement(
                        file_url=i["data"]["file"]
                    )
                )
            )
        elif i["type"] == "at":
            elems.append(
                Element(
                    type=ElementElementType.AT,
                    at=AtElement(
                        uin=int(i["data"]["qq"])
                    )
                )
            )
        elif i["type"] == "reply":
            try:
                try:
                    mid = list(filter(lambda x: message_ids[x] == int(i["data"]["id"]), message_ids))[1]
                except IndexError:
                    mid = list(filter(lambda x: message_ids[x] == int(i["data"]["id"]), message_ids))[0]

                elems.append(
                    Element(
                        type=ElementElementType.REPLY,
                        reply=ReplyElement(
                            message_id=mid
                        )
                    )
                )
            except:
                traceback.print_exc()

    return elems


class OneBotEventJsonBuilder:
    class BaseBuilder:
        def __init__(self, data: dict):
            self.data = data

        def message(self, message: PushMessageBody) -> Self:
            self.data["post_type"] = "message"
            self.data["sub_type"] = None
            self.data["message"] = to_ob_msg(message)
            self.data["message_id"] = message_ids[message.message_id]

            return self

        def group(self, message: PushMessageBody) -> Self:
            self.data["message_type"] = "group"
            self.data["sub_type"] = "normal"
            sender = {
                "user_id": self.data["user_id"],
                "nickname": message.group.nick,
                "sex": "unknown",
                "age": 0,
                "card": message.group.nick,
                "area": "unknown",
                "level": "0",
                "role": "unknown",
                "title": None
            }
            self.data["sender"] = sender
            self.data["anonymous"] = None

            return self

        def private(self, message: PushMessageBody) -> Self:
            self.data["message_type"] = "private"
            self.data["sub_type"] = "friend"
            sender = {
                "user_id": message.private.uin,
                "nickname": message.private.nick,
                "sex": "unknown",
                "age": 0
            }
            self.data["sender"] = sender

            return self

        def notice(self, ev: NoticeProto) -> Self:
            self.data["post_type"] = "notice"
            self.data["notice_type"] = None

            return self

        def group_admin(self, ev: NoticeProto) -> Self:
            self.data["notice_type"] = "group_admin"
            try:
                self.data["sub_type"] = "set" if ev.group_admin_changed.is_admin else "unset"
            except:
                self.data["sub_type"] = "unset"

            return self

        def group_decrease(self, ev: NoticeProto) -> Self:
            self.data["notice_type"] = "group_decrease"
            sub_map = {
                GroupMemberDecreasedNoticeGroupMemberDecreasedType.KICK: "kick",
                GroupMemberDecreasedNoticeGroupMemberDecreasedType.KICK_ME: "kick_me",
                GroupMemberDecreasedNoticeGroupMemberDecreasedType.LEAVE: "leave"
            }
            self.data["sub_type"] = sub_map[ev.group_member_decrease.type]
            self.data["operator_id"] = ev.group_member_decrease.operator_uin

            return self

        def group_increase(self, ev: NoticeProto) -> Self:
            self.data["notice_type"] = "group_increase"
            sub_map = {
                GroupMemberIncreasedNoticeGroupMemberIncreasedType.INVITE: "invite",
                GroupMemberIncreasedNoticeGroupMemberIncreasedType.APPROVE: "approve",
            }
            self.data["sub_type"] = sub_map[ev.group_member_increase.type]
            try:
                self.data["operator_id"] = ev.group_member_increase.operator_uin
            except:
                self.data["operator_id"] = None

            return self

    def __init__(self, self_id: int):
        self.self_id = self_id

    def get_base(self, time: int, user_id: int, group_id: int) -> BaseBuilder:
        return OneBotEventJsonBuilder.BaseBuilder(
            {
                "time": time,
                "self_id": self.self_id,
                "user_id": user_id,
                "group_id": group_id,
                "post_type": None
            }
        )


class SegmentBase(ABC):
    def __init__(self, *args, **kwargs):
        var = self.__var
        anns = self.__anns
        arg = {}
        if len(args) > 0:
            for i in args:
                arg[list(anns.keys())[list(args).index(i)]] = i

        if len(kwargs) > 0:
            for i in kwargs:
                try:
                    arg[i] = anns[i](kwargs[i])
                except TypeError:
                    arg[i] = kwargs[i]
        new_arg = arg.copy()

        if len(anns) > len(arg):
            for i in anns.keys():
                if i not in arg.keys():
                    if i not in var.keys():
                        new_arg[i] = None
                        continue
                    if not isinstance(var[i], anns[i]):
                        new_arg[i] = anns[i](var[i])
                    else:
                        new_arg[i] = var[i]

        for i in new_arg:
            setattr(self, i, new_arg[i])

    def __init_subclass__(cls, **kwargs):
        sg_type = kwargs.get("sg_type") or kwargs.get("st")
        summary_tmp = kwargs.get("summary_tmp") or kwargs.get("su")

        if sg_type is summary_tmp is None:
            return

        cls.__sg_type = sg_type
        cls.__var = dict(vars(cls))
        cls.__anns: dict = cls.__var.get("__annotations__", False) or dict()

        def to_str(self) -> str:
            text = summary_tmp
            if text is None:
                text = "[]"
            if "<" not in text and ">" not in text:
                return text

            for i in cls.__anns:
                if f"<{i}>" in summary_tmp:
                    try:
                        v = self.__getattribute__(i)
                    except AttributeError:
                        v = None
                    text = text.replace(f"<{i}>", str(v))

            return text

        cls.__str__ = to_str if cls().__str__() == "__not_set__" else cls.__str__

        message_types[sg_type] = {
            "type": cls,
            "args": list(cls.__anns.keys()),
        }

        return cls

    def to_json(self) -> dict:
        base = {"type": self.__sg_type, "data": {}}
        for i in self.__anns:
            if i.startswith("__") or getattr(self, i) is None:
                continue
            if not isinstance(getattr(self, i), self.__anns[i]):
                base["data"][i] = self.__anns[i](getattr(self, i))
            else:
                base["data"][i] = getattr(self, i)
        return base

    def __str__(self) -> str:
        return "__not_set__"

    def __eq__(self, other) -> bool:
        if type(self) is type(other) and self.to_json() == other.to_json():
            return True
        else:
            return False

    def __ne__(self, other) -> bool:
        if type(self) is type(other) and self.to_json() == other.to_json():
            return False
        else:
            return True


class EventService:
    def __init__(self, stub: EventServiceStub):
        self.stub = stub
        self.uin: int = BotConfig.get("hyper-bot").uin

    async def core_service(self) -> NoReturn:
        while 1:
            try:
                async for i in self.stub.register_active_listener(
                        RequestPushEvent(type=EventType.EVENT_TYPE_CORE_EVENT)
                ):
                    print(i)
                    # print(i.to_json())
            except EOFError:
                continue

    async def message_service(self) -> NoReturn:
        while 1:
            try:
                async for i in self.stub.register_active_listener(
                        RequestPushEvent(type=EventType.EVENT_TYPE_MESSAGE)
                ):
                    message_ids[i.message.message_id] = len(message_ids)
                    if i.message.scene == Scene.GROUP:  # Try to solve a bug caused by 坏黑兔子
                        message_ids[
                            i.message.message_id
                            .replace("p", "g0")
                            .replace(str(i.message.group.uin), str(i.message.group.group_id))
                        ] = message_ids[i.message.message_id]

                    ev = (
                        OneBotEventJsonBuilder(self.uin)
                        .get_base(
                            time=i.message.time, user_id=i.message.group.uin, group_id=int(i.message.group.group_id)
                        )
                        .message(message=i.message)
                    )
                    if i.message.scene == Scene.GROUP:
                        ev = ev.group(i.message).data
                    elif i.message.scene == Scene.FRIEND:
                        ev = ev.private(i.message).data
                    else:
                        print(i)
                        continue

                    # print(ev)
                    event_queue.put(ev)

            except EOFError:
                continue

    async def notice_service(self) -> NoReturn:
        while 1:
            try:
                async for i in self.stub.register_active_listener(
                        RequestPushEvent(type=EventType.EVENT_TYPE_NOTICE)
                ):
                    ev = OneBotEventJsonBuilder(self.uin)
                    if i.notice.type == NoticeEventNoticeType.GROUP_MEMBER_DECREASE:
                        ev = (
                            ev.get_base(
                                time=i.notice.time,
                                user_id=i.notice.group_member_decrease.target_uin,
                                group_id=i.notice.group_member_decrease.group_id
                            )
                            .notice(ev=i.notice)
                            .group_decrease(ev=i.notice)
                        ).data
                    elif i.notice.type == NoticeEventNoticeType.GROUP_MEMBER_INCREASE:
                        ev = (
                            ev.get_base(
                                time=i.notice.time,
                                user_id=i.notice.group_member_increase.target_uin,
                                group_id=i.notice.group_member_increase.group_id
                            )
                            .notice(ev=i.notice)
                            .group_increase(ev=i.notice)
                        ).data
                    else:
                        print(i)
                        continue  # not implemented

                    event_queue.put(ev)

            except EOFError:
                continue

    async def request_service(self) -> NoReturn:
        while 1:
            try:
                async for i in self.stub.register_active_listener(
                        RequestPushEvent(type=EventType.EVENT_TYPE_REQUEST)
                ):
                    print(i)
                    # print(i.to_json())
            except EOFError:
                continue

    async def run(self) -> NoReturn:
        tasks = [
            asyncio.create_task(self.core_service()),
            asyncio.create_task(self.message_service()),
            asyncio.create_task(self.notice_service()),
            asyncio.create_task(self.request_service())
        ]

        await asyncio.gather(*tasks)
