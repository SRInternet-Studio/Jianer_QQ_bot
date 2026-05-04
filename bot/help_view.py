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
    if isinstance(event, Events.GroupMessageEvent):
        lines = [
            f"如何与{bot_name}交流( •̀ ω •́ )✧",
            f"       注：对话前必须加上 {reminder} 噢！~",
            f"       {reminder}(任意问题，必填) —> {bot_name}回复",
            f"       {reminder}ai管理菜单 —> 切换和管理AI模型",
            f"       {reminder}插件视角 —> 看看{bot_name}又收集了哪些好好用的工具🔮{plugins_help}",
            f"       {reminder}角色扮演 —> {bot_name}切换不同的角色互动噢！~",
            f"       {reminder}设置特定后缀 (后缀) —> 给你自己的回复加后缀",
            f"       {reminder}删除特定后缀 —> 删除你自己的后缀",
        ]
        if is_feishu_protocol(config):
            lines.append(f"       {reminder}绑定QQ [QQ号] —> 绑定当前飞书账号到QQ")
            lines.append(f"       {reminder}我的绑定 —> 查看当前飞书账号绑定的QQ")
        if is_qq_protocol(config):
            lines.append(f"       {reminder}设置帮助模式 图片/文本 —> 切换帮助为图片或转发文本（仅QQ）")
        lines.append("快来聊天吧(*≧︶≦)")
        return "\n".join(lines)
    elif isinstance(event, Events.PrivateMessageEvent):
        lines = [
            f"如何与{bot_name}私聊( •̀ ω •́ )✧",
            f"       (任意问题，必填) —> {bot_name}回复",
            f"       {reminder}ai管理菜单 —> 查看你可用的私聊AI配置",
            f"       {reminder}切换AI [AI代码] —> 仅切换你自己的私聊AI",
            f"       {reminder}角色扮演 —> {bot_name}切换不同的角色互动噢！~",
        ]
        if is_qq_protocol(config):
            lines.append(f"       {reminder}设置帮助模式 图片/文本 —> 切换帮助为图片或文本（仅QQ）")
        if is_feishu_protocol(config):
            lines.append(f"       {reminder}绑定QQ [QQ号] —> 绑定当前飞书账号到QQ")
            lines.append(f"       {reminder}我的绑定 —> 查看当前飞书账号绑定的QQ")
        lines.append("快来聊天吧(*≧︶≦)")
        return "\n".join(lines)


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
):
    mode = get_help_mode(config, help_mode_settings, getattr(event, "user_id", ""))
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
