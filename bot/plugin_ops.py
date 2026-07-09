"""插件管理命令：重载/禁用/启用插件。

这些命令返回最新加载结果中的插件模块列表，调用方据此刷新展示状态。
"""
import os

from . import plugin_state


async def cmd_reload_plugins(actions, Manager, Segments, event,
                             ADMINS, CONFUSED_WORD, bot_name, bot_name_en, ONE_SLOGAN, reminder,
                             load_plugins):
    """返回 (new_plugins | None)；None 表示无权限，调用方不应覆盖 plugins。"""
    if str(event.user_id) not in ADMINS:
        await actions.send(group_id=event.group_id, message=Manager.Message(
            Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
        return None
    new_plugins = load_plugins()
    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(
        f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
外部后端已重载完成。发送 {reminder}插件视角 以查看更多信息。''')))
    return new_plugins


async def _toggle_plugin(actions, Manager, Segments, event, user_message,
                         ADMINS, CONFUSED_WORD, bot_name, bot_name_en, ONE_SLOGAN, reminder,
                         load_plugins, action_word: str, enable: bool):
    if str(event.user_id) not in ADMINS:
        await actions.send(group_id=event.group_id, message=Manager.Message(
            Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
        return None

    parts = user_message.split(action_word)
    plugin_name = parts[-1].strip() if len(parts) > 1 else ""
    if not plugin_name:
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(
            f"管理员：你的格式有误。\n格式：{reminder}{action_word} (plugin_name)\n参考：{reminder}{action_word} Hello World")))
        return None

    found = plugin_state.find_plugin_path(plugin_name, enable=enable)
    found_path = str(found) if found is not None else None
    if not found_path:
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(
            f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: 找不到插件 {plugin_name}。''')))
        return None

    dirname, basename = os.path.split(found_path)
    renamed = False
    try:
        if enable and basename.startswith("d_"):
            os.rename(found_path, os.path.join(dirname, basename[2:]))
            renamed = True
        elif not enable and not basename.startswith("d_"):
            os.rename(found_path, os.path.join(dirname, "d_" + basename))
            renamed = True
    except Exception as e:
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(
            f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: {action_word} {plugin_name} 时发生错误。
错误信息：{str(e)}''')))
        return None

    new_plugins = load_plugins()
    verb = "启用" if enable else "禁用"
    if renamed:
        msg = f"插件 {plugin_name} 已经成功{verb}"
    else:
        msg = f"插件 {plugin_name} 当前已处于{verb}状态，无需{verb}"
    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(
        f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
{msg}''')))
    return new_plugins


async def cmd_disable_plugin(actions, Manager, Segments, event, user_message,
                             ADMINS, CONFUSED_WORD, bot_name, bot_name_en, ONE_SLOGAN, reminder,
                             load_plugins):
    return await _toggle_plugin(actions, Manager, Segments, event, user_message,
                                ADMINS, CONFUSED_WORD, bot_name, bot_name_en, ONE_SLOGAN, reminder,
                                load_plugins, "禁用插件", enable=False)


async def cmd_enable_plugin(actions, Manager, Segments, event, user_message,
                            ADMINS, CONFUSED_WORD, bot_name, bot_name_en, ONE_SLOGAN, reminder,
                            load_plugins):
    return await _toggle_plugin(actions, Manager, Segments, event, user_message,
                                ADMINS, CONFUSED_WORD, bot_name, bot_name_en, ONE_SLOGAN, reminder,
                                load_plugins, "启用插件", enable=True)
