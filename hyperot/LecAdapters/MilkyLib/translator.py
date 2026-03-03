import httpx
import json

from hyperot.network import WebsocketConnection
from ...utils.logic import Matcher
from ...adapters.obuilder import OneBotEventBuilder, OneBotJsonMessageBuilder


def msg_enid(scene: int, seq: int, peer_id: int) -> int:
    # For scene: friend: 0, group: 1
    return (scene << 128) | (seq << 64) | peer_id


def msg_deid(enid: int) -> tuple[int, int, int]:
    scene = (enid >> 128) & 0xFFFF
    seq = (enid >> 64) & 0xFFFFFFFF
    peer_id = enid & 0xFFFFFFFFFFFFFFFF
    return scene, seq, peer_id


def message_translator(milky_message: list[dict], peer_id: int, scene: int = 0) -> list[dict]:
    builder = OneBotJsonMessageBuilder()
    for seg in milky_message:
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type")
        seg_data = seg.get("data") or {}
        if not seg_type or not isinstance(seg_data, dict):
            continue
        ma = Matcher(seg_type).match
        if ma("text"):
            builder.text(seg_data.get("text", ""))
        elif ma("image"):
            file = seg_data.get("temp_url") or seg_data.get("url") or seg_data.get("uri") or seg_data.get("file")
            if file:
                builder.image(file=file, summary=seg_data.get("summary", "[Image]"))
        elif ma("mention"):
            builder.at(str(seg_data.get("user_id", "")))
        elif ma("mention_all"):
            builder.at("all")
        elif ma("reply"):
            seq = seg_data.get("message_seq")
            if seq is None:
                seq = seg_data.get("seq")
            if seq is not None:
                builder.reply(message_id=str(msg_enid(scene, int(seq), peer_id)))
            else:
                message_id = seg_data.get("message_id") or seg_data.get("id")
                if message_id is not None:
                    builder.reply(message_id=str(message_id))
        elif ma("face"):
            face_id = seg_data.get("face_id") or seg_data.get("id")
            if face_id is not None:
                builder.faces(face_id=str(face_id))
        elif ma("record"):
            file = seg_data.get("temp_url") or seg_data.get("url") or seg_data.get("uri") or seg_data.get("file")
            if file:
                builder.record(file=file)
        elif ma("video"):
            file = seg_data.get("temp_url") or seg_data.get("url") or seg_data.get("uri") or seg_data.get("file")
            if file:
                builder.video(file=file)
        elif ma("forward"):
            forward_id = seg_data.get("forward_id") or seg_data.get("id")
            if forward_id is not None:
                builder.forward(forward_id=str(forward_id))
        elif ma("market_face"):
            continue
        else:
            continue

    return builder.build()


class MilkyHttpConnection(WebsocketConnection):
    def connect(self) -> None:
        if self.auth:
            self.ws.connect(self.url + "/event", header={"Authorization": "Bearer " + self.auth})
        else:
            self.ws.connect(self.url + "/event")

    def recv(self) -> dict:
        while True:
            raw = self.ws.recv()
            try:
                milky_rp = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(milky_rp, dict):
                continue
            if "type" not in milky_rp:
                body = milky_rp.get("body")
                if isinstance(body, dict) and "type" in body:
                    milky_rp = body
                elif isinstance(body, dict) and "event_type" in body:
                    body["type"] = body["event_type"]
                    milky_rp = body
                elif "event_type" in milky_rp:
                    milky_rp["type"] = milky_rp["event_type"]
                else:
                    continue
            try:
                milky_event_type = milky_rp["type"]
                milky_time = milky_rp["time"]
                milky_self_id = milky_rp["self_id"]
                milky_data = milky_rp["data"]
            except KeyError:
                continue
            ma = Matcher(milky_event_type).match
            builder = OneBotEventBuilder()
            if ma("bot_offline"):
                raise Exception("Bot offline")
            if not ma("message_receive"):
                continue

            milky_segments = milky_data.get("segments") or milky_data.get("message")
            if not isinstance(milky_segments, list):
                continue

            scene_val = milky_data.get("message_scene")
            if scene_val in ("friend", "private", 0, "0"):
                message_scene = "friend"
            elif scene_val in ("group", 1, "1"):
                message_scene = "group"
            else:
                continue

            sender_id = milky_data.get("sender_id") or milky_data.get("user_id")
            peer_id = milky_data.get("peer_id") or sender_id
            message_seq = milky_data.get("message_seq") or milky_data.get("seq")
            if sender_id is None or peer_id is None or message_seq is None:
                continue

            if message_scene == "friend":
                friend = milky_data.get("friend") or milky_data.get("sender") or {}
                nickname = friend.get("nickname") or friend.get("name") or str(sender_id)
                sex = friend.get("sex") or "unknown"
                return builder \
                    .init(milky_time, milky_self_id, int(sender_id), 0) \
                    .as_private_message(
                        message_translator(milky_segments, int(peer_id), 0),
                        str(msg_enid(0, int(message_seq), int(peer_id)))
                    ) \
                    .private_sender(nickname, sex, 0) \
                    .build()
            elif message_scene == "group":
                group_member = milky_data.get("group_member") or milky_data.get("member") or {}
                nickname = group_member.get("nickname") or group_member.get("name") or str(sender_id)
                sex = group_member.get("sex") or "unknown"
                card = group_member.get("card") or ""
                level = str(group_member.get("level") or "")
                role = group_member.get("role") or "member"
                title = group_member.get("title") or ""
                return builder \
                    .init(milky_time, milky_self_id, int(sender_id), int(peer_id)) \
                    .as_group_message(
                        message_translator(milky_segments, int(peer_id), 1),
                        str(msg_enid(1, int(message_seq), int(peer_id)))
                    ) \
                    .group_sender(
                        nickname,
                        sex,
                        0,
                        card,
                        "",
                        level,
                        role,
                        title,
                    ) \
                    .build()
            else:
                continue

    def http_send(self, endpoint: str, data: dict) -> dict:
        if not data:
            data = dict()
        base_url = self.url
        if base_url.startswith("ws://"):
            base_url = "http://" + base_url[len("ws://"):]
        elif base_url.startswith("wss://"):
            base_url = "https://" + base_url[len("wss://"):]
        if self.auth:
            response = httpx.post(f"{base_url}/api/{endpoint}", json=data,
                                  headers={"Authorization": f"Bearer {self.auth}"})
        else:
            response = httpx.post(f"{base_url}/api/{endpoint}", json=data)
        res = response.json()
        return res

    class MilkyOutGoingSegBuilder:
        def __init__(self) -> None:
            self.segments: list[dict] = []

        def text(self, text: str) -> 'MilkyOutGoingSegBuilder':
            self.segments.append({
                "type": "text",
                "data": {
                    "text": text
                }
            })
            return self

        def mention(self, user_id: int) -> 'MilkyOutGoingSegBuilder':
            self.segments.append({
                "type": "mention",
                "data": {
                    "user_id": user_id
                }
            })
            return self

        def mention_all(self) -> 'MilkyOutGoingSegBuilder':
            self.segments.append({
                "type": "mention_all",
                "data": {}
            })
            return self

        def face(self, face_id: str) -> 'MilkyOutGoingSegBuilder':
            self.segments.append({
                "type": "face",
                "data": {
                    "face_id": face_id
                }
            })
            return self

        def reply(self, message_seq: int) -> 'MilkyOutGoingSegBuilder':
            self.segments.append({
                "type": "reply",
                "data": {
                    "message_seq": message_seq
                }
            })
            return self

        def image(self, uri: str, summary: str = "[Image]", sub_type: str = "normal") -> 'MilkyOutGoingSegBuilder':
            self.segments.append({
                "type": "image",
                "data": {
                    "uri": uri,
                    "summary": summary,
                    "sub_type": sub_type
                }
            })
            return self

        def record(self, uri: str) -> "MilkyOutGoingSegBuilder":
            self.segments.append({
                "type": "record",
                "data": {
                    "uri": uri
                }
            })
            return self

        def video(self, uri: str, thumb_uri: str = None) -> "MilkyOutGoingSegBuilder":
            self.segments.append({
                "type": "video",
                "data": {
                    "uri": uri,
                    "thumb_uri": thumb_uri
                }
            })
            return self

        @staticmethod
        def outgoing_forward(user_id: int, sender_name: str, segments: list[dict]) -> dict:
            return {
                "user_id": user_id,
                "sender_name": sender_name,
                "segments": segments
            }

        def forward(self, messages: list[dict]) -> "MilkyOutGoingSegBuilder":
            self.segments.append({
                "type": "forward",
                "data": {
                    "messages": messages
                }
            })
            return self

        def build(self) -> list[dict]:
            return self.segments


MilkyOutGoingSegBuilder = MilkyHttpConnection.MilkyOutGoingSegBuilder
