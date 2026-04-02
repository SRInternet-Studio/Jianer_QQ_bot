import httpx
import json
import os

from hyperot.network import WebsocketConnection
from ...utils.logic import Matcher
from ...adapters.obuilder import OneBotEventBuilder, OneBotJsonMessageBuilder
from .types import (
    MilkyForwardNode,
    MilkyOutgoingSegment,
    MilkySegment,
    consume_friend_entity,
    consume_group_member_entity,
    consume_milky_event,
    consume_segments,
    make_face_segment,
    make_forward_node,
    make_forward_segment,
    make_image_segment,
    make_mention_all_segment,
    make_mention_segment,
    make_record_segment,
    make_reply_segment,
    make_text_segment,
    make_video_segment,
    normalize_scene,
)


def msg_enid(scene: int, seq: int, peer_id: int) -> int:
    # For scene: friend: 0, group: 1
    return (scene << 128) | (seq << 64) | peer_id


def msg_deid(enid: int) -> tuple[int, int, int]:
    scene = (enid >> 128) & 0xFFFF
    seq = (enid >> 64) & 0xFFFFFFFF
    peer_id = enid & 0xFFFFFFFFFFFFFFFF
    return scene, seq, peer_id


def normalize_uri(uri: str | None) -> str | None:
    if uri is None:
        return None
    raw = str(uri).strip()
    if len(raw) == 0:
        return raw
    lower = raw.lower()
    if lower.startswith("file://") or lower.startswith("http://") or lower.startswith("https://") or lower.startswith("base64://"):
        return raw
    if len(raw) >= 3 and raw[1] == ":" and (raw[2] == "\\" or raw[2] == "/"):
        path = raw.replace("\\", "/")
        while "//" in path:
            path = path.replace("//", "/")
        if not path.startswith("/"):
            path = "/" + path
        return f"file://{path}"
    if "\\" in raw or "/" in raw:
        path = os.path.abspath(raw).replace("\\", "/")
        while "//" in path:
            path = path.replace("//", "/")
        if not path.startswith("/"):
            path = "/" + path
        return f"file://{path}"
    return raw


def message_translator(milky_message: list[MilkySegment], peer_id: int, scene: int = 0) -> list[dict]:
    builder = OneBotJsonMessageBuilder()
    for seg in milky_message:
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
            seq = seg_data.get("message_seq") or seg_data.get("seq")
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
            milky_event = consume_milky_event(milky_rp)
            if milky_event is None:
                continue
            milky_event_type = milky_event["type"]
            milky_time = milky_event["time"]
            milky_self_id = milky_event["self_id"]
            milky_data = milky_event["data"]
            ma = Matcher(milky_event_type).match
            builder = OneBotEventBuilder()
            if ma("bot_offline"):
                raise Exception("Bot offline")
            if not ma("message_receive"):
                continue

            milky_segments = consume_segments(milky_data.get("segments") or milky_data.get("message"))
            if len(milky_segments) == 0:
                continue

            message_scene = normalize_scene(milky_data.get("message_scene"))
            if message_scene is None:
                continue

            sender_id = milky_data.get("sender_id") or milky_data.get("user_id")
            peer_id = milky_data.get("peer_id") or sender_id
            message_seq = milky_data.get("message_seq") or milky_data.get("seq")
            if sender_id is None or peer_id is None or message_seq is None:
                continue

            if message_scene == "friend":
                friend = consume_friend_entity(milky_data.get("friend") or milky_data.get("sender"))
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
                group_member = consume_group_member_entity(milky_data.get("group_member") or milky_data.get("member"))
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
            self.segments: list[MilkyOutgoingSegment] = []

        def text(self, text: str) -> 'MilkyOutGoingSegBuilder':
            self.segments.append(make_text_segment(text))
            return self

        def mention(self, user_id: int) -> 'MilkyOutGoingSegBuilder':
            self.segments.append(make_mention_segment(user_id))
            return self

        def mention_all(self) -> 'MilkyOutGoingSegBuilder':
            self.segments.append(make_mention_all_segment())
            return self

        def face(self, face_id: str) -> 'MilkyOutGoingSegBuilder':
            self.segments.append(make_face_segment(face_id))
            return self

        def reply(self, seq: int) -> 'MilkyOutGoingSegBuilder':
            self.segments.append(make_reply_segment(seq))
            return self

        def image(self, uri: str, summary: str = "[Image]", sub_type: str = "normal") -> 'MilkyOutGoingSegBuilder':
            normalized_uri = normalize_uri(uri) or ""
            self.segments.append(make_image_segment(normalized_uri, summary, sub_type))
            return self

        def record(self, uri: str) -> "MilkyOutGoingSegBuilder":
            normalized_uri = normalize_uri(uri) or ""
            self.segments.append(make_record_segment(normalized_uri))
            return self

        def video(self, uri: str, thumb_uri: str = None) -> "MilkyOutGoingSegBuilder":
            normalized_uri = normalize_uri(uri) or ""
            normalized_thumb_uri = normalize_uri(thumb_uri)
            self.segments.append(make_video_segment(normalized_uri, normalized_thumb_uri))
            return self

        @staticmethod
        def outgoing_forward(user_id: int, sender_name: str, segments: list[MilkySegment]) -> dict:
            return make_forward_node(user_id, sender_name, segments)

        def forward(self, messages: list[MilkyForwardNode]) -> "MilkyOutGoingSegBuilder":
            self.segments.append(make_forward_segment(messages))
            return self

        def build(self) -> list[MilkyOutgoingSegment]:
            return self.segments


MilkyOutGoingSegBuilder = MilkyHttpConnection.MilkyOutGoingSegBuilder
