"""End-to-end flows for the native RollPigPlus plugin through the manager."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from tests import rollpig_support

rollpig_support.bootstrap()

DATA_DIR = rollpig_support.TEST_DATA_DIR


def _load_plugin(tmp_path, monkeypatch):
    from bot import plugin_state

    rollpig_support.install_plugin_folder(tmp_path, monkeypatch)
    result = plugin_state.reload_plugins()
    assert result.failed == []
    assert "jianerbot-plugin-rollpig-plus" in result.loaded


def _dispatch(text: str, **event_kwargs):
    return rollpig_support.run(rollpig_support.dispatch(text, **event_kwargs))


def _image_segments(actions):
    from jianer import segments

    return [
        content
        for item in actions.sent
        for content in getattr(item["message"], "contents", [])
        if isinstance(content, segments.Image)
    ]


def test_today_pig_sends_a_card_and_persists_state(tmp_path, monkeypatch) -> None:
    _load_plugin(tmp_path, monkeypatch)
    handled, actions = _dispatch("~今日小猪")

    assert handled is True
    assert actions.sent
    assert "猪" in actions.texts()[0]
    assert _image_segments(actions), "今日小猪必须渲染一张小猪卡片"
    assert list(DATA_DIR.rglob("*.json")), "首次抽猪必须落盘到插件数据目录"


def test_repeated_today_pig_keeps_the_same_pig(tmp_path, monkeypatch) -> None:
    _load_plugin(tmp_path, monkeypatch)
    _, first = _dispatch("~今日小猪")
    _, second = _dispatch("~今日小猪")

    def pig_name(actions) -> str:
        text = actions.texts()[0]
        for line in text.splitlines():
            if "【" in line and "】" in line:
                return line[line.index("【") + 1 : line.index("】")]
        return text

    assert pig_name(first) == pig_name(second)


def test_pigsty_summary_reports_collected_count(tmp_path, monkeypatch) -> None:
    _load_plugin(tmp_path, monkeypatch)
    _dispatch("~今日小猪")
    handled, actions = _dispatch("~我的猪圈")

    assert handled is True
    text = actions.texts()[0]
    assert "我的猪圈统计" in text
    assert "已收集：" in text


def test_roast_today_and_random_roast_answer(tmp_path, monkeypatch) -> None:
    _load_plugin(tmp_path, monkeypatch)
    _dispatch("~今日小猪")

    handled, actions = _dispatch("~今日烤猪")
    assert handled is True
    assert actions.sent, "今日烤猪必须有回复"

    # 随机烤猪在只有自己抽过猪时应提示没有目标。
    handled, actions = _dispatch("~随机烤群友")
    assert handled is True
    assert actions.sent


def test_oven_refill_needs_enough_active_players(tmp_path, monkeypatch) -> None:
    _load_plugin(tmp_path, monkeypatch)
    _dispatch("~今日小猪")
    handled, actions = _dispatch("~烤箱续火")

    assert handled is True
    assert "活跃" in actions.texts()[0] or "补货" in actions.texts()[0]


def test_daily_report_switch_toggles_and_persists(tmp_path, monkeypatch) -> None:
    _load_plugin(tmp_path, monkeypatch)

    _, enabled = _dispatch("~小猪日报 开启")
    assert "已开启群 33333" in enabled.texts()[0]

    _, status = _dispatch("~小猪日报 状态")
    text = status.texts()[0]
    assert "开启" in text and "33333" in text

    _, disabled = _dispatch("~小猪日报 关闭")
    assert "已关闭群 33333" in disabled.texts()[0]

    switch_file = DATA_DIR / "data" / "daily_summary_groups.json"
    payload = json.loads(switch_file.read_text(encoding="utf-8"))
    assert "33333" in payload["disabled_group_ids"]


def test_non_superuser_cannot_sync_resources(tmp_path, monkeypatch) -> None:
    _load_plugin(tmp_path, monkeypatch)
    handled, actions = _dispatch("~同步小猪资源", user_id=44444, role="member")

    assert handled is True
    assert "只有超级用户" in actions.texts()[0]


def test_private_chat_commands_work(tmp_path, monkeypatch) -> None:
    _load_plugin(tmp_path, monkeypatch)
    handled, actions = _dispatch("~明日小猪", group_id=None)

    assert handled is True
    assert str(actions.sent[0]["user_id"]) == "22222"
    assert actions.sent[0]["group_id"] is None


def test_message_hook_ignores_unrelated_replies(tmp_path, monkeypatch) -> None:
    """预约加入钩子只认回复自己的「加入」短语。"""

    _load_plugin(tmp_path, monkeypatch)
    from plugins.RollPigPlus.rollpig.handlers import roast

    payload = [
        {"type": "reply", "data": {"id": "555"}},
        {"type": "text", "data": {"text": "今天吃什么"}},
    ]
    handled, actions = _dispatch(
        "今天吃什么", segments_payload=payload, user_id=22222
    )
    assert handled is False
    assert actions.sent == []

    join_payload = [
        {"type": "reply", "data": {"id": "555"}},
        {"type": "text", "data": {"text": "加入"}},
    ]
    event = rollpig_support.group_event("加入", segments_payload=join_payload)
    assert roast._is_reservation_reply(event) is True


def test_startup_hooks_are_wired_to_the_scheduler(tmp_path, monkeypatch) -> None:
    """首次连接后启动定时任务；网络相关的 startup 钩子在测试里被替换。"""

    from bot import plugin_state
    from plugins.RollPigPlus.host import scheduler
    from plugins.RollPigPlus.rollpig import jobs
    from plugins.RollPigPlus.rollpig.pighub_service import pighub_service

    rollpig_support.install_plugin_folder(tmp_path, monkeypatch)
    assert plugin_state.reload_plugins().failed == []

    async def scenario():
        actions = rollpig_support.Actions()
        await plugin_state.observe_plugins(
            rollpig_support.group_event("~明日小猪"), actions, message_text="~明日小猪"
        )
        return actions

    with patch.object(jobs, "schedule_background_resource_sync"), patch.object(
        jobs, "schedule_background_maintenance"
    ), patch.object(jobs, "schedule_daily_report_recovery"), patch.object(
        jobs, "schedule_protection_recovery"
    ), patch.object(type(pighub_service), "schedule_startup_refresh"):
        actions = asyncio.run(scenario())
        assert scheduler.get_jobs()
        asyncio.run(scheduler.shutdown())

    assert actions.sent == []
