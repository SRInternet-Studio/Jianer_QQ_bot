"""杂项群命令：AI 菜单、后缀、黑名单、群成员管理、TTS 等。"""
import os
import random
import re

import Tools.ARC_AI as ARC_AI
from Tools.tools import get_system_info, seconds_to_hms

from .utils import load_blacklist


_BLACKLIST_FILE = "blacklist.sr"


async def _send(actions, Manager, Segments, event, text, reply=False):
    msg = Manager.Message(Segments.Reply(event.message_id), Segments.Text(text)) if reply else Manager.Message(Segments.Text(text))
    await actions.send(group_id=event.group_id, message=msg)


async def _confused(actions, Manager, Segments, event, CONFUSED_WORD, bot_name):
    await _send(actions, Manager, Segments, event, CONFUSED_WORD.format(bot_name=bot_name))


async def cmd_ai_menu(actions, Manager, Segments, event, bot_name, bot_name_en, reminder, EnableNetwork):
    ais = ARC_AI.list_available_ais()
    current_ai_friendly = ARC_AI.get_current_ai_name(EnableNetwork)
    ai_list_str = "\n".join([f"- {friendly} (代码: {name})" for name, friendly in ais.items()])
    menu = f'''{bot_name} {bot_name_en} - AI管理菜单
————————————————————
当前使用的AI: {current_ai_friendly} (代码: {EnableNetwork})

可用AI列表:
{ai_list_str}

指令:
{reminder}切换AI [AI代码] —> 切换到指定的AI
例如: {reminder}切换AI gemini
'''
    await _send(actions, Manager, Segments, event, menu)


async def cmd_switch_ai(actions, Manager, Segments, event, user_message, reminder, logger):
    """成功时返回新的 EnableNetwork（字符串），失败/无效时返回 None。"""
    target_ai = user_message.replace(f"{reminder}切换AI ", "").strip()
    available_ais = ARC_AI.list_available_ais()
    if target_ai in available_ais:
        friendly_name = available_ais[target_ai]
        logger.info(f"sys: AI Mode change to {friendly_name} ({target_ai})")
        await _send(actions, Manager, Segments, event, f"成功切换到AI: {friendly_name}")
        return target_ai
    await _send(actions, Manager, Segments, event, f"找不到AI配置: {target_ai}，请检查代码拼写。")
    return None


async def cmd_list_blacklist(actions, Manager, Segments, event, ADMINS, CONFUSED_WORD, bot_name):
    if str(event.user_id) not in ADMINS:
        await _confused(actions, Manager, Segments, event, CONFUSED_WORD, bot_name)
        return
    try:
        with open(_BLACKLIST_FILE, "r", encoding="utf-8") as f:
            blacklist1 = set(line.strip() for line in f)
        await _send(actions, Manager, Segments, event, f"黑名单列表加载完成: {blacklist1}")
    except FileNotFoundError:
        await _send(actions, Manager, Segments, event, "黑名单列表加载失败,原因:没有文件")
    except UnicodeDecodeError:
        await _send(actions, Manager, Segments, event, "黑名单列表加载失败,原因:解码失败")


async def cmd_add_blacklist(actions, Manager, Segments, event, order,
                             ADMINS, ROOT_User, CONFUSED_WORD, bot_name,
                             get_user_nickname):
    if str(event.user_id) not in ADMINS:
        await _confused(actions, Manager, Segments, event, CONFUSED_WORD, bot_name)
        return
    target = order[order.find("添加黑名单 ") + len("添加黑名单 "):].strip()
    blacklist = load_blacklist()
    if target in blacklist:
        await _send(actions, Manager, Segments, event, f"黑名单添加失败,是因为{target}已在黑名单")
        return
    blacklist.add(target)
    try:
        with open(_BLACKLIST_FILE, "w", encoding="utf-8") as f:
            for item in blacklist:
                f.write(item + "\n")
        nick = await get_user_nickname(event.user_id, Manager, actions)
        r_admin = f"用户 {nick} 在 {event.time_str} 将群 {target} 添加到禁止群发黑名单"
        await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin)))
        await _send(actions, Manager, Segments, event, f"黑名单添加成功\n现在的群发黑名单: {blacklist}")
    except Exception as e:
        await _send(actions, Manager, Segments, event, f"黑名单添加失败, 是因为\n{e}")


async def cmd_remove_blacklist(actions, Manager, Segments, event, order,
                                ADMINS, ROOT_User, CONFUSED_WORD, bot_name,
                                get_user_nickname):
    if str(event.user_id) not in ADMINS:
        await _confused(actions, Manager, Segments, event, CONFUSED_WORD, bot_name)
        return
    target = order[order.find("删除黑名单 ") + len("删除黑名单 "):].strip()
    blacklist = load_blacklist()
    if target not in blacklist:
        await _send(actions, Manager, Segments, event, f"黑名单删除失败, 是因为群{target}不在黑名单")
        return
    blacklist.remove(target)
    try:
        with open(_BLACKLIST_FILE, "w", encoding="utf-8") as f:
            for item in blacklist:
                f.write(item + "\n")
        nick = await get_user_nickname(event.user_id, Manager, actions)
        r_admin = f"用户 {nick} 在 {event.time_str} 将群 {target} 从禁止群发黑名单中删除"
        await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin)))
        await _send(actions, Manager, Segments, event, f"黑名单删除成功\n现在黑名单: {blacklist}")
    except Exception as e:
        await _send(actions, Manager, Segments, event, f"黑名单删除失败, 是因为\n{e}")


async def cmd_set_global_suffix(actions, Manager, Segments, event, order,
                                 ADMINS, CONFUSED_WORD, bot_name, suffix_manager):
    if str(event.user_id) not in ADMINS:
        await _confused(actions, Manager, Segments, event, CONFUSED_WORD, bot_name)
        return
    suffix = order[order.find("设置全局后缀 ") + len("设置全局后缀 "):].strip()
    if suffix:
        suffix_manager.set_global_suffix(suffix)
        await _send(actions, Manager, Segments, event, f"全局后缀已设置为：{suffix}")
    else:
        await _send(actions, Manager, Segments, event, "后缀不能为空！")


async def cmd_remove_global_suffix(actions, Manager, Segments, event,
                                    ADMINS, CONFUSED_WORD, bot_name, suffix_manager):
    if str(event.user_id) not in ADMINS:
        await _confused(actions, Manager, Segments, event, CONFUSED_WORD, bot_name)
        return
    suffix_manager.remove_global_suffix()
    await _send(actions, Manager, Segments, event, "全局后缀已删除。")


async def cmd_set_user_suffix(actions, Manager, Segments, event, order, suffix_manager):
    suffix = order[order.find("设置特定后缀 ") + len("设置特定后缀 "):].strip()
    if suffix:
        suffix_manager.set_user_suffix(event.user_id, suffix)
        await _send(actions, Manager, Segments, event, f"已为你配置特定后缀：{suffix}")
    else:
        await _send(actions, Manager, Segments, event, "后缀不能为空！")


async def cmd_remove_user_suffix(actions, Manager, Segments, event, suffix_manager):
    suffix_manager.remove_user_suffix(event.user_id)
    await _send(actions, Manager, Segments, event, "你的特定后缀已删除。")


async def cmd_uncalm(actions, Manager, Segments, event, order,
                     ADMINS, CONFUSED_WORD, bot_name, reminder):
    if str(event.user_id) not in ADMINS:
        await _confused(actions, Manager, Segments, event, CONFUSED_WORD, bot_name)
        return
    start_index = order.find("取消冷静 ")
    if start_index == -1:
        return
    result = order[start_index + len("取消冷静 "):].strip()
    numbers = re.findall(r"\d+", result)
    complete = False
    for i in event.message:
        if isinstance(i, Segments.At):
            await actions.set_group_ban(group_id=event.group_id, user_id=numbers[0], duration=0)
            complete = True
            break
    if not complete:
        if "@all" in order:
            await actions.custom.set_group_whole_ban(group_id=event.group_id, enable=False)
        else:
            await _send(actions, Manager, Segments, event,
                        f"管理员：你的格式有误。\n格式：{reminder}取消冷静 @anyone/@all\n参考：{reminder}取消冷静 @Harcic#8042")


async def cmd_calm(actions, Manager, Segments, event, order,
                   ADMINS, CONFUSED_WORD, bot_name, reminder):
    if str(event.user_id) not in ADMINS:
        await _confused(actions, Manager, Segments, event, CONFUSED_WORD, bot_name)
        return
    try:
        start_index = order.find("冷静")
        if start_index == -1:
            return
        result = order[start_index + len("冷静"):].strip()
        numbers = re.findall(r"\d+", result)
        complete = False
        time114 = None
        for i in event.message:
            if isinstance(i, Segments.At):
                userid114 = numbers[0]
                time114 = numbers[1]
                if str(userid114) == str(event.user_id):
                    await _send(actions, Manager, Segments, event,
                                f"你抖M是吧！{bot_name}生气了！自己找个没人的地方自己处理自己去，懒得理你 ┗(•̀へ •́ ╮)")
                    complete = None
                else:
                    await actions.set_group_ban(group_id=event.group_id, user_id=userid114, duration=time114)
                    complete = True
                break
        if complete is not None:
            if not complete:
                if "@all" in order:
                    await actions.custom.set_group_whole_ban(group_id=event.group_id, enable=True)
                    await _send(actions, Manager, Segments, event, "管理员：已冷静。")
                else:
                    await _send(actions, Manager, Segments, event,
                                f"管理员：你的格式有误。\n格式：{reminder}冷静 @anyone/@all (seconds of duration)\n参考：{reminder}冷静 @Harcic#8042 128")
            else:
                await _send(actions, Manager, Segments, event, f"管理员：已冷静，时长 {time114} 秒。")
    except Exception:
        await _send(actions, Manager, Segments, event,
                    f"管理员：你的格式有误。\n格式：{reminder}冷静 @anyone/@all (seconds of duration)\n参考：{reminder}冷静 @Harcic#8042 128")


async def cmd_kick(actions, Manager, Segments, event, order,
                   ADMINS, ROOT_User, CONFUSED_WORD, bot_name, get_user_nickname):
    if str(event.user_id) not in ADMINS:
        await _confused(actions, Manager, Segments, event, CONFUSED_WORD, bot_name)
        return
    for i in event.message:
        if isinstance(i, Segments.At):
            await actions.set_group_kick(group_id=event.group_id, user_id=i.qq)
            op_nick = await get_user_nickname(event.user_id, Manager, actions)
            target_nick = await get_user_nickname(i.qq, Manager, actions)
            r_admin = f"用户 {op_nick} 在 {event.time_str} 使 {target_nick} 退出了群聊：{event.group_id}"
            await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin)))



async def cmd_role_play(actions, Manager, Segments, event, presets, presets_tool, bot_name, bot_name_en, reminder):
    info = f"""{bot_name} {bot_name_en} - 角色扮演后台
————————————————————
{presets_tool.list_presets(presets, presets_tool.current_preset, reminder)}

发送相应的关键词，{bot_name}会尽力扮演不同角色和你交流哒！⌯>ᴗoᴗ⌯ .ᐟ.ᐟ
————————————————————
若您是 Manage_User, Super_User 或 ROOT_User，你可以管理这些角色，尝试：
    {reminder}添加预设 [name] [info] : [content]
    {reminder}删除预设 [name]
其中，name 为角色名称， info 为预设简介， content 为预设内容。"""
    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(info)))


async def cmd_add_preset(actions, Manager, Segments, event, order,
                         ADMINS, ROOT_User, CONFUSED_WORD, bot_name, bot_name_en, reminder,
                         presets, presets_tool, PRESET_DIR):
    if str(event.user_id) not in ADMINS:
        return
    match = re.match(r"添加预设\s+(.+?)\s+(.+?)\s*[:：]\s*(.+)", order, re.DOTALL)
    if not match:
        info = f"""{bot_name} {bot_name_en} - 角色扮演后台
————————————————————
添加预设 格式错误。
用法：{reminder}添加预设 [name] [info] : [content]
其中，name 为角色名称， info 为预设简介， content 为预设内容。

示例：{reminder}添加预设 助手 让{bot_name}成为你有帮助的助手！ : 你是一个有帮助的助手。"""
        await _send(actions, Manager, Segments, event, info)
        return
    name, pinfo, pcontent = match.groups()
    while True:
        preset_id = "p" + str(random.randint(1000000, 9999999))
        if not os.path.exists(os.path.join(PRESET_DIR, f"{preset_id}.txt")):
            break
    existing_preset_id = None
    for pid, pdata in presets.items():
        if pdata["name"] == name:
            existing_preset_id = pid
            break
    if existing_preset_id:
        preset_id = existing_preset_id
        preset_path = os.path.join(PRESET_DIR, presets[preset_id]["path"])
        with open(preset_path, "w", encoding="utf-8") as f:
            f.write(pcontent)
        presets[preset_id]["info"] = pinfo
    else:
        preset_filename = f"{preset_id}.txt"
        preset_path = os.path.join(PRESET_DIR, preset_filename)
        with open(preset_path, "w", encoding="utf-8") as f:
            f.write(pcontent)
        presets[preset_id] = {"name": name, "uid": [], "info": pinfo, "path": preset_filename}
    presets_tool.write_presets(presets)
    verb = "更新现有" if existing_preset_id else "添加"
    await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(f"用户 {event.user_id} 在群 {event.group_id} 中{verb}预设: {name} ")))
    info = f"""{bot_name} {bot_name_en} - 角色扮演后台
————————————————————
已{verb}预设: {name}"""
    await _send(actions, Manager, Segments, event, info)


async def cmd_del_preset(actions, Manager, Segments, event, order,
                         ADMINS, ROOT_User, CONFUSED_WORD, bot_name, bot_name_en, reminder,
                         presets, presets_tool, PRESET_DIR, logger):
    if str(event.user_id) not in ADMINS:
        return
    match = re.match(r"删除预设\s+(.+)", order)
    if not match:
        info = f"""{bot_name} {bot_name_en} - 角色扮演后台
————————————————————
删除预设 格式错误。
用法：{reminder}删除预设 [name] 
其中，name 为角色名称。

示例：{reminder}删除预设 助手"""
        await _send(actions, Manager, Segments, event, info)
        return
    name = match.group(1).strip()
    preset_id_to_delete = None
    for preset_id, preset_data in presets.items():
        if preset_data["name"] == name:
            preset_id_to_delete = preset_id
            break
    if preset_id_to_delete:
        preset_path = os.path.join(PRESET_DIR, presets[preset_id_to_delete]["path"])
        logger.info(f"Removed {preset_path}")
        os.remove(preset_path)
        del presets[preset_id_to_delete]
        presets_tool.write_presets(presets)
        await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(f"用户 {event.user_id} 在群 {event.group_id} 中删除 {name} 预设")))
        info = f"""{bot_name} {bot_name_en} - 角色扮演后台
————————————————————
已删除预设: {name}"""
        await _send(actions, Manager, Segments, event, info)


async def cmd_sleep(actions, Manager, Segments, event, ADMINS, ROOT_User, CONFUSED_WORD,
                    bot_name, suffix_manager, get_user_nickname):
    """返回 True 表示要把 stop_working 置 True。"""
    if str(event.user_id) not in ADMINS:
        await _confused(actions, Manager, Segments, event, CONFUSED_WORD, bot_name)
        return False
    nick = await get_user_nickname(event.user_id, Manager, actions)
    r_admin = f"用户 {nick} 在 {event.time_str} 休眠QQ机器人"
    await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin)))
    await _send(actions, Manager, Segments, event,
                suffix_manager.process_text(f"谢谢喵，{bot_name}睡觉去了 ヾ(＠ ˘ω˘ ＠)ノ💤", event.user_id))
    return True


async def cmd_status(actions, Manager, Segments, event, ADMINS, CONFUSED_WORD,
                     bot_name, bot_name_en, ONE_SLOGAN, second_start, time_module):
    if str(event.user_id) not in ADMINS:
        await _confused(actions, Manager, Segments, event, CONFUSED_WORD, bot_name)
        return
    system_info = get_system_info()
    feel = f"""{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
系统当前运行状况
运行时间：{seconds_to_hms(round(time_module.time() - second_start, 2))}
系统版本：{system_info["version_info"]}
体系结构：{system_info["architecture"]}
CPU占用：{system_info["cpu_usage"]}%
内存占用：{system_info["memory_usage_percentage"]}%"""
    for i, usage in enumerate(system_info["gpu_usage"]):
        feel += f"\nGPU {i} Usage：{usage * 100:.2f}%"
    await _send(actions, Manager, Segments, event, feel)


async def cmd_logout(actions, Manager, Segments, event, ADMINS, ROOT_User, CONFUSED_WORD,
                     bot_name, user_lists, get_user_nickname):
    if str(event.user_id) not in ADMINS:
        await _confused(actions, Manager, Segments, event, CONFUSED_WORD, bot_name)
        return
    user_lists.clear()
    await _send(actions, Manager, Segments, event, f"卸下包袱，{bot_name}更轻松了~ (/≧▽≦)/")
    nick = await get_user_nickname(event.user_id, Manager, actions)
    r_admin = f"用户 {nick} 在 {event.time_str} 手动清空了所有用户的 AI 对话上下文"
    await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin)))


async def cmd_modify_timing(actions, Manager, Segments, event, order,
                            ADMINS, ROOT_User, CONFUSED_WORD, bot_name, reminder,
                            get_user_nickname):
    if str(event.user_id) not in ADMINS:
        await _confused(actions, Manager, Segments, event, CONFUSED_WORD, bot_name)
        return
    try:
        tm = order[order.find("修改 ") + len("修改 "):].strip()
        if not bool(re.match(r"^([01][0-9]|2[0-3]):([0-5][0-9])$", tm[:5])):
            r = f"{bot_name}不能识别给定的时间是什么 Σ( ° △ °|||)︴\n举个🌰子：{reminder}修改 00:00 早安 —> 即可让{bot_name}在0点0分准时问候早安噢⌯oᴗo⌯"
        else:
            timing_settings = f"{tm[:5]}⊕{tm[6::].strip()}"
            with open("timing_message.ini", "w", encoding="utf-8") as f:
                f.write(timing_settings)
            r = f"{bot_name}设置成功！(*≧▽≦) "
            nick = await get_user_nickname(event.user_id, Manager, actions)
            r_admin = f"用户 {nick} 在 {event.time_str} 将机器人的定时群发消息修改为时间：{tm[:5]} \n内容：{tm[6::]}"
            await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin)))
    except Exception as e:
        r = f"{type(e)}\n{bot_name}设置失败了…… (╥﹏╥)"
    await _send(actions, Manager, Segments, event, r)


async def cmd_broadcast_msg(actions, Manager, Segments, event, order, user_message,
                            ADMINS, ROOT_User, CONFUSED_WORD, bot_name, reminder, logger,
                            send_msg_all_groups_fn, get_user_nickname):
    if str(event.user_id) not in ADMINS:
        await _confused(actions, Manager, Segments, event, CONFUSED_WORD, bot_name)
        return
    words = order.split(" ")
    if len(words) < 2 and len(event.message) == 1:
        r = f"群发格式错误 Σ( ° △ °|||)︴\n举个🌰子：{reminder}群发 {bot_name}有更新新功能啦！ —> 在所有群聊中发送消息 “{bot_name}有更新新功能啦！”"
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(r)))
        return
    logger.debug(f"消息长度: {len(event.message)}")
    if len(event.message) > 0 and isinstance(event.message[0], Segments.Text):
        new_text = str(event.message[0]).replace(f"{reminder}群发 ", "", 1) if f"{reminder}群发 " in str(event.message[0]) else str(event.message[0]).replace(f"{reminder}群发", "", 1)
        if len(event.message) > 1:
            m = Manager.Message(Segments.Text(new_text), *event.message[1:])
        else:
            m = Manager.Message(Segments.Text(new_text))
    else:
        m = event.message
    words.pop(0)
    word = " ".join(words)
    nick = await get_user_nickname(event.user_id, Manager, actions)
    r_admin = f"用户 {nick} 在 {event.time_str} 启动群发消息：\n"
    await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin), *m))
    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text("已启动群发消息：\n"), *m))
    await send_msg_all_groups_fn(word, actions, m)


async def cmd_leave_group(actions, Manager, Segments, event,
                          SUPERS, ROOT_User, CONFUSED_WORD, bot_name,
                          get_user_nickname):
    import asyncio as _aio
    if str(event.user_id) not in SUPERS:
        await _confused(actions, Manager, Segments, event, CONFUSED_WORD, bot_name)
        return
    nick = await get_user_nickname(event.user_id, Manager, actions)
    r_admin = f"用户 {nick} 在 {event.time_str} 使机器人退出了群聊：{event.group_id}"
    await actions.send(user_id=ROOT_User[0], message=Manager.Message(Segments.Text(r_admin)))
    await _send(actions, Manager, Segments, event, "呜呜呜，各位再见了……")
    await _aio.sleep(3)
    await actions.custom.set_group_leave(group_id=event.group_id, is_dismiss=True)


async def cmd_recall(actions, Manager, Segments, event, ADMINS, CONFUSED_WORD, bot_name):
    if str(event.user_id) not in ADMINS:
        await _confused(actions, Manager, Segments, event, CONFUSED_WORD, bot_name)
        return
    if isinstance(event.message[0], Segments.Reply):
        try:
            await actions.del_message(event.message[0].id)
        except Exception:
            pass


async def cmd_assign_title_other(actions, Manager, Segments, event, order,
                                 SUPERS, CONFUSED_WORD, bot_name, logger):
    if str(event.user_id) not in SUPERS:
        await _confused(actions, Manager, Segments, event, CONFUSED_WORD, bot_name)
        return
    try:
        start_index = order.find("给他人分配头衔")
        if start_index == -1:
            return
        result = order[start_index + len("给他人分配头衔"):].strip()
        match = re.search(r"(\d+)\s+(.+)", result)
        if not match:
            await _send(actions, Manager, Segments, event, "指令格式有误，请使用 用户ID 头衔 的格式。")
            return
        userid114 = match.group(1)
        title114 = match.group(2).strip()
        if len(title114) > 6:
            await _send(actions, Manager, Segments, event, "头衔不能超过6个字！")
            return
        try:
            await actions.custom.set_group_special_title(group_id=event.group_id, user_id=userid114, title=title114)
            await _send(actions, Manager, Segments, event, "已设置！")
        except Exception as set_title_error:
            logger.error(f"设置头衔失败: {set_title_error}")
            await _send(actions, Manager, Segments, event, f"设置头衔失败：{set_title_error}")
    except Exception as e:
        logger.error(f"处理分配头衔指令时出错: {e}")
        await _send(actions, Manager, Segments, event, "格式有误或发生未知错误！")


async def cmd_assign_title_self(actions, Manager, Segments, event, order,
                                SUPERS, self_service_titles):
    titletext = order[order.find("分配头衔 ") + len("分配头衔 "):].strip()
    if len(titletext) > 6:
        await _send(actions, Manager, Segments, event, "头衔不能超过6个字！")
        return
    if str(event.user_id) in SUPERS:
        await actions.custom.set_group_special_title(group_id=event.group_id, user_id=event.user_id, title=titletext)
        await _send(actions, Manager, Segments, event, "已设置！")
    elif self_service_titles:
        await actions.custom.set_group_special_title(group_id=event.group_id, user_id=event.user_id, special_title=titletext, duration=-1)
        await _send(actions, Manager, Segments, event, "已设置！")
    else:
        await _send(actions, Manager, Segments, event, "当前功能未开放,请联系管理员(高级用户 或者 根用户)开放权限！")
