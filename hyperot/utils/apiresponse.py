from abc import ABC, abstractmethod
from typing import Union, Literal

from ..events import PrivateSender, GroupSender, gen_message
from ..common import Message


class BaseResponse(ABC):
    def __init__(self, json_data: Union[dict, str]):
        self.raw = json_data
        if json_data:
            # getattr(self, "inner_build")(json_data)
            self.inner_build(json_data)

    def inner_build(self, json_data: Union[dict, str]):
        ...

    @classmethod
    def build(cls, *args, **kwargs):
        ...


class MsgSendRsp(BaseResponse):
    message_id: int = 0

    def inner_build(self, json_data: dict):
        self.message_id = json_data["message_id"]

    @classmethod
    def build(cls, message_id: int):
        return cls({"message_id": message_id})


class GetLoginInfoRsp(BaseResponse):
    user_id: int = 0
    nickname: str = ""

    def inner_build(self, json_data: dict):
        if not isinstance(json_data, dict):
            return
        self.user_id = json_data.get("user_id") or json_data.get("userId") or self.user_id
        self.nickname = json_data.get("nickname") or json_data.get("nick") or self.nickname

    @classmethod
    def build(cls, user_id: int, nickname: str):
        return cls({"user_id": user_id, "nickname": nickname})


class GetVerInfoRsp(BaseResponse):
    app_name: str = ""
    app_version: str = ""
    protocol_version: str = ""

    def inner_build(self, json_data: dict):
        if not isinstance(json_data, dict):
            return
        self.app_name = json_data.get("app_name") or json_data.get("appName") or json_data.get("name") or self.app_name
        self.app_version = json_data.get("app_version") or json_data.get("appVersion") or json_data.get("version") or self.app_version
        self.protocol_version = json_data.get("protocol_version") or json_data.get("protocolVersion") or json_data.get("protocol") or self.protocol_version

    @classmethod
    def build(cls, app_name: str, app_version: str, protocol_version: str):
        return cls({"app_name": app_name, "app_version": app_version, "protocol_version": protocol_version})


class SendForwardRsp(BaseResponse):
    res_id: str

    def inner_build(self, json_data: str):
        self.res_id = str(json_data)

    @classmethod
    def build(cls, res_id: str):
        return cls(res_id)


class SendGrpForwardRsp(BaseResponse):
    message_id: int
    forward_id: str

    def inner_build(self, json_data: dict):
        self.message_id = json_data["message_id"]
        self.forward_id = json_data["forward_id"]

    @classmethod
    def build(cls, message_id: int, forward_id: str):
        return cls({"message_id": message_id, "forward_id": forward_id})


class GetStrInfoRsp(BaseResponse):
    user_id: int
    nickname: str
    sex: str
    age: int

    def inner_build(self, json_data: dict):
        self.user_id = json_data["user_id"]
        self.nickname = json_data["nickname"]
        self.sex = json_data["sex"]
        self.age = json_data["age"]

    @classmethod
    def build(cls, user_id: int, nickname: str, sex: str, age: int):
        return cls({"user_id": user_id, "nickname": nickname, "sex": sex, "age": age})


class GetGrpMemInfoRsp(BaseResponse):
    group_id: int
    user_id: int
    nickname: str
    card: str
    sex: str
    age: str
    area: str
    join_time: int
    last_sent_time: int
    level: str
    role: Literal["owner", "admin", "member"]
    unfriendly: bool
    title: str
    title_expire_time: int
    card_changeable: bool

    def inner_build(self, json_data: dict):
        self.group_id = json_data["group_id"]
        self.user_id = json_data["user_id"]
        self.nickname = json_data["nickname"]
        self.card = json_data["card"]
        self.sex = json_data["sex"]
        self.age = json_data["age"]
        self.area = json_data["area"]
        self.join_time = json_data["join_time"]
        self.last_sent_time = json_data["last_sent_time"]
        self.level = json_data["level"]
        self.role = json_data["role"]
        self.unfriendly = json_data["unfriendly"]
        self.title = json_data["title"]
        self.title_expire_time = json_data["title_expire_time"]
        self.card_changeable = json_data["card_changeable"]

    @classmethod
    def build(cls, **kwargs):
        return cls(**kwargs)


class GetGrpInfoRsp(BaseResponse):
    group_id: int
    group_name: str
    member_count: int
    max_member_count: int

    def inner_build(self, json_data: dict):
        self.group_id = json_data["group_id"]
        self.group_name = json_data["group_name"]
        self.member_count = json_data["member_count"]
        self.max_member_count = json_data["max_member_count"]

    @classmethod
    def build(cls, group_id: int, group_name: str, member_count: int, max_member_count: int):
        return cls(
            {
                "group_id": group_id,
                "group_name": group_name,
                "member_count": member_count,
                "max_member_count": max_member_count
            }
        )


class GetMsgRsp(BaseResponse):
    time: int
    message_type: Literal["private", "group"]
    message_id: int
    real_id: int
    sender: Union[PrivateSender, GroupSender]
    message: Message

    def inner_build(self, json_data: dict):
        self.time = json_data["time"]
        self.message_type = json_data["message_type"]
        self.message_id = json_data["message_id"]
        self.real_id = json_data["real_id"]
        self.sender = GroupSender(
            json_data["sender"]
        ) if self.message_type == "group" else PrivateSender(
            json_data["sender"]
        )
        self.message = gen_message(json_data)

    @classmethod
    def build(
            cls, time: int, message_type: Literal["private", "group"], message_id: int, real_id: int, sender: dict,
            message: dict
    ):
        return cls(
            {
                "time": time,
                "message_type": message_type,
                "message_id": message_id,
                "real_id": real_id,
                "sender": sender,
                "message": message
            }
        )
