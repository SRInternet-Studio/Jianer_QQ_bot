from typing import Any

from arclet.alconna import Alconna, Args
from jianer.plugins import PluginMetadata
from jianer.plugins.builtin.alconna import Command

from bot import plugin_state
from plugins.JianerAI.service import JianerAIService
from plugins.JianerAI.tools import ToolRegistration, ToolSpec


__plugin_meta__ = PluginMetadata(
    name="jianerbot-plugin-jianer-ai",
    description="Jianer AI agent, tools, dialogue, personas, speech, suffixes, and memory.",
    usage=(
        "@机器人 [问题] —> 群聊 AI 对话（裸 At 也会回应）；私聊直接发送问题\n"
        "{reminder}ai管理菜单 / {reminder}切换AI [代码] —> 管理当前会话模型\n"
        "{reminder}角色扮演 / {reminder}切换角色 [名称] —> 管理当前会话角色\n"
        "{reminder}添加预设 / {reminder}删除预设 —> 管理角色预设\n"
        "{reminder}设置全局后缀 / {reminder}删除全局后缀 —> 管理 AI 全局后缀\n"
        "{reminder}设置特定后缀 / {reminder}删除特定后缀 —> 管理个人 AI 后缀\n"
        "{reminder}注销 —> 清空当前会话的短期上下文\n"
        "{reminder}简儿记忆 [子命令] —> 管理长期记忆\n"
        "{reminder}TTS [开启|关闭|状态] —> 管理当前会话语音回复\n"
        "{reminder}Agent [开启|关闭|自动|状态|工具] —> 管理当前会话 Agent"
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


def setup(client: Any, manager: Any) -> None:
    global _service
    if _service is not None:
        raise RuntimeError("JianerAI service is already set up")
    _service = JianerAIService.from_runtime(plugin_state.get_runtime())


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


@Command(f"{_REMINDER}ai管理菜单").handle()
async def _model_menu(event: Any, actions: Any) -> bool:
    return await _invoke("show_model_menu", event, actions)


@Command(f"{_REMINDER}切换AI <model>").handle()
async def _switch_model(model: str, event: Any, actions: Any) -> bool:
    return await _invoke("switch_model", event, actions, model)


@Command(f"{_REMINDER}角色扮演").handle()
async def _persona_menu(event: Any, actions: Any) -> bool:
    return await _invoke("show_persona_menu", event, actions)


@Command(f"{_REMINDER}切换角色 <name>").handle()
async def _switch_persona(name: str, event: Any, actions: Any) -> bool:
    return await _invoke("switch_persona", event, actions, name)


@Command(f"{_REMINDER}添加预设 <definition>").handle()
async def _add_persona(definition: str, event: Any, actions: Any) -> bool:
    return await _invoke("add_persona", event, actions, definition)


@Command(f"{_REMINDER}删除预设 <name>").handle()
async def _delete_persona(name: str, event: Any, actions: Any) -> bool:
    return await _invoke("delete_persona", event, actions, name)


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


@Command(f"{_REMINDER}注销").handle()
async def _logout(event: Any, actions: Any) -> bool:
    return await _invoke("clear_context", event, actions)


@Command(f"{_REMINDER}简儿记忆").handle()
async def _memory_help(event: Any, actions: Any) -> bool:
    return await _invoke("memory_command", event, actions, "帮助")


@Command(f"{_REMINDER}简儿记忆 <command>").handle()
async def _memory_command(command: str, event: Any, actions: Any) -> bool:
    return await _invoke("memory_command", event, actions, command)
