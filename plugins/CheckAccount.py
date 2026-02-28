from Tools.log_helper import project_log, LOG_LEVELS
import re
import json, time, gc
from datetime import datetime

from Hyper import Configurator, Listener
Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())

TRIGGHT_KEYWORD = "开"
HELP_MESSAGE = f"{Configurator.cm.get_cfg().others['reminder']}开 【@一个用户/QQ号】 —> 打开该用户的账户 👁"
MAX_retry = 5
retry_sleep = 1

async def on_message(event, actions: Listener.Actions, Manager, Segments, 
                     order, bot_name, bot_name_en, ONE_SLOGAN, ADMINS, SUPERS, ROOT_User):
    uid = 0
    for i in event.message:
        if isinstance(i, Segments.At):
            uid = int(i.qq)
            break
        
    if uid == 0:
        uid = order[order.find(f"{TRIGGHT_KEYWORD} ") + len(f"{TRIGGHT_KEYWORD} "):].strip()
        
    retry_time = 0
    while True:
        retry_time += 1
        project_log(f"try to get_user {uid} time: {retry_time}")

        gc.collect()
        nikename = Manager.Ret.fetch(await actions.custom.get_stranger_info(user_id=uid, no_cache=True)).data.raw
        if len(nikename) == 0:
            r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: {uid} 不是一个有效的用户'''
            project_log(f"get_user {uid} failed: didn't found user", level=LOG_LEVELS.ERROR)
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
            break

        else:
            if str(nikename.get('user_id', '未知')) == str(uid):
                avatar, r = parse_user_info(nikename, ADMINS, SUPERS, ROOT_User)
                project_log(f"get_user {uid} successfully")
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(avatar), Segments.Text(r)))
                break
            else:
                if retry_time > MAX_retry:
                    project_log(f"get_user {uid} failed: max retry", level=LOG_LEVELS.ERROR)
                    r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: 在 {MAX_retry} 次尝试连接服务器后，未能找到 {uid} 的信息'''
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
                    break
                else:
                    time.sleep(retry_sleep)
        
    return True

def parse_user_info(user_dict, ADMINS, SUPERS, ROOT_User):
    try:
        avatar = user_dict.get('avatar', '')
        register_time = user_dict.get('RegisterTime', '')
        try:
            dt = datetime.strptime(register_time, '%Y-%m-%dT%H:%M:%SZ')
            register_time = dt.strftime('%Y')
        except (ValueError, TypeError):
            register_time = '未知年份'
            
        business = user_dict.get('Business', [])
        is_vip = any(item.get('type') == 1 for item in business)
        vip_level = next((item.get('level', 0) for item in business if item.get('type') == 1), 0)
        is_year_vip = any(item.get('isyear') == 1 for item in business if item.get('type') == 1)

        status_msg = user_dict.get('status', {}).get('message', '暂无状态')
        if str(user_dict.get('user_id', '未知')) in ROOT_User:
            status_user = "ROOT_User"
        elif str(user_dict.get('user_id', '未知')) in SUPERS:
            status_user = "Super_User"
        elif str(user_dict.get('user_id', '未知')) in ADMINS:
            status_user = "Manage_User"
        else:
            status_user = "普通用户"
            
        result = f"""昵称: {user_dict.get('nickname', '未知')}
状态: {status_msg}
QQ号: {user_dict.get('user_id', '未知')}
QID: {user_dict.get('q_id', '未知')}
性别: {'男' if user_dict.get('sex') == 'male' else '女'}
年龄: {user_dict.get('age', '未知')}
权限: {status_user}
QQ等级: {user_dict.get('level', '未知')}
个性签名: {user_dict.get('sign', '暂无签名')}
注册年份: {register_time}
超级会员: {'是' if is_vip else '否'}
会员等级: {vip_level}
年费会员: {'是' if is_year_vip else '否'}"""

        return (avatar, result)

    except Exception as e:
        project_log(f"解析失败: {e}", level=LOG_LEVELS.ERROR)
        return ("", "无法打开该用户的账户")
