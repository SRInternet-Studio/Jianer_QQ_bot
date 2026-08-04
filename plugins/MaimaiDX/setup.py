"""JianerCore plugin entry for MaimaiDX."""

from jianer import events
from jianer.plugins import PluginMetadata


__plugin_meta__ = PluginMetadata(
    name="jianerbot-plugin-maimaidx",
    description="舞萌DX曲目、查分、别名、猜歌和进度表插件。",
    usage=(
        "帮助maimaiDX —> 查看舞萌DX插件使用说明\n"
        "b50 [@成员|QQ号] / ap50 / info / ginfo —> 查询成绩与谱面\n"
        "查歌 / id / 今日舞萌 / 定数表 / 完成表 —> 曲目与进度功能\n"
        "lxbind / 数据源 / 主题 —> 管理查分器设置"
    ),
    requires={"jianerbot-plugin-alconna", "jianerbot-plugin-jianer-ai"},
)


from plugins.MaimaiDX import commands
from plugins.MaimaiDX.ai_tools import register_ai_tools, unregister_ai_tools
from plugins.MaimaiDX.commands import guess as guess_commands
from plugins.MaimaiDX.runtime import runtime


_ai_tool_module = None
_ai_tool_registrations = ()


async def _listener_started(event, actions):
    await runtime.start(event, actions)


def setup(client, manager):
    global _ai_tool_module, _ai_tool_registrations
    client.subscribe(_listener_started, events.HyperListenerStartNotify)
    _ai_tool_module, _ai_tool_registrations = register_ai_tools(manager)


async def on_message_observe(event, actions):
    """Keep the latest adapter context without delaying unrelated messages."""
    runtime.observe_context(event, actions)


async def on_message(event, actions):
    return await commands.handle_raw(event, actions)


async def shutdown(client, manager):
    global _ai_tool_module, _ai_tool_registrations
    module, registrations = _ai_tool_module, _ai_tool_registrations
    _ai_tool_module, _ai_tool_registrations = None, ()
    try:
        unregister_ai_tools(module, registrations)
    finally:
        await guess_commands.shutdown()
        await runtime.shutdown()
