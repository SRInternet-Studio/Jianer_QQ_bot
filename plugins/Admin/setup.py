"""Native permission and group-management commands."""

from __future__ import annotations

import asyncio
import re

from jianer import segments as Segments
from jianer.plugins import PluginMetadata

from Tools.tools import get_user_nickname_with_userid
from bot import auth_store
from bot import plugin_state
from bot import plugin_support as support


__plugin_meta__ = PluginMetadata(
    name="jianerbot-plugin-admin",
    description="原生权限管理、禁言、踢人、撤回与群头衔命令。",
    usage=(
        "{reminder}让我访问 —> 查看权限用户\n"
        "{reminder}管理 S/M (QQ或@用户) —> 设置管理员\n"
        "{reminder}删除管理 (QQ或@用户) —> 删除管理员\n"
        "{reminder}冷静 @用户 (秒) / {reminder}取消冷静 @用户|@all\n"
        "{reminder}送飞机票 @用户 —> 踢出群聊\n"
        "{reminder}分配头衔 / {reminder}给他人分配头衔\n"
        "撤回【引用消息】 —> 撤回消息"
    ),
    requires={"jianerbot-plugin-alconna"},
)

_REMINDER = str(plugin_state.get_runtime().get("reminder", "~"))
_BOT_NAME = str(plugin_state.get_runtime().get("bot_name", "简儿"))
_BOT_NAME_EN = str(plugin_state.get_runtime().get("bot_name_en", "Jianer"))
_SLOGAN = str(plugin_state.get_runtime().get("one_slogan", ""))


def _runtime() -> dict:
    return plugin_state.get_runtime()


def _others() -> dict:
    return dict(getattr(plugin_state.get_config(), "others", {}) or {})


def _banner() -> str:
    name = f"{_BOT_NAME} {_BOT_NAME_EN}"
    if _SLOGAN:
        name = f"{name} - {_SLOGAN}"
    return f"{name}\n————————————————————\n"


def _confused() -> str:
    template = str(
        _others().get(
            "confused_words",
            "不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱",
        )
    )
    return template.format(bot_name=_BOT_NAME)


def _extract_at(event) -> str:
    for segment in getattr(event, "message", ()) or ():
        if isinstance(segment, Segments.At):
            return str(segment.qq)
    return ""


async def _send(event, actions, text: str, *, reply: bool = False):
    await support.send_text(event, actions, text, reply=reply)


def _write_groups(s: list, m: list) -> bool:
    root_users = list(_runtime().get("root_users", []))
    if not auth_store.write_user_groups(s, m, root_users=root_users):
        return False
    runtime = _runtime()
    runtime["super_users"] = list(s)
    runtime["manage_users"] = list(m)
    roots = {str(item) for item in root_users}
    runtime["admins"] = list(roots | {str(item) for item in s} | {str(item) for item in m})
    runtime["supers"] = list(roots | {str(item) for item in s})
    return True


async def _delete_admin(event, actions, order: str) -> None:
    if not support.is_super(event):
        await _send(event, actions, _confused())
        return
    target = _extract_at(event) or order[len("删除管理 "):].strip()
    roots = {str(item) for item in _runtime().get("root_users", [])}
    supers, manages = auth_store.read_user_groups()
    if target in roots:
        await _send(
            event,
            actions,
            _banner() + "失败：指定的用户是 ROOT_User 且组 ROOT_User 为只读。",
        )
        await _send_root(
            event,
            actions,
            "用户 {nick} 在 {time_str} 尝试夺取您的 ROOT_User 权限，已被阻止",
        )
        return
    removed_s = target in supers
    removed_m = target in manages
    if removed_s:
        supers.remove(target)
    if removed_m:
        manages.remove(target)
    nick = await support.nickname(target, actions)
    if _write_groups(supers, manages):
        op_nick = await support.nickname(
            event.user_id,
            actions,
            getattr(event, "sender", None),
        )
        await support.send_root_text(
            f"用户 {op_nick} 在 {getattr(event, 'time_str', '')} "
            f"删除了用户 {nick} 的管理员权限",
            actions=actions,
        )
        await _send(
            event,
            actions,
            _banner()
            + f"成功: {nick} 现在是一个普通用户了。\n"
            f"现在发送 {_REMINDER}帮助 了解你拥有的权限。",
        )
        await _send_root(
            event,
            actions,
            f"用户 {{nick}} 在 {{time_str}} 删除了用户 {nick} 的管理员权限",
        )
    else:
        await _send(event, actions, _banner() + "失败：设置文件不可写。")


async def _add_admin(event, actions, order: str) -> None:
    if not support.is_super(event):
        await _send(event, actions, _confused())
        return
    target = _extract_at(event)
    level = ""
    if target:
        if "管理 S " in order:
            level = "S"
        elif "管理 M " in order:
            level = "M"
    elif "管理 S " in order:
        level = "S"
        target = order[order.find("管理 S ") + len("管理 S "):].strip()
    elif "管理 M " in order:
        level = "M"
        target = order[order.find("管理 M ") + len("管理 M "):].strip()
    if not level or not target:
        await _send(event, actions, _banner() + "失败：只能设置 Manage_User 或 Super_User 。")
        return
    nick = await support.nickname(target, actions)
    if not nick:
        await _send(event, actions, _banner() + f"失败: {target} 不是一个有效的用户。")
        return
    supers, manages = auth_store.read_user_groups()
    roots = {str(item) for item in _runtime().get("root_users", [])}
    if target in roots:
        await _send(
            event,
            actions,
            _banner() + "失败：指定的用户是 ROOT_User 且组 ROOT_User 为只读。",
        )
        return
    if level == "M":
        if target in manages:
            await _send(event, actions, _banner() + f"成功: {nick}(@{target}) 已加入管理组 Manage_User 。")
            return
        if target in supers:
            supers.remove(target)
        manages.append(target)
        verb = "加入管理组 Manage_User"
    else:
        if target in supers:
            await _send(event, actions, _banner() + f"成功: {nick}(@{target}) 已加入管理组 Super_User 。")
            return
        if target in manages:
            manages.remove(target)
        supers.append(target)
        verb = "加入管理组 Super_User"
    if _write_groups(supers, manages):
        op_nick = await support.nickname(
            event.user_id,
            actions,
            getattr(event, "sender", None),
        )
        await support.send_root_text(
            f"用户 {op_nick} 在 {getattr(event, 'time_str', '')} "
            f"将用户 {nick}(@{target}) 设置为了 "
            f"{'Manage_User' if level == 'M' else 'Super_User'}",
            actions=actions,
        )
        await _send(
            event,
            actions,
            _banner()
            + f"成功: {nick}(@{target}) 已{verb} 。\n"
            f"现在发送 {_REMINDER}帮助 了解你拥有的权限。",
        )
    else:
        await _send(event, actions, _banner() + "失败：设置文件不可写。")


async def _list_admins(event, actions) -> None:
    if not support.is_admin(event):
        await _send(event, actions, _confused())
        return
    runtime = _runtime()
    manages = list(runtime.get("manage_users", []))
    supers = list(runtime.get("super_users", []))
    roots = list(runtime.get("root_users", []))
    manage_users = await asyncio.gather(
        *[get_user_nickname_with_userid(uid, None, actions) for uid in manages]
    )
    super_users = await asyncio.gather(
        *[get_user_nickname_with_userid(uid, None, actions) for uid in supers]
    )
    root_users = await asyncio.gather(
        *[get_user_nickname_with_userid(uid, None, actions) for uid in roots]
    )
    text = f"""{_BOT_NAME} {_BOT_NAME_EN} - {_SLOGAN}
————————————————————
Manage_User: {", ".join(manage_users)}
————————————————————
Super_User: {", ".join(super_users)}
————————————————————
ROOT_User: {", ".join(root_users)}
————————————————————
If you are a Super_User or ROOT_User, you can manage these users. Use {_REMINDER}帮助 to know more."""
    await _send(event, actions, text, reply=True)


async def _calm(event, actions, order: str) -> None:
    if not support.is_admin(event):
        await _send(event, actions, _confused())
        return
    if not support.supports_group_admin(actions):
        await _send(event, actions, support.UNSUPPORTED_GROUP_ADMIN)
        return
    try:
        numbers = re.findall(r"\d+", order)
        for segment in getattr(event, "message", ()) or ():
            if isinstance(segment, Segments.At):
                duration = numbers[-1] if numbers else ""
                if not duration:
                    await _send(
                        event,
                        actions,
                        "管理员：你的格式有误。\n"
                        f"格式：{_REMINDER}冷静 @anyone/@all (seconds of duration)\n"
                        f"参考：{_REMINDER}冷静 @Harcic#8042 128",
                    )
                    return
                user_id = segment.qq
                if str(user_id) == str(event.user_id):
                    await _send(
                        event,
                        actions,
                        f"你抖M是吧！{_BOT_NAME}生气了！自己找个没人的地方自己处理自己去，"
                        "懒得理你 ┗(•̀へ •́ ╮)",
                    )
                    return
                await actions.set_group_ban(
                    group_id=event.group_id,
                    user_id=user_id,
                    duration=duration,
                )
                await _send(event, actions, f"管理员：已冷静，时长 {duration} 秒。")
                return
        if "@all" in order:
            await support.set_group_whole_ban(actions, event.group_id, True)
            await _send(event, actions, "管理员：已冷静。")
        else:
            await _send(
                event,
                actions,
                "管理员：你的格式有误。\n"
                f"格式：{_REMINDER}冷静 @anyone/@all (seconds of duration)\n"
                f"参考：{_REMINDER}冷静 @Harcic#8042 128",
            )
    except Exception:
        await _send(
            event,
            actions,
            "管理员：你的格式有误。\n"
            f"格式：{_REMINDER}冷静 @anyone/@all (seconds of duration)\n"
            f"参考：{_REMINDER}冷静 @Harcic#8042 128",
        )


async def _uncalm(event, actions, order: str) -> None:
    if not support.is_admin(event):
        await _send(event, actions, _confused())
        return
    if not support.supports_group_admin(actions):
        await _send(event, actions, support.UNSUPPORTED_GROUP_ADMIN)
        return
    for segment in getattr(event, "message", ()) or ():
        if isinstance(segment, Segments.At):
            await actions.set_group_ban(
                group_id=event.group_id,
                user_id=segment.qq,
                duration=0,
            )
            return
    if "@all" in order:
        await support.set_group_whole_ban(actions, event.group_id, False)
        return
    await _send(
        event,
        actions,
        "管理员：你的格式有误。\n"
        f"格式：{_REMINDER}取消冷静 @anyone/@all\n"
        f"参考：{_REMINDER}取消冷静 @Harcic#8042",
    )


async def _kick(event, actions) -> None:
    if not support.is_admin(event):
        await _send(event, actions, _confused())
        return
    if not support.supports_group_admin(actions):
        await _send(event, actions, support.UNSUPPORTED_GROUP_ADMIN)
        return
    for segment in getattr(event, "message", ()) or ():
        if isinstance(segment, Segments.At):
            await actions.set_group_kick(
                group_id=event.group_id,
                user_id=segment.qq,
            )
            op_nick = await support.nickname(
                event.user_id,
                actions,
                sender=getattr(event, "sender", None),
            )
            target_nick = await support.nickname(segment.qq, actions)
            await support.send_root_text(
                f"用户 {op_nick} 在 {getattr(event, 'time_str', '')} "
                f"使 {target_nick} 退出了群聊：{event.group_id}",
                actions=actions,
            )
            return


async def _leave(event, actions) -> None:
    if not support.is_super(event):
        await _send(event, actions, _confused())
        return
    if not support.supports_group_admin(actions):
        await _send(event, actions, support.UNSUPPORTED_GROUP_ADMIN)
        return
    nick = await support.nickname(
        event.user_id,
        actions,
        sender=getattr(event, "sender", None),
    )
    await support.send_root_text(
        f"用户 {nick} 在 {getattr(event, 'time_str', '')} 使机器人退出了群聊：{event.group_id}",
        actions=actions,
    )
    await _send(event, actions, "呜呜呜，各位再见了……")
    await asyncio.sleep(3)
    await support.leave_group(actions, event.group_id)


async def _recall(event, actions) -> None:
    if not support.is_admin(event):
        await _send(event, actions, _confused())
        return
    segments = list(getattr(event, "message", ()) or ())
    if not segments or not isinstance(segments[0], Segments.Reply):
        return
    try:
        await actions.del_message(segments[0].id)
    except Exception:
        pass


async def _title_other(event, actions, order: str) -> None:
    if not support.is_super(event):
        await _send(event, actions, _confused())
        return
    if not support.supports_group_admin(actions):
        await _send(event, actions, support.UNSUPPORTED_GROUP_ADMIN)
        return
    match = re.search(r"(\d+)\s+(.+)", order[len("给他人分配头衔"):].strip())
    if not match:
        await _send(event, actions, "指令格式有误，请使用 用户ID 头衔 的格式。")
        return
    user_id, title = match.group(1), match.group(2).strip()
    if len(title) > 6:
        await _send(event, actions, "头衔不能超过6个字！")
        return
    try:
        await actions.set_group_special_title(
            group_id=event.group_id,
            user_id=user_id,
            title=title,
        )
        await _send(event, actions, "已设置！")
    except Exception as exc:
        await _send(event, actions, f"设置头衔失败：{exc}")


async def _title_self(event, actions, order: str) -> None:
    if not support.supports_group_admin(actions):
        await _send(event, actions, support.UNSUPPORTED_GROUP_ADMIN)
        return
    title = order[len("分配头衔 "):].strip()
    if len(title) > 6:
        await _send(event, actions, "头衔不能超过6个字！")
        return
    runtime = _runtime()
    if support.is_super(event) or runtime.get("self_service_titles", False):
        await actions.set_group_special_title(
            group_id=event.group_id,
            user_id=event.user_id,
            title=title,
        )
        await _send(event, actions, "已设置！")
    else:
        await _send(
            event,
            actions,
            "当前功能未开放,请联系管理员(高级用户 或者 根用户)开放权限！",
        )


async def on_message(event, actions):
    if getattr(event, "group_id", None) is None:
        return False
    text = str(
        getattr(event, "msg_str", getattr(event, "message", ""))
    ).strip()
    reminder = str(_runtime().get("reminder", _REMINDER))
    order = ""
    index = text.find(reminder)
    if index != -1:
        order = text[index + len(reminder):].strip()

    if text == "撤回":
        await _recall(event, actions)
        return True
    if order.startswith("删除管理 "):
        await _delete_admin(event, actions, order)
        return True
    if order.startswith("管理 "):
        await _add_admin(event, actions, order)
        return True
    if "让我访问" in order:
        await _list_admins(event, actions)
        return True
    if order.startswith("取消冷静"):
        await _uncalm(event, actions, order)
        return True
    if order.startswith("冷静"):
        await _calm(event, actions, order)
        return True
    if order.startswith("送飞机票"):
        await _kick(event, actions)
        return True
    if text == f"{reminder}退出本群":
        await _leave(event, actions)
        return True
    if order.startswith("给他人分配头衔"):
        await _title_other(event, actions, order)
        return True
    if order.startswith("分配头衔 "):
        await _title_self(event, actions, order)
        return True
    if text == f"{reminder}更改分配头衔开放状态":
        if not support.is_super(event):
            await _send(event, actions, _confused())
        else:
            runtime = _runtime()
            runtime["self_service_titles"] = not bool(
                runtime.get("self_service_titles", False)
            )
            await _send(
                event,
                actions,
                "分配头衔功能已开放！"
                if runtime["self_service_titles"]
                else "分配头衔功能已取消开放！",
            )
        return True
    return False
