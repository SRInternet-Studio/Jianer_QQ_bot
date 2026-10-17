"""Contract tests for the native JianerCore RollPigPlus plugin."""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path

from jianer import common, segments

from tests import rollpig_support

rollpig_support.bootstrap()

from plugins.RollPigPlus import UPSTREAM_COMMIT, UPSTREAM_VERSION, PACKAGE_DIR  # noqa: E402
from plugins.RollPigPlus import setup as plugin_setup  # noqa: E402
from plugins.RollPigPlus.host import (  # noqa: E402
    ActionFailed,
    Bot,
    Message,
    MessageSegment,
    Scheduler,
    get_plugin_config,
    lifecycle,
    scheduler,
)


def test_plugin_metadata_and_upstream_pins() -> None:
    meta = plugin_setup.__plugin_meta__
    assert meta.name == "jianerbot-plugin-rollpig-plus"
    assert "jianerbot-plugin-alconna" in meta.requires
    assert "今日小猪" in meta.usage
    assert "烤箱续火" in meta.usage
    assert UPSTREAM_VERSION == "0.14.4"
    assert len(UPSTREAM_COMMIT) == 40
    assert (PACKAGE_DIR / "rollpig" / "resource" / "pig.json").is_file()


def test_plugin_has_no_nonebot_dependency() -> None:
    """The port must stay on the JianerCore plugin API."""

    offenders: list[str] = []
    for path in Path(PACKAGE_DIR).rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import nonebot", "from nonebot")):
                offenders.append(f"{path.relative_to(PACKAGE_DIR)}: {stripped}")
    assert offenders == []
    assert not (Path(PACKAGE_DIR) / "vendor").exists()
    assert not (Path(PACKAGE_DIR) / "bridge.py").exists()


def test_host_message_builder_produces_jianer_segments() -> None:
    message = MessageSegment.reply("7") + "今日小猪"
    message += MessageSegment.image(b"\x89PNG")
    message += MessageSegment.at(123)

    assert isinstance(message, common.Message)
    kinds = [type(item) for item in message.contents]
    assert kinds == [segments.Reply, segments.Text, segments.Image, segments.At]
    assert message.extract_plain_text() == "今日小猪"
    assert message.contents[2].file.startswith("base64://")
    assert isinstance(Message("纯文本"), common.Message)


def test_host_bot_maps_onebot_calls_onto_actions() -> None:
    actions = rollpig_support.Actions()
    bot = Bot("10000", actions)

    result = asyncio.run(bot.send_group_msg(group_id="33333", message="hello"))
    assert result["message_id"]
    sent = actions.sent[0]
    assert sent["group_id"] == "33333"
    assert isinstance(sent["message"], common.Message)
    assert str(sent["message"]) == "hello"

    member = asyncio.run(bot.get_group_member_info(group_id="33333", user_id="22222"))
    assert member["card"] == "名片22222"

    with_action = rollpig_support.Actions()
    try:
        asyncio.run(Bot("10000", object()).call_api("no_such_action"))
    except ActionFailed as error:
        assert "不支持" in str(error)
    else:  # pragma: no cover - the facade must fail closed
        raise AssertionError("unsupported actions must raise ActionFailed")
    assert with_action.sent == []


def test_scheduler_registers_cron_and_interval_jobs() -> None:
    local = Scheduler()

    @local.scheduled_job("cron", hour=23, minute=45, id="daily")
    async def _daily() -> None:  # pragma: no cover - never executed here
        return None

    @local.scheduled_job("interval", hours=24, id="sync")
    async def _sync() -> None:  # pragma: no cover - never executed here
        return None

    daily = local.get_job("daily")
    sync = local.get_job("sync")
    assert daily is not None and sync is not None
    assert daily.trigger == "cron"
    assert daily.next_run.tzinfo is not None
    assert sync.interval_seconds == 86400
    assert sync.next_run > dt.datetime.now(dt.timezone.utc)
    assert {job.job_id for job in local.get_jobs()} == {"daily", "sync"}
    local.clear()
    assert local.get_jobs() == []


def test_plugin_jobs_register_with_the_host_scheduler() -> None:
    """导入插件即注册定时任务与生命周期回调。"""

    jobs = {job.job_id for job in scheduler.get_jobs()}
    assert {
        "rollpig_data_maintenance",
        "rollpig_pighub_refresh",
        "rollpig_resource_sync",
        "rollpig_protection_recovery",
        "rollpig_daily_report",
    } <= jobs

    hooks = lifecycle.registered_hooks()
    assert hooks["startup"]
    assert hooks["shutdown"]
    assert hooks["bot_connect"]


def test_plugin_config_reads_rollpig_environment(monkeypatch) -> None:
    from plugins.RollPigPlus.rollpig.config import Config

    monkeypatch.setenv("ROLLPIG_ROAST_COOLDOWN_HOURS", "3")
    monkeypatch.setenv("ROLLPIG_DAILY_SUMMARY_ENABLED", "true")
    config = get_plugin_config(Config)
    assert config.rollpig_roast_cooldown_hours == 3
    assert config.rollpig_daily_summary_enabled is True


def test_command_boundaries_are_enforced_by_alconna(tmp_path, monkeypatch) -> None:
    from bot import plugin_state

    rollpig_support.install_plugin_folder(tmp_path, monkeypatch)
    result = plugin_state.reload_plugins()
    assert result.failed == []

    handled, actions = rollpig_support.run(rollpig_support.dispatch("~明日小猪"))
    assert handled is True
    assert actions.sent

    # 无参数命令不接受黏连参数，也不会被其他插件误吞。
    handled, actions = rollpig_support.run(rollpig_support.dispatch("~今日小猪测试"))
    assert handled is False
    assert actions.sent == []

    handled, _ = rollpig_support.run(rollpig_support.dispatch("今天天气不错"))
    assert handled is False


def test_superuser_helper_accepts_host_auth_snapshot() -> None:
    from plugins.RollPigPlus.rollpig.helpers import is_superuser_user

    assert is_superuser_user("22222") is True
    assert is_superuser_user(22222) is True
    assert is_superuser_user("44444") is False
