"""用户权限管理命令：管理 / 删除管理 / 让我访问。"""
import asyncio


def _banner(bot_name, bot_name_en, ONE_SLOGAN):
    return f"{bot_name} {bot_name_en} - {ONE_SLOGAN}\n————————————————————\n"


def _extract_at_target(event, Segments) -> str:
    for i in event.message:
        if isinstance(i, Segments.At):
            return str(i.qq)
    return ""


async def cmd_del_admin(actions, Manager, Segments, event, order,
                        SUPERS, ROOT_User, Super_User, Manage_User,
                        CONFUSED_WORD, bot_name, bot_name_en, ONE_SLOGAN, reminder,
                        Write_Settings, get_user_nickname):
    r = ""
    r_admin = ""
    Toset = _extract_at_target(event, Segments)
    banner = _banner(bot_name, bot_name_en, ONE_SLOGAN)

    if str(event.user_id) in SUPERS:
        Toset = order[order.find("删除管理 ") + len("删除管理 "):].strip() if Toset == "" else Toset
        s = Super_User
        m = Manage_User
        if Toset in ROOT_User:
            r = f"{banner}失败：指定的用户是 ROOT_User 且组 ROOT_User 为只读。"
            r_admin = f"用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 尝试夺取您的 ROOT_User 权限，已被阻止"
        else:
            if Toset in s:
                s.remove(Toset)
            if Toset in m:
                m.remove(Toset)
            nick = await get_user_nickname(Toset, Manager, actions)
            if Write_Settings(s, m):
                r = f"{banner}成功: {nick} 现在是一个普通用户了。\n现在发送 {reminder}帮助 了解你拥有的权限。"
                r_admin = f"用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 删除了用户 {nick} 的管理员权限"
            else:
                r = f"{banner}失败：设置文件不可写。"
                r_admin = f"用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 尝试删除用户 {nick} 的管理员权限，但因为无法读写配置文件导致修改失败"
    else:
        r = CONFUSED_WORD.format(bot_name=bot_name)

    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
    if r_admin:
        await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin)))


async def cmd_add_admin(actions, Manager, Segments, event, order,
                        SUPERS, ROOT_User, Super_User, Manage_User,
                        CONFUSED_WORD, bot_name, bot_name_en, ONE_SLOGAN, reminder,
                        Write_Settings, get_user_nickname, logger):
    r = ""
    r_admin = ""
    Toset = _extract_at_target(event, Segments)
    banner = _banner(bot_name, bot_name_en, ONE_SLOGAN)
    operator_nick = None

    async def _op_nick():
        nonlocal operator_nick
        if operator_nick is None:
            operator_nick = await get_user_nickname(event.user_id, Manager, actions)
        return operator_nick

    if str(event.user_id) in SUPERS:
        if "管理 M " in order:
            Toset = order[order.find("管理 M ") + len("管理 M "):].strip() if Toset == "" else Toset
            logger.debug(f"try to get_user {Toset}")
            nikename = await get_user_nickname(Toset, Manager, actions)
            logger.debug(str(nikename))
            if len(nikename) == 0:
                r = f"{banner}失败: {Toset} 不是一个有效的用户。"
            else:
                m = Manage_User
                s = Super_User
                if Toset in Manage_User:
                    r = f"{banner}成功: {nikename}(@{Toset}) 已加入管理组 Manage_User 。"
                elif Toset in Super_User:
                    s.remove(Toset)
                    m.append(Toset)
                    if Write_Settings(s, m):
                        r = f"{banner}成功: {nikename}(@{Toset}) 已加入管理组 Manage_User 。\n现在发送 {reminder}帮助 了解你拥有的权限。"
                        r_admin = f"用户 {await _op_nick()} 在 {event.time_str} 将用户 {nikename}(@{Toset}) 从 Super_User 设置为了 Manage_User "
                    else:
                        r = f"{banner}失败: 设置文件不可写。"
                        r_admin = f"用户 {await _op_nick()} 在 {event.time_str} 尝试将用户 {nikename}(@{Toset}) 设置为 Manage_User 但因为无法读写配置文件导致修改失败"
                elif Toset in ROOT_User:
                    r = f"{banner}失败：指定的用户是 ROOT_User 且组 ROOT_User 为只读。"
                    r_admin = f"用户 {await _op_nick()} 在 {event.time_str} 尝试改变您的 ROOT_User 权限，已被阻止"
                else:
                    m.append(Toset)
                    if Write_Settings(s, m):
                        r = f"{banner}成功: {nikename}(@{Toset}) 已加入管理组 Manage_User 。\n现在发送 {reminder}帮助 了解你拥有的权限。"
                        r_admin = f"用户 {await _op_nick()} 在 {event.time_str} 将用户 {nikename}(@{Toset}) 设置为了 Manage_User "
                    else:
                        r = f"{banner}失败: 设置文件不可写"
                        r_admin = f"用户 {await _op_nick()} 在 {event.time_str} 尝试将用户 {nikename}(@{Toset}) 设置为 Manage_User 但因为无法读写配置文件导致修改失败"

        elif "管理 S " in order:
            Toset = order[order.find("管理 S ") + len("管理 S "):].strip() if Toset == "" else Toset
            logger.debug(f"try to get_user {Toset}")
            nikename = await get_user_nickname(Toset, Manager, actions)
            logger.debug(str(nikename))
            if len(nikename) == 0:
                r = f"{banner}失败: {Toset} 不是一个有效的用户"
            else:
                m = Manage_User
                s = Super_User
                if Toset in Manage_User:
                    m.remove(Toset)
                    s.append(Toset)
                    if Write_Settings(s, m):
                        r = f"{banner}成功: {nikename}(@{Toset}) 已加入管理组 Super_User 。\n现在发送 {reminder}帮助 了解你拥有的权限。"
                        r_admin = f"用户 {await _op_nick()} 在 {event.time_str} 将用户 {nikename}(@{Toset}) 从 Manage_User 设置为了 Super_User "
                    else:
                        r = f"{banner}失败：设置文件不可写。"
                        r_admin = f"用户 {await _op_nick()} 在 {event.time_str} 尝试将用户 {nikename}(@{Toset}) 设置为 Super_User 但因为无法读写配置文件导致修改失败"
                elif Toset in Super_User:
                    r = f"{banner}成功: {nikename}(@{Toset}) 已加入管理组 Super_User 。"
                elif Toset in ROOT_User:
                    r = f"{banner}失败：指定的用户是 ROOT_User 且组 ROOT_User 为只读。"
                    r_admin = f"用户 {await _op_nick()} 在 {event.time_str} 尝试改变您的 ROOT_User 权限，已被阻止"
                else:
                    s.append(Toset)
                    if Write_Settings(s, m):
                        r = f"{banner}成功: {nikename}(@{Toset}) 已加入管理组 Super_User 。\n现在发送 {reminder}帮助 了解你拥有的权限。"
                        r_admin = f"用户 {await _op_nick()} 在 {event.time_str} 将用户 {nikename}(@{Toset}) 设置为了 Super_User "
                    else:
                        r = f"{banner}失败：设置文件不可写。"
                        r_admin = f"用户 {await _op_nick()} 在 {event.time_str} 尝试将用户 {nikename}(@{Toset}) 设置为 Super_User 但因为无法读写配置文件导致修改失败"
        else:
            r = f"{banner}失败：只能设置 Manage_User 或 Super_User 。"
    else:
        r = CONFUSED_WORD.format(bot_name=bot_name)

    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
    if r_admin:
        await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin)))


async def cmd_list_admins(actions, Manager, Segments, event,
                          ADMINS, ROOT_User, Super_User, Manage_User,
                          CONFUSED_WORD, bot_name, bot_name_en, ONE_SLOGAN, reminder,
                          get_user_nickname_with_userid):
    if str(event.user_id) in ADMINS:
        manage_users = await asyncio.gather(*[get_user_nickname_with_userid(uid, Manager, actions) for uid in Manage_User])
        super_users = await asyncio.gather(*[get_user_nickname_with_userid(uid, Manager, actions) for uid in Super_User])
        root_users = await asyncio.gather(*[get_user_nickname_with_userid(uid, Manager, actions) for uid in ROOT_User])
        r = f"""{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
Manage_User: {", ".join(manage_users)}
————————————————————
Super_User: {", ".join(super_users)}
————————————————————
ROOT_User: {", ".join(root_users)}
————————————————————
If you are a Super_User or ROOT_User, you can manage these users. Use {reminder}帮助 to know more.
""".strip()
    else:
        r = CONFUSED_WORD.format(bot_name=bot_name)
    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(r)))
