import asyncio
from types import SimpleNamespace

from jianer import common, segments
from jianer.utils.apiresponse import GetMsgRsp

from plugins.AdvancedQuote import AdvancedQuote


def _typed_get_msg_response():
    return SimpleNamespace(
        data=GetMsgRsp(
            {
                "time": 1_786_853_716,
                "message_type": "group",
                "message_id": "om-quoted",
                "real_id": "om-quoted",
                "sender": {
                    "user_id": "ou-feishu-user",
                    "nickname": "ou-feishu-user",
                    "card": "",
                    "sex": "unknown",
                    "age": 0,
                    "area": "",
                    "level": "",
                    "role": "member",
                    "title": "",
                },
                "message": [
                    {"type": "text", "data": {"text": "飞书名言"}}
                ],
            }
        )
    )


def test_response_message_accepts_typed_get_msg_rsp():
    message = AdvancedQuote.response_message(_typed_get_msg_response())

    assert isinstance(message, common.Message)
    assert str(message) == "飞书名言"


def test_get_message_response_normalizes_official_feishu_message_shape():
    class Client:
        def get_message(self, message_id):
            assert message_id == "om-quoted"
            return {
                "message_id": message_id,
                "msg_type": "text",
                "chat_id": "oc-group",
                "body": {"content": '{"text":"飞书原始引用"}'},
                "sender": {
                    "id": "ou-feishu-user",
                    "id_type": "open_id",
                    "sender_type": "user",
                },
            }

    actions = SimpleNamespace(protocol="feishu", client=Client())
    response = asyncio.run(
        AdvancedQuote.get_message_response(actions, "om-quoted")
    )

    assert str(response.data.message) == "飞书原始引用"
    assert response.data.sender.user_id == "ou-feishu-user"


def test_handle_uses_feishu_profile_and_non_qq_avatar(monkeypatch):
    captured = {}

    class Actions:
        async def get_stranger_info(self, user_id):
            assert user_id == "ou-feishu-user"
            return SimpleNamespace(
                data=SimpleNamespace(nickname="飞书用户", card="")
            )

    async def fake_get_image(quote_text, avatar, name):
        captured.update(
            quote_text=quote_text,
            avatar=avatar,
            name=name,
        )
        return "D:/tmp/advanced-quote.png"

    monkeypatch.setattr(AdvancedQuote, "get_image", fake_get_image)
    image = asyncio.run(
        AdvancedQuote.handle(
            [segments.Reply("om-quoted")],
            Actions(),
            Manager=common,
            Segments=segments,
            content=_typed_get_msg_response(),
        )
    )

    assert image.file == "D:/tmp/advanced-quote.png"
    assert captured["quote_text"] == "飞书名言"
    assert captured["name"] == "飞书用户"
    assert captured["avatar"].startswith("data:image/svg+xml")
