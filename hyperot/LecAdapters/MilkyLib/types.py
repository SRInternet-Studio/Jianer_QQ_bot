from typing import Any, Literal, TypedDict, Union, cast


MilkyScene = Literal["friend", "private", "group", 0, 1, "0", "1"]
MilkySceneNormalized = Literal["friend", "group"]
MilkySegmentType = Literal[
    "text",
    "image",
    "mention",
    "mention_all",
    "reply",
    "face",
    "record",
    "video",
    "forward",
    "market_face",
]


class MilkyTextSegData(TypedDict):
    text: str


class MilkyImageSegData(TypedDict, total=False):
    temp_url: str
    url: str
    uri: str
    file: str
    summary: str
    sub_type: str


class MilkyMentionSegData(TypedDict):
    user_id: int


class MilkyReplySegData(TypedDict, total=False):
    message_seq: int
    seq: int
    message_id: int
    id: int


class MilkyFaceSegData(TypedDict, total=False):
    face_id: str
    id: str


class MilkyRecordSegData(TypedDict, total=False):
    temp_url: str
    url: str
    uri: str
    file: str


class MilkyVideoSegData(TypedDict, total=False):
    temp_url: str
    url: str
    uri: str
    file: str
    thumb_uri: str


class MilkyForwardNode(TypedDict):
    user_id: int
    sender_name: str
    segments: list["MilkySegment"]


class MilkyForwardSegData(TypedDict):
    messages: list[MilkyForwardNode]


class MilkyMarketFaceSegData(TypedDict, total=False):
    id: str


class MilkyTextSegment(TypedDict):
    type: Literal["text"]
    data: MilkyTextSegData


class MilkyImageSegment(TypedDict):
    type: Literal["image"]
    data: MilkyImageSegData


class MilkyMentionSegment(TypedDict):
    type: Literal["mention"]
    data: MilkyMentionSegData


class MilkyMentionAllSegment(TypedDict):
    type: Literal["mention_all"]
    data: dict[str, Any]


class MilkyReplySegment(TypedDict):
    type: Literal["reply"]
    data: MilkyReplySegData


class MilkyFaceSegment(TypedDict):
    type: Literal["face"]
    data: MilkyFaceSegData


class MilkyRecordSegment(TypedDict):
    type: Literal["record"]
    data: MilkyRecordSegData


class MilkyVideoSegment(TypedDict):
    type: Literal["video"]
    data: MilkyVideoSegData


class MilkyForwardSegment(TypedDict):
    type: Literal["forward"]
    data: MilkyForwardSegData


class MilkyMarketFaceSegment(TypedDict):
    type: Literal["market_face"]
    data: MilkyMarketFaceSegData


MilkySegment = Union[
    MilkyTextSegment,
    MilkyImageSegment,
    MilkyMentionSegment,
    MilkyMentionAllSegment,
    MilkyReplySegment,
    MilkyFaceSegment,
    MilkyRecordSegment,
    MilkyVideoSegment,
    MilkyForwardSegment,
    MilkyMarketFaceSegment,
]

MilkyOutgoingSegment = MilkySegment
IncomingSegment = MilkySegment
OutgoingSegment = MilkyOutgoingSegment


class IncomingForwardedMessage(TypedDict, total=False):
    user_id: int
    sender_name: str
    segments: list[IncomingSegment]


OutgoingForwardedMessage = MilkyForwardNode


class MilkyFriendEntity(TypedDict, total=False):
    nickname: str
    name: str
    sex: str


class FriendCategoryEntity(TypedDict, total=False):
    category_id: int
    category_name: str
    friends: list["FriendEntity"]


class GroupEntity(TypedDict, total=False):
    group_id: int
    group_name: str
    member_count: int
    max_member_count: int


class MilkyGroupMemberEntity(TypedDict, total=False):
    nickname: str
    name: str
    sex: str
    card: str
    level: Union[str, int]
    role: str
    title: str


class GroupAnnouncementEntity(TypedDict, total=False):
    sender_id: int
    publish_time: int
    content: str
    image_url: str


class GroupFileEntity(TypedDict, total=False):
    file_id: str
    file_name: str
    busid: int
    size: int
    upload_time: int
    modify_time: int
    dead_time: int
    download_times: int
    uploader_id: int
    uploader_name: str


class GroupFolderEntity(TypedDict, total=False):
    folder_id: str
    folder_name: str
    create_time: int
    creator_id: int
    creator_name: str
    total_file_count: int


class FriendRequest(TypedDict, total=False):
    request_id: str
    user_id: int
    nickname: str
    comment: str
    source: str
    time: int


class GroupNotification(TypedDict, total=False):
    notification_id: str
    group_id: int
    operator_id: int
    user_id: int
    subtype: str
    comment: str
    flag: str
    time: int


class GroupEssenceMessage(TypedDict, total=False):
    group_id: int
    sender_id: int
    operator_id: int
    message_id: int
    sub_type: str
    time: int


class MilkyMessageReceiveData(TypedDict, total=False):
    message_scene: MilkyScene
    sender_id: int
    user_id: int
    peer_id: int
    message_seq: int
    seq: int
    segments: list[MilkySegment]
    message: list[MilkySegment]
    friend: MilkyFriendEntity
    sender: MilkyFriendEntity
    group_member: MilkyGroupMemberEntity
    member: MilkyGroupMemberEntity


class MilkyEvent(TypedDict):
    type: str
    time: int
    self_id: int
    data: MilkyMessageReceiveData


Event = MilkyEvent
FriendEntity = MilkyFriendEntity
GroupMemberEntity = MilkyGroupMemberEntity
IncomingMessage = MilkyMessageReceiveData


__all__ = [
    "Event",
    "FriendEntity",
    "FriendCategoryEntity",
    "GroupEntity",
    "GroupMemberEntity",
    "GroupAnnouncementEntity",
    "GroupFileEntity",
    "GroupFolderEntity",
    "FriendRequest",
    "GroupNotification",
    "IncomingMessage",
    "IncomingForwardedMessage",
    "GroupEssenceMessage",
    "IncomingSegment",
    "OutgoingForwardedMessage",
    "OutgoingSegment",
    "MilkyScene",
    "MilkySceneNormalized",
    "MilkySegmentType",
    "MilkyTextSegData",
    "MilkyImageSegData",
    "MilkyMentionSegData",
    "MilkyReplySegData",
    "MilkyFaceSegData",
    "MilkyRecordSegData",
    "MilkyVideoSegData",
    "MilkyForwardNode",
    "MilkyForwardSegData",
    "MilkyMarketFaceSegData",
    "MilkyTextSegment",
    "MilkyImageSegment",
    "MilkyMentionSegment",
    "MilkyMentionAllSegment",
    "MilkyReplySegment",
    "MilkyFaceSegment",
    "MilkyRecordSegment",
    "MilkyVideoSegment",
    "MilkyForwardSegment",
    "MilkyMarketFaceSegment",
    "MilkySegment",
    "MilkyOutgoingSegment",
    "MilkyFriendEntity",
    "MilkyGroupMemberEntity",
    "MilkyMessageReceiveData",
    "MilkyEvent",
    "consume_segment",
    "consume_segments",
    "consume_friend_entity",
    "consume_group_member_entity",
    "normalize_scene",
    "consume_milky_event",
    "make_text_segment",
    "make_mention_segment",
    "make_mention_all_segment",
    "make_face_segment",
    "make_reply_segment",
    "make_image_segment",
    "make_record_segment",
    "make_video_segment",
    "make_forward_node",
    "make_forward_segment",
]


def _as_dict(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return cast(dict[str, Any], data)
    return {}


def consume_segment(seg: Any) -> MilkySegment | None:
    if not isinstance(seg, dict):
        return None
    seg_type = seg.get("type")
    seg_data = seg.get("data")
    if not isinstance(seg_type, str) or not isinstance(seg_data, dict):
        return None
    if seg_type not in {
        "text",
        "image",
        "mention",
        "mention_all",
        "reply",
        "face",
        "record",
        "video",
        "forward",
        "market_face",
    }:
        return None
    return cast(MilkySegment, {"type": seg_type, "data": seg_data})


def consume_segments(raw: Any) -> list[MilkySegment]:
    if not isinstance(raw, list):
        return []
    segments: list[MilkySegment] = []
    for seg in raw:
        item = consume_segment(seg)
        if item is not None:
            segments.append(item)
    return segments


def consume_friend_entity(raw: Any) -> MilkyFriendEntity:
    data = _as_dict(raw)
    return cast(MilkyFriendEntity, data)


def consume_group_member_entity(raw: Any) -> MilkyGroupMemberEntity:
    data = _as_dict(raw)
    return cast(MilkyGroupMemberEntity, data)


def normalize_scene(scene: Any) -> MilkySceneNormalized | None:
    if scene in ("friend", "private", 0, "0"):
        return "friend"
    if scene in ("group", 1, "1"):
        return "group"
    return None


def consume_milky_event(raw: Any) -> MilkyEvent | None:
    if not isinstance(raw, dict):
        return None
    packet = cast(dict[str, Any], raw.copy())
    if "type" not in packet:
        body = packet.get("body")
        if isinstance(body, dict) and "type" in body:
            packet = cast(dict[str, Any], body.copy())
        elif isinstance(body, dict) and "event_type" in body:
            packet = cast(dict[str, Any], body.copy())
            packet["type"] = packet["event_type"]
        elif "event_type" in packet:
            packet["type"] = packet["event_type"]
        else:
            return None
    event_type = packet.get("type")
    event_time = packet.get("time")
    self_id = packet.get("self_id")
    data = packet.get("data")
    if not isinstance(event_type, str):
        return None
    if not isinstance(data, dict):
        return None
    try:
        event_time_i = int(event_time)
        self_id_i = int(self_id)
    except (TypeError, ValueError):
        return None
    event_data = cast(MilkyMessageReceiveData, data)
    return {
        "type": event_type,
        "time": event_time_i,
        "self_id": self_id_i,
        "data": event_data,
    }


def make_text_segment(text: str) -> MilkyTextSegment:
    return {"type": "text", "data": {"text": text}}


def make_mention_segment(user_id: int) -> MilkyMentionSegment:
    return {"type": "mention", "data": {"user_id": user_id}}


def make_mention_all_segment() -> MilkyMentionAllSegment:
    return {"type": "mention_all", "data": {}}


def make_face_segment(face_id: str) -> MilkyFaceSegment:
    return {"type": "face", "data": {"face_id": face_id}}


def make_reply_segment(seq: int) -> MilkyReplySegment:
    return {"type": "reply", "data": {"message_seq": seq}}


def make_image_segment(uri: str, summary: str = "[Image]", sub_type: str = "normal") -> MilkyImageSegment:
    return {
        "type": "image",
        "data": {
            "uri": uri,
            "summary": summary,
            "sub_type": sub_type,
        },
    }


def make_record_segment(uri: str) -> MilkyRecordSegment:
    return {"type": "record", "data": {"uri": uri}}


def make_video_segment(uri: str, thumb_uri: str | None = None) -> MilkyVideoSegment:
    data: MilkyVideoSegData = {"uri": uri}
    if thumb_uri is not None:
        data["thumb_uri"] = thumb_uri
    return {"type": "video", "data": data}


def make_forward_node(user_id: int, sender_name: str, segments: list[MilkySegment]) -> MilkyForwardNode:
    return {
        "user_id": user_id,
        "sender_name": sender_name,
        "segments": segments,
    }


def make_forward_segment(messages: list[MilkyForwardNode]) -> MilkyForwardSegment:
    return {"type": "forward", "data": {"messages": messages}}
