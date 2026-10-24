from typing import Any

from arclet.alconna import Alconna, Args, MultiVar
from jianer import events, submit_awaitable
from jianer.plugins import PluginMetadata
from jianer.plugins.builtin.alconna import Command

from bot import plugin_state
from plugins.JianerAI.service import JianerAIService
from plugins.JianerAI.tools import (
    ToolPluginRegistration,
    ToolRegistration,
    ToolSpec,
)


__plugin_meta__ = PluginMetadata(
    name="jianerbot-plugin-jianer-ai",
    description="Jianer AI agent, tools, dialogue, personas, speech, suffixes, and memory.",
    usage=(
        "@机器人 [问题] —> 群聊需要当前用户已在白名单；私聊自动处理\n"
        "{reminder}Agent 开启 / 关闭 —> 管理当前群白名单\n"
        "{reminder}模型菜单 / {reminder}切换模型 [代码] —> 管理全局 Agent 模型\n"
        "{reminder}人设 / {reminder}切换人设 [名称] —> 管理全局人设\n"
        "{reminder}添加人设 / {reminder}删除人设 —> 管理全局原始人设\n"
        "{reminder}重载人设 [名称] —> 从原始模板重新加载人设\n"
        "{reminder}人设 迁移 [强制] —> 将原始模板导入 profiles.json\n"
        "{reminder}设置全局后缀 / {reminder}删除全局后缀 —> 管理 AI 全局后缀\n"
        "{reminder}设置特定后缀 / {reminder}删除特定后缀 —> 管理个人 AI 后缀\n"
        "{reminder}清空上下文 —> 删除当前持久 Main Context（兼容旧命令：注销）\n"
        "{reminder}简儿记忆 [子命令] —> 管理长期记忆\n"
        "{reminder}偷表情 [关键词] —> 收藏当前或引用图片到全局表情包库\n"
        "{reminder}表情包 [状态|列表|查看|删除] —> 管理全局表情包库\n"
        "{reminder}TTS [开启|关闭|状态] —> 管理当前会话语音回复\n"
        "{reminder}Agent [开启|关闭|状态|工具|子代理] —> 管理白名单与 Agent 运行状态\n"
        "{reminder}agent.context / stop / profile / func / model —> HypeR 风格 Agent 管理\n"
        "{reminder}批准Shell / {reminder}拒绝Shell [请求ID] —> 审核 Agent 的 Shell 命令"
    ),
    requires={"jianerbot-plugin-alconna"},
)


_REMINDER = str(plugin_state.get_runtime().get("reminder", "~"))
_service: JianerAIService | None = None


def get_service() -> JianerAIService | None:
    return _service


def register_tool(spec: ToolSpec) -> ToolRegistration:
    return _require_service().register_tool(spec)


def unregister_tool(registration: ToolRegistration | str) -> bool:
    return _require_service().unregister_tool(registration)


def register_tool_plugin(
    plugin: Any,
    *,
    context: dict[str, Any] | None = None,
    plugin_id: str | None = None,
) -> ToolPluginRegistration:
    """Register a third-party provider of model-callable tools."""

    return _require_service().register_tool_plugin(
        plugin,
        context=context,
        plugin_id=plugin_id,
    )


def unregister_tool_plugin(plugin: ToolPluginRegistration | str) -> bool:
    return _require_service().unregister_tool_plugin(plugin)


def setup(client: Any, manager: Any) -> None:
    global _service
    if _service is not None:
        raise RuntimeError("JianerAI service is already set up")
    previous_manager = getattr(client, "plugin_manager", None)
    for event_type in (
        events.GroupFileUploadEvent,
        events.GroupAdminEvent,
        events.GroupMemberDecreaseEvent,
        events.GroupMemberIncreaseEvent,
        events.GroupMuteEvent,
        events.FriendAddEvent,
        events.GroupRecallEvent,
        events.FriendRecallEvent,
        events.NotifyEvent,
        events.GroupEssenceEvent,
        events.MessageReactionEvent,
    ):
        client.subscribe(_agent_notice, event_type)
    _service = JianerAIService.from_runtime(plugin_state.get_runtime())
    try:
        startup = submit_awaitable(
            _service.start_after_activation(manager, previous_manager)
        )
        startup.add_done_callback(
            lambda completed, service=_service: _report_startup(service, completed)
        )
    except Exception:
        _service._log_exception("JianerAI startup scheduling failed")


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
    return await service.handle_fallback(
        event,
        actions,
        background_dialogue=True,
    )


async def _agent_notice(event: Any, actions: Any) -> None:
    service = _service
    if service is not None:
        try:
            await service.agent_facade.on_notice(event, actions)
        except Exception:
            service._log_exception("JianerAI Agent notice handling failed")


def authorize(
    *,
    protocol: str,
    self_id: str,
    external_id: str,
    canonical_user_id: str,
    reason: str = "binding",
) -> bool:
    service = _service
    if service is None:
        return False
    return service.authorize(
        protocol=protocol,
        self_id=self_id,
        external_id=external_id,
        canonical_user_id=canonical_user_id,
        reason=reason,
    )


def merge_identity(
    *,
    source_protocol: str,
    source_self_id: str,
    source_external_id: str,
    target_protocol: str = "qq",
    target_self_id: str = "",
    target_external_id: str,
    reason: str = "binding",
) -> bool:
    service = _service
    if service is None:
        return False
    return service.merge_identity(
        source_protocol=source_protocol,
        source_self_id=source_self_id,
        source_external_id=source_external_id,
        target_protocol=target_protocol,
        target_self_id=target_self_id,
        target_external_id=target_external_id,
        reason=reason,
    )


def _require_service() -> JianerAIService:
    if _service is None:
        raise RuntimeError("JianerAI service is not available")
    return _service


def _report_startup(service: JianerAIService, future: Any) -> None:
    try:
        future.result()
    except Exception:
        service._log_exception("JianerAI activation startup failed")


async def _invoke(
    method_name: str,
    event: Any,
    actions: Any,
    *args: Any,
) -> bool:
    service = _require_service()
    if await service.reject_blocked_group(event, actions):
        return True
    method = getattr(service, method_name)
    return await method(event, actions, *args)


_model_menu_command = Alconna(
    [f"{_REMINDER}模型菜单", f"{_REMINDER}ai管理菜单"]
)


@Command(_model_menu_command).handle()
async def _model_menu(event: Any, actions: Any) -> bool:
    return await _invoke("show_model_menu", event, actions)


_switch_model_command = Alconna(
    [f"{_REMINDER}切换模型", f"{_REMINDER}切换AI"],
    Args["model", str],
)


@Command(_switch_model_command).handle()
async def _switch_model(model: str, event: Any, actions: Any) -> bool:
    return await _invoke("switch_model", event, actions, model)


_persona_menu_command = Alconna(
    [f"{_REMINDER}人设", f"{_REMINDER}角色扮演"]
)


@Command(_persona_menu_command).handle()
async def _persona_menu(event: Any, actions: Any) -> bool:
    return await _invoke("show_persona_menu", event, actions)


_switch_persona_command = Alconna(
    [f"{_REMINDER}切换人设", f"{_REMINDER}切换角色"],
    Args["name", str],
)


@Command(_switch_persona_command).handle()
async def _switch_persona(name: str, event: Any, actions: Any) -> bool:
    return await _invoke("switch_persona", event, actions, name)


_add_persona_command = Alconna(
    [f"{_REMINDER}添加人设", f"{_REMINDER}添加预设"],
    Args["definition", MultiVar(str)],
)


@Command(_add_persona_command).handle()
async def _add_persona(
    definition: str | tuple[str, ...],
    event: Any,
    actions: Any,
) -> bool:
    text = (
        " ".join(str(item) for item in definition)
        if isinstance(definition, tuple)
        else str(definition)
    )
    return await _invoke("add_persona", event, actions, text)


_reload_persona_command = Alconna(
    [f"{_REMINDER}重载人设", f"{_REMINDER}重建人格"],
    Args["name", str],
)


@Command(_reload_persona_command).handle()
async def _rebuild_persona(name: str, event: Any, actions: Any) -> bool:
    return await _invoke("rebuild_persona", event, actions, name)


_persona_command = Alconna(
    [f"{_REMINDER}人设", f"{_REMINDER}人格"],
    Args["action", str],
    Args["rest", MultiVar(str, "*")],
)


@Command(_persona_command).handle()
async def _persona_command_handler(
    action: str,
    rest: tuple[str, ...] = (),
    event: Any = None,
    actions: Any = None,
) -> bool:
    if str(action or "").strip() != "迁移":
        return False
    force = any(str(item).strip() == "强制" for item in rest)
    return await _invoke("migrate_personas", event, actions, force)


_delete_persona_command = Alconna(
    [f"{_REMINDER}删除人设", f"{_REMINDER}删除预设"],
    Args["name", str],
)


@Command(_delete_persona_command).handle()
async def _delete_persona(name: str, event: Any, actions: Any) -> bool:
    return await _invoke("delete_persona", event, actions, name)


@Command(
    Alconna(
        f"{_REMINDER}偷表情",
        Args["keywords", MultiVar(str, "*")],
    )
).handle()
async def _steal_sticker(
    keywords: tuple[str, ...] | str = (),
    event: Any = None,
    actions: Any = None,
) -> bool:
    values = (keywords,) if isinstance(keywords, str) else keywords
    return await _invoke(
        "steal_sticker",
        event,
        actions,
        " ".join(str(item) for item in values),
    )


@Command(
    Alconna(
        f"{_REMINDER}表情包",
        Args["action", str],
        Args["argument", str],
        Args["extra", MultiVar(str, "*")],
    )
).handle()
async def _sticker_with_extra(
    action: str,
    argument: str = "",
    extra: tuple[str, ...] | str = (),
    event: Any = None,
    actions: Any = None,
) -> bool:
    values = (extra,) if isinstance(extra, str) else extra
    return await _invoke(
        "sticker_command",
        event,
        actions,
        action,
        argument,
        " ".join(str(item) for item in values),
    )


@Command(Alconna(f"{_REMINDER}表情包")).handle()
async def _sticker_status(event: Any = None, actions: Any = None) -> bool:
    return await _invoke("sticker_command", event, actions, "状态", "", "")


@Command(f"{_REMINDER}设置全局后缀 <suffix>").handle()
async def _set_global_suffix(suffix: str, event: Any, actions: Any) -> bool:
    return await _invoke("set_global_suffix", event, actions, suffix)


@Command(f"{_REMINDER}删除全局后缀").handle()
async def _delete_global_suffix(event: Any, actions: Any) -> bool:
    return await _invoke("remove_global_suffix", event, actions)


@Command(f"{_REMINDER}设置特定后缀 <suffix>").handle()
async def _set_user_suffix(suffix: str, event: Any, actions: Any) -> bool:
    return await _invoke("set_user_suffix", event, actions, suffix)


@Command(f"{_REMINDER}删除特定后缀").handle()
async def _delete_user_suffix(event: Any, actions: Any) -> bool:
    return await _invoke("remove_user_suffix", event, actions)


@Command(f"{_REMINDER}TTS").handle()
@Command(f"{_REMINDER}更改TTS状态").handle()
async def _toggle_tts(event: Any, actions: Any) -> bool:
    return await _invoke("configure_tts", event, actions, "toggle")


@Command(f"{_REMINDER}TTS <state>").handle()
async def _configure_tts(state: str, event: Any, actions: Any) -> bool:
    return await _invoke("configure_tts", event, actions, state)


_agent_command = Alconna(
    f"{_REMINDER}Agent",
    Args["state", str, "status"],
)


@Command(_agent_command).handle()
async def _configure_agent(
    state: str = "status",
    event: Any = None,
    actions: Any = None,
) -> bool:
    return await _invoke("configure_agent", event, actions, state)


@Command(f"{_REMINDER}批准Shell <request_id>").handle()
async def _approve_shell_command(
    request_id: str,
    event: Any,
    actions: Any,
) -> bool:
    return await _invoke(
        "resolve_shell_review",
        event,
        actions,
        request_id,
        True,
    )


@Command(f"{_REMINDER}拒绝Shell <request_id>").handle()
async def _reject_shell_command(
    request_id: str,
    event: Any,
    actions: Any,
) -> bool:
    return await _invoke(
        "resolve_shell_review",
        event,
        actions,
        request_id,
        False,
    )


_clear_context_command = Alconna(
    [
        f"{_REMINDER}清空上下文",
        f"{_REMINDER}重置上下文",
        f"{_REMINDER}注销",
    ]
)


@Command(_clear_context_command).handle()
async def _logout(event: Any, actions: Any) -> bool:
    return await _invoke("clear_context", event, actions)


@Command(f"{_REMINDER}简儿记忆").handle()
async def _memory_help(event: Any, actions: Any) -> bool:
    return await _invoke("memory_command", event, actions, "帮助")


@Command(f"{_REMINDER}简儿记忆 <command>").handle()
async def _memory_command(command: str, event: Any, actions: Any) -> bool:
    return await _invoke("memory_command", event, actions, command)
