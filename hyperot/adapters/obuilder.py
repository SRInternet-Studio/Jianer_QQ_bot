from ..utils.hypetyping import Literal, Union

import json


class OneBotEventBuilder:
    def __init__(self):
        self.data = dict()

    def init(self, time: int, self_id: int, user_id: int, group_id: int) -> "OneBotEventBuilder":
        self.data["time"] = time
        self.data["self_id"] = self_id
        self.data["user_id"] = user_id
        self.data["group_id"] = group_id
        return self

    def _as_message(self, message: list, message_id: str) -> "OneBotEventBuilder":
        self.data["post_type"] = "message"
        self.data["message"] = message
        self.data["message_id"] = message_id
        return self

    def as_group_message(self, message: list, message_id: str, sub_type: str = "normal") -> "OneBotEventBuilder":
        self.data["message_type"] = "group"
        self.data["sub_type"] = sub_type
        return self._as_message(message, message_id)

    def as_private_message(self, message: list, message_id: str, sub_type: str = "friend") -> "OneBotEventBuilder":
        self.data["message_type"] = "private"
        self.data["sub_type"] = sub_type
        return self._as_message(message, message_id)

    def group_sender(self, nickname: str, sex: str, age: int, card: str, area: str, level: str, role: str, title: str) -> "OneBotEventBuilder":
        self.data["sender"] = {
            "user_id": self.data.get("user_id"),
            "nickname": nickname,
            "sex": sex,
            "age": age,
            "card": card,
            "area": area,
            "level": level,
            "role": role,
            "title": title
        }
        return self

    def private_sender(self, nickname: str, sex: str, age: int) -> "OneBotEventBuilder":
        self.data["sender"] = {
            "user_id": self.data.get("user_id"),
            "nickname": nickname,
            "sex": sex,
            "age": age
        }
        return self

    def _as_notice(self) -> "OneBotEventBuilder":
        self.data["post_type"] = "notice"
        return self

    def as_group_file_upload(self, file: dict) -> "OneBotEventBuilder":
        self.data["notice_type"] = "group_upload"
        self.data["file"] = file
        return self._as_notice()

    def as_group_admin_event(self):
        self.data["notice_type"] = "group_admin"
        return self._as_notice()

    def as_group_decrease_event(self, operator_id: int, sub_type: str = "leave") -> "OneBotEventBuilder":
        self.data["notice_type"] = "group_decrease"
        self.data["operator_id"] = operator_id
        self.data["sub_type"] = sub_type
        return self._as_notice()

    def as_group_increase_event(self, operator_id: int, sub_type: str = "approve") -> "OneBotEventBuilder":
        self.data["notice_type"] = "group_increase"
        self.data["operator_id"] = operator_id
        self.data["sub_type"] = sub_type
        return self._as_notice()

    def as_group_mute_event(self, operator_id: int, duration: int, sub_type: str = "ban") -> "OneBotEventBuilder":
        self.data["notice_type"] = "group_ban"
        self.data["operator_id"] = operator_id
        self.data["duration"] = duration
        self.data["sub_type"] = sub_type
        return self._as_notice()

    def as_friend_add_event(self) -> "OneBotEventBuilder":
        self.data["notice_type"] = "friend_add"
        return self._as_notice()

    def as_group_recall_event(self, operator_id: int, message_id: str) -> "OneBotEventBuilder":
        self.data["notice_type"] = "group_recall"
        self.data["operator_id"] = operator_id
        self.data["message_id"] = message_id
        return self._as_notice()

    def as_private_recall_event(self, message_id: str) -> "OneBotEventBuilder":
        self.data["notice_type"] = "private_recall"
        self.data["message_id"] = message_id
        return self._as_notice()

    def _as_notify(self, notify_type: Literal["poke", "lucky_king", "honor"]) -> "OneBotEventBuilder":
        self.data["post_type"] = "notify"
        self.data["notify_type"] = notify_type
        return self._as_notice()

    def as_poke(self) -> "OneBotEventBuilder":
        self.data["sub_type"] = "poke"
        return self._as_notify("poke")

    def as_lucky(self) -> "OneBotEventBuilder":
        self.data["sub_type"] = "lucky_king"
        return self._as_notify("lucky_king")

    def as_honor(self, honor_type: str) -> "OneBotEventBuilder":
        self.data["sub_type"] = "honor"
        self.data["honor_type"] = honor_type
        return self._as_notify("honor")

    def as_group_essence_event(self, sender_id: int, operator_id: int,  message_id: str) -> "OneBotEventBuilder":
        self.data["notice_type"] = "essence"
        self.data["sender_id"] = sender_id
        self.data["operator_id"] = operator_id
        self.data["message_id"] = message_id
        return self._as_notice()

    def as_group_reaction_event(self, message_id: str, operator_id: int, code: int, count: int) -> "OneBotEventBuilder":
        self.data["notice_type"] = "reaction"
        self.data["message_id"] = message_id
        self.data["operator_id"] = operator_id
        self.data["code"] = code
        self.data["count"] = count
        return self._as_notice()

    def as_bot_online_event(self, reason: str) -> "OneBotEventBuilder":
        self.data["notice_type"] = "bot_online"
        self.data["reason"] = reason
        return self

    def _as_request(self, comment: str, flag: str) -> "OneBotEventBuilder":
        self.data["post_type"] = "request"
        self.data["comment"] = comment
        self.data["flag"] = flag
        return self

    def as_friend_add_request(self, comment: str, flag: str) -> "OneBotEventBuilder":
        self.data["request_type"] = "friend"
        return self._as_request(comment, flag)

    def as_group_add_request(self, comment: str, flag: str, sub_type: str = "add") -> "OneBotEventBuilder":
        self.data["request_type"] = "group"
        self.data["sub_type"] = sub_type
        return self._as_request(comment, flag)

    def build(self) -> dict:
        return self.data


class OneBotJsonMessageBuilder:
    def __init__(self):
        self.message: list = []

    def text(self, text: str) -> "OneBotJsonMessageBuilder":
        self.message.append({
            "type": "text",
            "data": {
                "text": text
            }
        })
        return self

    def image(self, file: str, summary: str = "[Image]") -> "OneBotJsonMessageBuilder":
        self.message.append({
            "type": "image",
            "data": {
                "file": file,
                "url": file,
                "summary": summary
            }
        })
        return self

    def at(self, qq: str):
        self.message.append({
            "type": "at",
            "data": {
                "qq": qq
            }
        })
        return self

    def atall(self):
        self.message.append({
            "type": "at",
            "data": {
                "qq": "all"
            }
        })
        return self

    def reply(self, message_id: str):
        self.message.append({
            "type": "reply",
            "data": {
                "id": message_id
            }
        })
        return self

    def faces(self, face_id: str):
        self.message.append({
            "type": "face",
            "data": {
                "id": face_id
            }
        })
        return self

    def record(self, file: str) -> "OneBotJsonMessageBuilder":
        self.message.append({
            "type": "record",
            "data": {
                "file": file,
                "url": file
            }
        })
        return self

    def video(self, file: str) -> "OneBotJsonMessageBuilder":
        self.message.append({
            "type": "video",
            "data": {
                "file": file,
                "url": file
            }
        })
        return self

    def poke(self, poke_type: str, poke_id: str) -> "OneBotJsonMessageBuilder":
        self.message.append({
            "type": "poke",
            "data": {
                "type": poke_type,
                "id": poke_id
            }
        })
        return self

    def contact(self, contact_type: str, contact_id: str) -> "OneBotJsonMessageBuilder":
        self.message.append({
            "type": "contact",
            "data": {
                "type": contact_type,
                "id": contact_id
            }
        })
        return self

    def forward(self, forward_id: str) -> "OneBotJsonMessageBuilder":
        self.message.append({
            "type": "forward",
            "data": {
                "id": forward_id
            }
        })
        return self

    def json(self, data: Union[dict, list, str]) -> "OneBotJsonMessageBuilder":
        self.message.append({
            "type": "json",
            "data": {
                "data": data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
            }
        })
        return self

    def mface(self, face_id: str, tab_id: str, key: str) -> "OneBotJsonMessageBuilder":
        self.message.append({
            "type": "mface",
            "data": {
                "face_id": face_id,
                "tab_id": tab_id,
                "key": key
            }
        })
        return self

    def dice(self) -> "OneBotJsonMessageBuilder":
        self.message.append({
            "type": "dice",
            "data": {}
        })
        return self

    def rps(self) -> "OneBotJsonMessageBuilder":
        self.message.append({
            "type": "rps",
            "data": {}
        })
        return self

    def music(self, music_type: str, url: str = "", music_id: str = "", title: str = "", audio: str = "") -> "OneBotJsonMessageBuilder":
        self.message.append({
            "type": "music",
            "data": {
                "type": music_type,
                "url": url,
                "audio": audio,
                "title": title,
                "id": music_id
            }
        })
        return self

    def build(self) -> list:
        return self.message
