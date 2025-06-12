import asyncio
from typing import Tuple, Optional
from Hyper import Configurator
import plugins.CloudBlacklistConsole.pathmagic # 保证寻找本地文件

from run import main
import app.services.config_service as config_service
Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())

TRIGGHT_KEYWORD = "Any"
HELP_MESSAGE = f"{Configurator.cm.get_cfg().others["reminder"]}群云黑名单 —> 禁止加群黑名单管理"

async def on_message(event, actions, Manager, Segments, Events, reminder, ADMINS, CONFUSED_WORD, bot_name, bot_name_en):
    event.group_id = event.group_id if hasattr(event, "group_id") else ""      
    if isinstance(event, Events.HyperListenerStartNotify):
        _ = main()
        return False
    
    elif isinstance(event, Events.GroupAddInviteEvent):
        blacklist = config_service.load_config()
        if str(event.group_id) in blacklist and str(event.group_id) != "":
            if str(event.user_id) in blacklist[str(event.group_id)]:
                await actions.set_group_add_request(flag=event.flag, sub_type=event.sub_type, approve=False, reason="你已被本群拉黑，请联系群主以解决此问题。")
                return True

    elif isinstance(event, Events.GroupMessageEvent) or isinstance(event, Events.PrivateMessageEvent):
        msg = ""
        if f"{reminder}群云黑名单" == str(event.message):
            address = main()
            msg = f'''{bot_name} {bot_name_en} - 群云黑控制台
{address}
如果你是 Manage_User 或 Super_User ，你可以通过 {reminder}查看云黑列表 检查当前被禁止加入当前群聊的人员
请前往以上地址以在后台中管理所有群云黑名单'''
            await actions.send(group_id=event.group_id, user_id=event.user_id, message=Manager.Message(Segments.Text(msg)))
            return True
        
        elif f"{reminder}查看云黑列表" == str(event.message):
            if str(event.user_id) not in ADMINS:
                msg = CONFUSED_WORD.format(bot_name=bot_name)
                await actions.send(group_id=event.group_id, user_id=event.user_id, message=Manager.Message(Segments.Text(msg)))
                return True

            try:
                blacklist = config_service.load_config()
                header = f"{bot_name} {bot_name_en} - 禁止加群人员列表\n————————————————————"
                lines = []

                # 处理全局查看
                if not event.group_id:
                    valid_groups = 0
                    for group_id, user_list in blacklist.items():
                        if not user_list:
                            continue  # 跳过空名单群组
                        
                        try:
                            displays = await asyncio.gather(*[
                                get_display(uid, Manager, actions)
                                for uid in user_list
                            ])
                            lines.append(f"群 {group_id}:\n" + "\n".join([f"  • {d}" for d in displays]))
                            valid_groups += 1
                        except Exception as e:
                            lines.append(f"群 {group_id} 信息获取失败: {str(e)}")
                    
                    msg = header + ("\n" if valid_groups else "") + "\n————————————————————\n".join(lines)
                    msg += "\n————————————————————" if valid_groups else "\n当前没有禁止加群的群组"

                # 处理指定群组查看
                else:
                    group_id = str(event.group_id)
                    if user_list := blacklist.get(group_id):
                        displays = await asyncio.gather(*[
                            get_display(uid, Manager, actions)
                            for uid in user_list
                        ])
                        msg = header + "\n" + "\n".join([f"  • {d}" for d in displays])
                    else:
                        msg = header + "\n" + "本群没有被禁止加群的人员。"

            except Exception as e:
                msg = f"获取黑名单失败: {str(e)}"
            
            await actions.send(group_id=event.group_id, user_id=event.user_id, message=Manager.Message(Segments.Text(msg)))
            return True
        else:
            # 群内禁言
            blacklist = config_service.load_config()
            if str(event.group_id) in blacklist and str(event.group_id) != "":
                if str(event.user_id) in blacklist[str(event.group_id)]:
                    nickname = await get_display(event.user_id, Manager, actions)
                    await actions.set_group_ban(group_id=event.group_id, user_id=event.user_id, duration=604800)
                    await actions.send(group_id=event.group_id, user_id=event.user_id, message=Manager.Message(Segments.Text(f"Warning：{nickname} 在黑名单内，已尝试禁言\n请联系群主和管理员。")))

            return False
    
async def get_display(uid, Manager, actions):
    s, user_info = await get_user_info(uid, Manager, actions)
    if s:
        return f"@{user_info['nickname']}({uid})"
    else:
        return str(uid)

async def get_user_info(uid, Manager, actions) -> Tuple[bool, Optional[dict]]:
    try:
        info = Manager.Ret.fetch(await actions.custom.get_stranger_info(user_id=uid, no_cache=True))
        return True, info.data.raw
    except Exception as e:
        print(f"tools: 获取用户 {uid} 信息失败: {e}")
        return False, str(uid)