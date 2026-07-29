"""帮助内容生成与展示（图片/文本）。"""
import os

from Tools.tools import create_help_message_image_async

from .help_mode import normalize_help_mode
from .protocol import is_feishu_protocol, is_qq_protocol


def get_help_mode(config, help_mode_settings: dict, user_id) -> str:
    default_mode = normalize_help_mode(config.others.get("help_mode_default", "图片")) or "图片"
    mode = normalize_help_mode(help_mode_settings.get(str(user_id), default_mode))
    return mode or default_mode


def set_help_mode(help_mode_settings: dict, save_fn, user_id, mode: str) -> bool:
    """就地修改 help_mode_settings；写入失败时从磁盘回滚。返回是否成功。"""
    from .help_mode import load_help_mode_settings
    parsed_mode = normalize_help_mode(mode)
    if parsed_mode is None:
        return False
    help_mode_settings[str(user_id)] = parsed_mode
    if save_fn(help_mode_settings):
        return True
    help_mode_settings.clear()
    help_mode_settings.update(load_help_mode_settings())
    return False


def build_help_message(config, Events, event, bot_name: str, reminder: str, plugins_help: str) -> str:
    plugin_section = str(plugins_help or "").strip()
    if isinstance(event, Events.GroupMessageEvent):
        lines = [
            f"如何与{bot_name}交流( •̀ ω •́ )✧",
            f"       注：对话前必须加上 {reminder} 噢！~",
            f"       {reminder}插件视角 —> 看看{bot_name}已加载的功能🔮",
        ]
        if is_feishu_protocol(config):
            lines.append(f"       {reminder}绑定QQ [QQ号] —> 绑定当前飞书账号到QQ")
            lines.append(f"       {reminder}我的绑定 —> 查看当前飞书账号绑定的QQ")
        if is_qq_protocol(config):
            lines.append(f"       {reminder}设置帮助模式 图片/文本 —> 切换帮助为图片或转发文本（仅QQ）")
        if plugin_section:
            lines.extend(["", "插件功能：", plugin_section])
        lines.append("快来聊天吧(*≧︶≦)")
        return "\n".join(lines)
    elif isinstance(event, Events.PrivateMessageEvent):
        lines = [
            f"如何与{bot_name}私聊( •̀ ω •́ )✧",
            "       直接发送消息即可使用支持私聊的插件功能",
        ]
        if is_qq_protocol(config):
            lines.append(f"       {reminder}设置帮助模式 图片/文本 —> 切换帮助为图片或文本（仅QQ）")
        if is_feishu_protocol(config):
            lines.append(f"       {reminder}绑定QQ [QQ号] —> 绑定当前飞书账号到QQ")
            lines.append(f"       {reminder}我的绑定 —> 查看当前飞书账号绑定的QQ")
        if plugin_section:
            lines.extend(["", "插件功能：", plugin_section])
        lines.append("快来聊天吧(*≧︶≦)")
        return "\n".join(lines)
    return ""


def build_admin_help(config, bot_name: str, reminder: str, is_super: bool) -> str:
    """构建管理员帮助文本。"""
    content = [
        (f"{reminder}让我访问", "检索有权限的用户"),
        (f"{reminder}修改 (hh:mm) (内容)", "改变定时消息时间与内容"),
        (f"{reminder}感知", "查看运行状态"),
        (f"{reminder}休眠", f"奖励{bot_name}精致睡眠 💤"),
        (f"{reminder}重启", f"关闭所有线程和进程，关闭{bot_name}。然后重新启动{bot_name}。"),
        (f"{reminder}启用插件（插件名称）", "启用特定插件"),
        (f"{reminder}禁用插件（插件名称）", "忽略特定插件"),
        (f"{reminder}重载插件", "重新加载所有插件"),
        (f"{reminder}群发 (内容)", "在所有群聊中（黑名单群聊除外）发送一条消息"),
        (f"{reminder}冷静 (@QQ+时间)/(@all)", "冷静用户一段时间"),
        (f"{reminder}取消冷静 (@QQ)/(@all)", "解除用户冷静"),
        (f"{reminder}送飞机票 (@QQ)", "将用户移出群聊"),
        ("撤回【引用消息】", "撤回指定消息"),
        (f"{reminder}群发黑名单", "管理群发消息时不会发送到的群聊"),
        (f"{reminder}表情复述", "切换是否开启表情复述功能（默认启用）"),
    ]
    if is_qq_protocol(config):
        content.append((f"{reminder}设置帮助模式 图片/文本", "切换帮助展示样式（仅QQ平台）"))
    if is_super:
        content += [
            (f"{reminder}管理 M (QQ号)", "为用户添加 Manage_User 权限"),
            (f"{reminder}管理 S (QQ号)", "为用户添加 Super_User 权限"),
            (f"{reminder}删除管理 (QQ号)", "删除指定用户所有权限"),
            (f"{reminder}退出本群", "退出当前群聊"),
        ]
    command_lines = [f"{idx+1}. {cmd} —> {desc}" for idx, (cmd, desc) in enumerate(content)]
    return "\n".join([
        f"管理我们的{bot_name}\n————————————————————",
        f"你拥有管理{bot_name}的权限，以下是你可以使用的命令。若要查看普通帮助，请@{bot_name} 或发送【{reminder}用户帮助】",
        *command_lines,
        "你的每一步操作，与用户息息相关。",
    ])


async def send_help_visual(
    config,
    Events,
    Manager,
    Segments,
    help_mode_settings,
    bot_name,
    logger,
    actions,
    event,
    content: str,
    reply_message_id: str = None,
    user_id_override=None,
):
    mode = get_help_mode(config, help_mode_settings, user_id_override if user_id_override is not None else getattr(event, "user_id", ""))
    if isinstance(event, Events.GroupMessageEvent) and is_qq_protocol(config) and mode == "文本":
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        if not lines:
            lines = [content]
        try:
            nodes = [
                Segments.CustomNode(
                    str(event.self_id),
                    bot_name,
                    Manager.Message(Segments.Text(line))
                )
                for line in lines
            ]
            await actions.send_group_forward_msg(
                group_id=event.group_id,
                message=Manager.Message(*nodes)
            )
            return
        except Exception:
            logger.warning("群聊帮助转发发送失败，已回退为文本")
            if reply_message_id:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(reply_message_id), Segments.Text(content)))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(content)))
            return
    bg_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "bg.jpeg")
    image_path = await create_help_message_image_async(content, bg_path)
    if isinstance(event, Events.PrivateMessageEvent):
        if is_qq_protocol(config) and mode == "文本":
            await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(content)))
            return
        if image_path:
            try:
                await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Image(image_path)))
                return
            except Exception:
                logger.warning("帮助图片发送失败，已回退为文本")
        await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(content)))
        return
    if isinstance(event, Events.GroupMessageEvent):
        if image_path:
            try:
                if reply_message_id:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(reply_message_id), Segments.Image(image_path)))
                else:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(image_path)))
                return
            except Exception:
                logger.warning("群聊帮助图片发送失败，已回退为文本")
        if reply_message_id:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(reply_message_id), Segments.Text(content)))
        else:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(content)))
        return
    await actions.send(group_id=getattr(event, "group_id", None), user_id=getattr(event, "user_id", None), message=Manager.Message(Segments.Text(content)))
