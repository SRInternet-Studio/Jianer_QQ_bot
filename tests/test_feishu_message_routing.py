import json
from types import SimpleNamespace

from jianer.LecAdapters.FeishuLib.translator import build_hyper_event

from bot import protocol


def test_lark_placeholder_mention_is_normalized_and_restored():
    payload = {
        "header": {
            "event_type": "im.message.receive_v1",
            "create_time": "1786853716993",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_sender"},
                "name": "Tester",
            },
            "message": {
                "message_id": "om_message",
                "chat_id": "oc_group",
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps(
                    {"text": "@_user_1 \u200b~帮助"},
                    ensure_ascii=False,
                ),
                "mentions": [
                    {
                        "key": "@_user_1",
                        "id": {"open_id": "ou_bot"},
                        "name": "Jianer",
                    }
                ],
            },
        },
    }

    translated = build_hyper_event(payload, "ou_bot")
    raw_text = translated["message"][0]["data"]["text"]
    config = SimpleNamespace(protocol="feishu")
    event = SimpleNamespace(is_mentioned=False)

    assert raw_text == "@_user_1 \u200b~帮助"
    assert protocol.normalize_group_message_text(config, raw_text) == "~帮助"
    assert protocol.restore_feishu_mention_flag(config, event, raw_text) is True
    assert event.is_mentioned is True


def test_feishu_mention_restore_does_not_affect_other_messages_or_protocols():
    regular_event = SimpleNamespace(is_mentioned=False)
    assert protocol.restore_feishu_mention_flag(
        SimpleNamespace(protocol="feishu"),
        regular_event,
        "~帮助",
    ) is False
    assert regular_event.is_mentioned is False

    qq_event = SimpleNamespace(is_mentioned=False)
    assert protocol.restore_feishu_mention_flag(
        SimpleNamespace(protocol="onebot"),
        qq_event,
        "@_user_1 ~帮助",
    ) is False
    assert qq_event.is_mentioned is False
