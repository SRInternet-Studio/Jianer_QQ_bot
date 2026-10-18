"""JianerCore entry point for the JianerMate plugin."""

from typing import Any

from arclet.alconna import Alconna, Args, MultiVar
from jianer import submit_awaitable
from jianer.plugins import PluginMetadata
from jianer.plugins.builtin.alconna import Command

from bot import plugin_state
from plugins.JianerMate.service import JianerMateService


__plugin_meta__ = PluginMetadata(
    name="jianerbot-plugin-jianer-mate",
    description="Single-persona group chat AI with passive replies, proactive speech, and memes.",
    usage=(
        "@机器人 [问题] —> 群聊 AI 对话（裸 At 也会回应）；私聊直接发送问题\n"
        "{reminder}主动发言 [状态|开启|关闭|频率 0.5|免打扰 23:00-08:00|提示|决策|调试] —> 管理本群主动发言\n"
        "{reminder}人格 [状态|转换 <文本>|导入 <旧预设>|重置|概率 0.2|风格|风格添加|风格删除] —> 管理员人格设定\n"
        "{reminder}设置提示 <文本> / {reminder}清空提示 —> 当前会话补充要求\n"
        "{reminder}偷表情 —> 把回复的图片收进全局表情包库\n"
        "{reminder}表情包 [状态|列表 1|删除 <编号>|标签 <编号> <标签>|清空 确认] —> 管理表情包库"
    ),
    requires={"jianerbot-plugin-alconna"},
)


_REMINDER = str(plugin_state.get_runtime().get("reminder", "~"))
_service: JianerMateService | None = None


def get_service() -> JianerMateService | None:
    return _service


def register_tool(spec: Any) -> Any:
    """Let sibling plugins publish model-callable tools (JianerAI-compatible)."""

    return _require_service().register_tool(spec)


def unregister_tool(registration: Any) -> bool:
    return _require_service().unregister_tool(registration)


def _require_service() -> JianerMateService:
    if _service is None:
        raise RuntimeError("JianerMate service is not available")
    return _service


def _report_startup(service: JianerMateService, future: Any) -> None:
    try:
        future.result()
    except Exception:
        service._log_exception("JianerMate activation startup failed")


def setup(client: Any, manager: Any) -> None:
    global _service
    if _service is not None:
        raise RuntimeError("JianerMate service is already set up")
    service = JianerMateService.from_runtime(plugin_state.get_runtime())
    _service = service
    previous_manager = getattr(client, "plugin_manager", None)
    try:
        startup = submit_awaitable(
            service.start_after_activation(manager, previous_manager)
        )
        startup.add_done_callback(
            lambda completed, current=service: _report_startup(current, completed)
        )
    except Exception:
        service._log_exception("JianerMate startup scheduling failed")


async def shutdown(client: Any, manager: Any) -> None:
    global _service
    service, _service = _service, None
    if service is not None:
        await service.shutdown()


async def on_message_observe(event: Any, actions: Any) -> None:
    service = _service
    if service is not None:
        await service.observe(event, actions)


async def on_message_fallback(event: Any, actions: Any) -> bool:
    service = _service
    if service is None:
        return False
    return await service.handle_fallback(event, actions)


@Command(Alconna(f"{_REMINDER}主动发言", Args["action", str], Args["value", str])).handle()
async def _active_with_value(
    action: str = "",
    value: str = "",
    event: Any = None,
    actions: Any = None,
) -> bool:
    return await _require_service().handle_active_command(event, actions, action, value)


@Command(Alconna(f"{_REMINDER}主动发言", Args["action", str])).handle()
async def _active_action(
    action: str = "",
    event: Any = None,
    actions: Any = None,
) -> bool:
    return await _require_service().handle_active_command(event, actions, action, "")


@Command(Alconna(f"{_REMINDER}主动发言")).handle()
async def _active_status(event: Any = None, actions: Any = None) -> bool:
    return await _require_service().handle_active_command(event, actions, "", "")


_PERSONA_COMMAND = Alconna(
    f"{_REMINDER}人格",
    Args["action", str],
    Args["rest", MultiVar(str, "*")],
)

_SET_PROMPT = Alconna(f"{_REMINDER}设置提示", Args["text", MultiVar(str)])
_SET_GROUP_PROMPT = Alconna(
    f"{_REMINDER}设置群提示", Args["text", MultiVar(str)]
)


@Command(_PERSONA_COMMAND).handle()
async def _persona_command(
    action: str = "",
    rest: tuple[str, ...] = (),
    event: Any = None,
    actions: Any = None,
) -> bool:
    return await _require_service().handle_persona_command(
        event,
        actions,
        action,
        " ".join(str(item) for item in (rest or ())),
    )


@Command(Alconna(f"{_REMINDER}人格")).handle()
async def _persona_status(event: Any = None, actions: Any = None) -> bool:
    return await _require_service().handle_persona_command(
        event, actions, "", ""
    )


@Command(_SET_PROMPT).handle()
@Command(_SET_GROUP_PROMPT).handle()
async def _set_conversation_prompt(
    text: str = "",
    event: Any = None,
    actions: Any = None,
) -> bool:
    return await _require_service().set_conversation_prompt(event, actions, text)


@Command(Alconna(f"{_REMINDER}清空提示")).handle()
@Command(Alconna(f"{_REMINDER}清空群提示")).handle()
async def _clear_conversation_prompt(
    event: Any = None,
    actions: Any = None,
) -> bool:
    return await _require_service().clear_conversation_prompt(event, actions)


@Command(Alconna(f"{_REMINDER}偷表情")).handle()
async def _steal_meme(event: Any = None, actions: Any = None) -> bool:
    return await _require_service().steal_meme_command(event, actions)


@Command(
    Alconna(
        f"{_REMINDER}表情包",
        Args["action", str],
        Args["argument", str],
        Args["extra", MultiVar(str)],
    )
).handle()
async def _meme_with_extra(
    action: str = "",
    argument: str = "",
    extra: str = "",
    event: Any = None,
    actions: Any = None,
) -> bool:
    return await _require_service().meme_command(
        event, actions, action, argument, extra
    )


@Command(Alconna(f"{_REMINDER}表情包", Args["action", str], Args["argument", str])).handle()
async def _meme_with_argument(
    action: str = "",
    argument: str = "",
    event: Any = None,
    actions: Any = None,
) -> bool:
    return await _require_service().meme_command(
        event, actions, action, argument, ""
    )


@Command(Alconna(f"{_REMINDER}表情包")).handle()
async def _meme_status(event: Any = None, actions: Any = None) -> bool:
    return await _require_service().meme_command(event, actions, "", "", "")
