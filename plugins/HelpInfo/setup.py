"""Native help, about, and runtime status commands."""

from __future__ import annotations

import datetime
import os
import time
from importlib import metadata

from jianer import common as Manager
from jianer import events
from jianer import segments as Segments
from jianer.plugins import PluginMetadata
from jianer.plugins.builtin.alconna import Command, UniMessage

from Tools.capture_screenshot import capture_screenshot
from Tools.tools import create_help_message_images_async, get_system_info, seconds_to_hms
from bot import help_mode
from bot import plugin_state
from bot import plugin_support as support
from bot.help_view import build_admin_help, build_help_message, get_help_mode


__plugin_meta__ = PluginMetadata(
    name="jianerbot-plugin-help-info",
    description="原生帮助、关于、运行状态与帮助模式。",
    usage=(
        "{reminder}帮助 / {reminder}用户帮助 —> 查看帮助\n"
        "{reminder}设置帮助模式 图片/文本 —> 切换帮助展示样式（仅QQ）\n"
        "{reminder}关于 —> 查看机器人与框架版本\n"
        "{reminder}感知 —> 管理员查看运行状态"
    ),
    requires={"jianerbot-plugin-alconna"},
)

_REMINDER = str(plugin_state.get_runtime().get("reminder", "~"))
_BOT_NAME = str(plugin_state.get_runtime().get("bot_name", "简儿"))
_BOT_NAME_EN = str(plugin_state.get_runtime().get("bot_name_en", "Jianer"))
_SLOGAN = str(plugin_state.get_runtime().get("one_slogan", ""))
_HELP_MODE_SETTINGS = help_mode.load_help_mode_settings()

NATIVE_PLUGIN_IDS = frozenset(
    {
        "jianerbot-plugin-interaction-events",
        "jianerbot-plugin-help-info",
        "jianerbot-plugin-admin",
        "jianerbot-plugin-broadcast",
        "jianerbot-plugin-feishu-binding",
    }
)


def _runtime() -> dict:
    return plugin_state.get_runtime()


def _config():
    return plugin_state.get_config()


def _bot_name() -> str:
    return str(_runtime().get("bot_name", _BOT_NAME))


def _reminder() -> str:
    return str(_runtime().get("reminder", _REMINDER))


def _plugins_help() -> str:
    try:
        return plugin_state.plugin_help_text(exclude=NATIVE_PLUGIN_IDS)
    except TypeError:
        return plugin_state.plugin_help_text()


def _user_help(event) -> str:
    return build_help_message(
        _config(),
        events,
        event,
        _bot_name(),
        _reminder(),
        _plugins_help(),
    )


def _admin_help(event) -> str:
    return build_admin_help(
        _config(),
        _bot_name(),
        _reminder(),
        support.is_super(event),
    )


def _send_help_text(event, actions, content: str, reply: bool = False):
    return support.send_text(event, actions, content, reply=reply)


async def _send_help_visual(
    event,
    actions,
    content: str,
    *,
    reply: bool = False,
) -> None:
    mode = get_help_mode(
        _config(),
        _HELP_MODE_SETTINGS,
        str(getattr(event, "user_id", "")),
    )
    if (
        getattr(event, "group_id", None) is not None
        and support.is_qq_protocol(actions)
        and mode == "文本"
    ):
        lines = [line.strip() for line in content.splitlines() if line.strip()] or [content]
        try:
            nodes = [
                Segments.CustomNode(
                    str(event.self_id),
                    _bot_name(),
                    Manager.Message(Segments.Text(line)),
                )
                for line in lines
            ]
            await actions.send_group_forward_msg(
                group_id=event.group_id,
                message=Manager.Message(*nodes),
            )
            return
        except Exception:
            logger = plugin_state.get_logger()
            if logger is not None:
                logger.warning("群聊帮助转发发送失败，已回退为文本")
            await _send_help_text(event, actions, content, reply=reply)
            return

    if (
        getattr(event, "group_id", None) is None
        and support.is_qq_protocol(actions)
        and mode == "文本"
    ):
        await _send_help_text(event, actions, content, reply=reply)
        return

    bg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "assets",
        "bg.jpeg",
    )
    image_paths = await create_help_message_images_async(content, bg_path)
    if image_paths:
        try:
            message = UniMessage()
            if reply and getattr(event, "message_id", None):
                message += UniMessage.reply(str(event.message_id))
            for path in image_paths:
                message += UniMessage.image(path)
            await message.send(event=event, actions=actions)
            return
        except Exception:
            logger = plugin_state.get_logger()
            if logger is not None:
                logger.warning("帮助图片发送失败，已回退为文本")
    await _send_help_text(event, actions, content, reply=reply)


@Command(f"{_REMINDER}帮助").handle()
async def _help(event, actions):
    if getattr(event, "group_id", None) is not None and support.is_admin(event):
        content = _admin_help(event)
    else:
        content = _user_help(event)
    await _send_help_visual(event, actions, content)
    return True


@Command(f"{_REMINDER}用户帮助").handle()
async def _user_help_command(event, actions):
    await _send_help_visual(
        event,
        actions,
        _user_help(event),
        reply=getattr(event, "group_id", None) is not None,
    )
    return True


@Command(f"{_REMINDER}设置帮助模式 <mode>").handle()
async def _set_help_mode(mode: str, event, actions):
    if isinstance(mode, (tuple, list)):
        mode = " ".join(str(item) for item in mode)
    if not support.is_qq_protocol(actions):
        await support.send_text(
            event,
            actions,
            "该功能仅支持QQ平台（OneBot/Milky）。",
            reply=getattr(event, "group_id", None) is not None,
        )
        return True
    parsed = help_mode.normalize_help_mode(mode)
    if parsed is None:
        await support.send_text(
            event,
            actions,
            f"参数错误。示例：{_reminder()}设置帮助模式 图片 或 {_reminder()}设置帮助模式 文本",
            reply=getattr(event, "group_id", None) is not None,
        )
        return True
    user_id = str(getattr(event, "user_id", ""))
    _HELP_MODE_SETTINGS[user_id] = parsed
    if help_mode.save_help_mode_settings(_HELP_MODE_SETTINGS):
        text = f"已切换你的帮助模式为【{parsed}】。"
    else:
        text = "设置失败：无法写入帮助模式配置。"
    await support.send_text(
        event,
        actions,
        text,
        reply=getattr(event, "group_id", None) is not None,
    )
    return True


@Command(f"{_REMINDER}关于").handle()
async def _about(event, actions):
    if getattr(event, "group_id", None) is None:
        return False
    framework = await actions.get_version_info()
    framework = framework.data.raw
    template_path = os.path.join("static", "about_template.html")
    with open(template_path, "r", encoding="utf-8") as handle:
        html_content = handle.read()
    replacements = {
        "{{bot_name}}": _bot_name(),
        "{{bot_name_en}}": _runtime().get("bot_name_en", _BOT_NAME_EN),
        "{{ONE_SLOGAN}}": _runtime().get("one_slogan", _SLOGAN),
        "{{version_name}}": "JianerQQ机器人 版本 NEXT5",
        "{{app_name}}": framework.get("app_name", "Unknown"),
        "{{protocol_version}}": framework.get("protocol_version", ""),
        "{{app_version}}": framework.get("app_version", ""),
        "{{jianercore_version}}": _jianercore_version(),
        "{{year}}": str(datetime.datetime.now().year),
    }
    for key, value in replacements.items():
        html_content = html_content.replace(key, str(value))
    temp_html_path = os.path.abspath(
        os.path.join("static", f"about_temp_{int(time.time())}.html")
    )
    with open(temp_html_path, "w", encoding="utf-8") as handle:
        handle.write(html_content)
    image_path = await capture_screenshot(
        f"file:///{temp_html_path.replace(chr(92), '/')}",
        "about_image",
        "png",
    )
    await support.send_message(
        event,
        actions,
        UniMessage.image(image_path),
    )
    for path in (temp_html_path, image_path):
        try:
            os.remove(path)
        except OSError:
            pass
    return True


@Command(f"{_REMINDER}感知").handle()
async def _status(event, actions):
    if getattr(event, "group_id", None) is None:
        return False
    if not support.is_admin(event):
        await support.send_text(
            event,
            actions,
            str(
                _runtime().get("confused_word", "不能这么做！")
            ).format(bot_name=_bot_name()),
        )
        return True
    system_info = get_system_info()
    started_at = float(_runtime().get("started_at", time.time()))
    text = f"""{_bot_name()} {_runtime().get('bot_name_en', _BOT_NAME_EN)} - {_runtime().get('one_slogan', _SLOGAN)}
————————————————————
系统当前运行状况
运行时间：{seconds_to_hms(round(time.time() - started_at, 2))}
系统版本：{system_info["version_info"]}
体系结构：{system_info["architecture"]}
CPU占用：{system_info["cpu_usage"]}%
内存占用：{system_info["memory_usage_percentage"]}%"""
    for index, usage in enumerate(system_info["gpu_usage"]):
        text += f"\nGPU {index} Usage：{usage * 100:.2f}%"
    await support.send_text(event, actions, text)
    return True


async def on_message(event, actions):
    if (
        getattr(event, "group_id", None) is None
        or not getattr(event, "is_mentioned", False)
    ):
        return False
    text = str(
        getattr(event, "msg_str", getattr(event, "message", ""))
    ).strip()
    if text == "帮助":
        content = _admin_help(event) if support.is_admin(event) else _user_help(event)
        await _send_help_visual(event, actions, content)
        return True
    if text == "用户帮助":
        await _send_help_visual(
            event,
            actions,
            _user_help(event),
            reply=True,
        )
        return True
    return False


def _jianercore_version() -> str:
    try:
        return metadata.version("jianer-bot")
    except metadata.PackageNotFoundError:
        return "Unknown"
