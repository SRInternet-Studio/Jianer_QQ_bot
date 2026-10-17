"""猪圈日报查询与推送开关（JianerCore 原生实现）。"""

from __future__ import annotations

from jianer import common, segments
from jianer.plugins.builtin.alconna import Command

from bot import plugin_state

from ..data_manager import LocalStoreUnavailableError
from ..helpers import (
    command_tail,
    get_event_group_id,
    guard_store_errors,
    has_extra_text,
    image_message,
    is_superuser_user,
    reply,
    reply_to,
)
from ...host import bot_for, logger
from ..jobs import latest_daily_report_query_date, render_latest_daily_report_query_card
from ..runtime import get_daily_report_group_status, set_daily_report_group_enabled
from ..store.cloud import CloudStoreError

_REMINDER = str(plugin_state.get_runtime().get("reminder", ""))

ENABLE_WORDS = {"开启", "打开", "启用", "开", "on", "enable", "true"}
DISABLE_WORDS = {"关闭", "停用", "关", "off", "disable", "false"}
STATUS_WORDS = {"状态", "查看", "查询", "status", "info"}


def _is_group_manager(event) -> bool:
    """检查是否具备管理当前群日报推送开关的权限。"""

    if is_superuser_user(str(getattr(event, "user_id", ""))):
        return True
    if not get_event_group_id(event):
        return False
    sender = getattr(event, "sender", None)
    return str(getattr(sender, "role", "") or "") in {"admin", "owner"}


def _parse_action_and_group_id(raw_text: str, event) -> tuple[str, str]:
    """解析开关动作与目标群号。"""

    action = "status"
    target_group_id = ""
    for token in raw_text.split():
        normalized = token.lower()
        if normalized in ENABLE_WORDS:
            action = "enable"
        elif normalized in DISABLE_WORDS:
            action = "disable"
        elif normalized in STATUS_WORDS:
            action = "status"
        elif token.isdigit():
            target_group_id = token

    if not target_group_id:
        target_group_id = get_event_group_id(event)
    return action, target_group_id


def _can_control_target_group(event, target_group_id: str) -> bool:
    if is_superuser_user(str(getattr(event, "user_id", ""))):
        return True
    return get_event_group_id(event) == target_group_id and _is_group_manager(event)


def _format_status(group_id: str) -> str:
    enabled, source = get_daily_report_group_status(group_id)
    return (
        f"小猪日报推送状态：{'开启' if enabled else '关闭'}\n"
        f"群号：{group_id}\n"
        f"来源：{source}"
    )


# ================================ 猪圈日报主动查询 ================================ #


@Command(f"{_REMINDER}猪圈日报").handle()
async def cmd_daily_report_card(event, actions, user_message=None):
    """查本群最近一期封版卡，不触发自动投递或保护结算。"""

    if has_extra_text(event, "猪圈日报"):
        return False
    message_id = getattr(event, "message_id", None)
    group_id = get_event_group_id(event)
    if not group_id:
        await reply(actions, event, reply_to(message_id, "请在群里发送「猪圈日报」。"))
        return True

    report_date = latest_daily_report_query_date()
    if report_date is None:
        await reply(
            actions, event, reply_to(message_id, "本期日报正在出刊，00:10 后再来翻报纸。")
        )
        return True

    bot = bot_for(actions, getattr(event, "self_id", None))
    try:
        image, protection_unconfirmed = await render_latest_daily_report_query_card(
            bot, group_id, report_date
        )
    except (CloudStoreError, LocalStoreUnavailableError) as error:
        if await guard_store_errors(actions, event, error):
            return True
        raise
    except Exception as error:
        logger.exception(f"猪圈日报查卡失败: group={group_id} date={report_date} error={error}")
        await reply(actions, event, reply_to(message_id, "猪圈日报暂时查不到，请稍后再试。"))
        return True

    if image is None:
        await reply(actions, event, reply_to(message_id, "这期猪圈休刊，没攒出一张日报。"))
        return True

    message = common.Message(*image_message(image).contents)
    if protection_unconfirmed:
        message += common.Message(segments.Text("次日保护结算暂未确认。"))
    await reply(actions, event, reply_to(message_id, message))
    return True


# ================================ 小猪日报推送开关 ================================ #


@Command(f"{_REMINDER}小猪日报").handle()
@Command(f"{_REMINDER}小猪日报 <action>").handle()
@Command(f"{_REMINDER}rollpig日报").handle()
@Command(f"{_REMINDER}rollpig日报 <action>").handle()
@Command(f"{_REMINDER}每日总结设置").handle()
@Command(f"{_REMINDER}每日总结设置 <action>").handle()
async def cmd_daily_report_switch(event, actions, user_message=None, action: str = ""):
    raw_text = (
        command_tail(event, "小猪日报", "rollpig日报", "每日总结设置")
        or str(action or "").strip()
    )
    message_id = getattr(event, "message_id", None)
    parsed_action, target_group_id = _parse_action_and_group_id(raw_text, event)

    if not target_group_id:
        await reply(
            actions,
            event,
            reply_to(
                message_id,
                "请在群内使用，或由超级用户指定群号：小猪日报 开启 123456",
            ),
        )
        return True

    if parsed_action == "status":
        if get_event_group_id(event) != target_group_id and not is_superuser_user(
            str(getattr(event, "user_id", ""))
        ):
            await reply(
                actions,
                event,
                reply_to(message_id, "只有超级用户可以查看其他群的日报推送状态。"),
            )
            return True
        await reply(actions, event, reply_to(message_id, _format_status(target_group_id)))
        return True

    if not _can_control_target_group(event, target_group_id):
        await reply(
            actions,
            event,
            reply_to(
                message_id,
                "只有本群群主/管理员可以控制本群；控制其他群需要超级用户权限。",
            ),
        )
        return True

    if parsed_action == "enable":
        try:
            await set_daily_report_group_enabled(target_group_id, True)
        except Exception as error:
            await reply(actions, event, reply_to(message_id, f"日报推送开启失败：{error}"))
            return True
        await reply(
            actions,
            event,
            reply_to(
                message_id,
                f"已开启群 {target_group_id} 的猪圈日报推送。\n{_format_status(target_group_id)}",
            ),
        )
        return True

    if parsed_action == "disable":
        try:
            await set_daily_report_group_enabled(target_group_id, False)
        except Exception as error:
            await reply(actions, event, reply_to(message_id, f"日报推送关闭失败：{error}"))
            return True
        await reply(
            actions,
            event,
            reply_to(
                message_id,
                f"已关闭群 {target_group_id} 的猪圈日报推送。\n{_format_status(target_group_id)}",
            ),
        )
        return True

    await reply(actions, event, reply_to(message_id, _format_status(target_group_id)))
    return True


__all__ = ["cmd_daily_report_card", "cmd_daily_report_switch"]
