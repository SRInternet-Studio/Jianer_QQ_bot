"""Feishu account to QQ identity binding."""

from __future__ import annotations

import re

from jianer import events
from jianer.plugins import PluginMetadata

from bot import feishu_bindings
from bot import plugin_state
from bot import plugin_support as support


__plugin_meta__ = PluginMetadata(
    name="jianerbot-plugin-feishu-binding",
    description="飞书账号与 QQ 身份绑定。",
    usage=(
        "{reminder}绑定QQ (QQ号) —> 绑定当前飞书账号到QQ\n"
        "{reminder}我的绑定 —> 查看当前飞书账号绑定的QQ"
    ),
    requires={"jianerbot-plugin-alconna"},
)

_REMINDER = str(plugin_state.get_runtime().get("reminder", "~"))
_reconciled = False


async def _bind(event, actions, qq_id: str) -> None:
    qq_id = qq_id.strip()
    if not re.fullmatch(r"\d{5,20}", qq_id):
        await support.send_text(
            event,
            actions,
            f"绑定失败：请输入正确的QQ号，例如：{_REMINDER}绑定QQ 123456789",
            reply=getattr(event, "group_id", None) is not None,
        )
        return
    if not feishu_bindings.bind_feishu_user(
        str(event.user_id),
        qq_id,
        self_id=getattr(event, "self_id", None),
    ):
        await support.send_text(
            event,
            actions,
            "绑定失败：写入绑定数据时出错",
            reply=getattr(event, "group_id", None) is not None,
        )
        return
    await feishu_bindings.reconcile_feishu_binding(
        str(event.user_id),
        qq_id,
        self_id=getattr(event, "self_id", None),
    )
    await support.send_text(
        event,
        actions,
        f"绑定成功：当前飞书账号已绑定 QQ {qq_id}",
        reply=getattr(event, "group_id", None) is not None,
    )


async def on_message(event, actions):
    text = str(
        getattr(event, "msg_str", getattr(event, "message", ""))
    ).strip()
    reminder = str(plugin_state.get_runtime().get("reminder", _REMINDER))
    order = ""
    index = text.find(reminder)
    if index != -1:
        order = text[index + len(reminder):].strip()
    if order.startswith("绑定QQ "):
        if not support.is_feishu_protocol(actions):
            await support.send_text(
                event,
                actions,
                "该功能仅支持飞书平台。",
                reply=getattr(event, "group_id", None) is not None,
            )
            return True
        await _bind(event, actions, order[len("绑定QQ "):])
        return True
    if order == "我的绑定":
        if not support.is_feishu_protocol(actions):
            await support.send_text(
                event,
                actions,
                "该功能仅支持飞书平台。",
                reply=getattr(event, "group_id", None) is not None,
            )
            return True
        qq_id = feishu_bindings.get_bound_qq(str(event.user_id))
        message = (
            f"当前绑定QQ：{qq_id}"
            if qq_id
            else f"当前未绑定QQ。请发送：{reminder}绑定QQ 你的QQ号"
        )
        await support.send_text(
            event,
            actions,
            message,
            reply=getattr(event, "group_id", None) is not None,
        )
        return True
    return False


async def _on_listener_start(event, actions):
    global _reconciled
    if _reconciled:
        return
    _reconciled = True
    try:
        report = await feishu_bindings.reconcile_pending_bindings()
        logger = plugin_state.get_logger()
        if logger is not None and (report.pending or report.errors):
            logger.warning(
                "feishu binding reconciliation: "
                f"pending={report.pending} errors={report.errors}"
            )
    except Exception:
        logger = plugin_state.get_logger()
        if logger is not None:
            logger.exception("feishu binding reconciliation failed")


def setup(client, manager):
    client.subscribe(_on_listener_start, events.HyperListenerStartNotify)
