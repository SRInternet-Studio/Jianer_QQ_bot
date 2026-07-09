import json
import traceback
import uuid
from datetime import datetime

import aiohttp
from jianer import common as Manager, segments as Segments
from jianer.plugins import PluginMetadata

from bot import plugin_state

__plugin_meta__ = PluginMetadata(
    name="jianerbot-plugin-check-account",
    description="查看用户资料。",
    usage="{reminder}开 【@一个用户/QQ号】 —> 打开该用户的账户 👁",
    requires={"jianerbot-plugin-alconna"},
)


async def get_user_info_from_ws(user_id):
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(plugin_state.websocket_url()) as ws:
            request_id = str(uuid.uuid4())
            payload = {
                "action": "get_stranger_info",
                "params": {"user_id": user_id, "no_cache": True},
                "echo": request_id,
            }
            await ws.send_str(json.dumps(payload))

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    response_data = json.loads(msg.data)
                    if response_data.get("echo") == request_id:
                        return response_data.get("data")
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
    return None


async def dispatch(event, actions) -> bool:
    if plugin_state.current_stage() != "command":
        return False
    order = plugin_state.current_order()
    if not (order == "开" or order.startswith("开 ")):
        return False

    runtime = plugin_state.get_runtime()
    bot_name = runtime["bot_name"]
    bot_name_en = runtime["bot_name_en"]
    one_slogan = runtime["one_slogan"]
    admins = runtime["admins"]
    supers = runtime["supers"]
    root_users = [str(item) for item in runtime["root_users"]]

    uid = 0
    openme = False
    for item in getattr(event, "message", []):
        if isinstance(item, Segments.At):
            uid = int(item.qq)
            break

    if uid == 0:
        uid_str = order.removeprefix("开").strip() or getattr(event, "user_id", "")
        try:
            uid = int(uid_str)
        except (ValueError, TypeError):
            r = f'''{bot_name} {bot_name_en} - {one_slogan}
————————————————————
失败: {uid_str} 不是一个有效的用户'''
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
            return True
    if uid == event.self_id:
        uid = event.user_id
        openme = True

    try:
        user_info = await get_user_info_from_ws(uid)
        print(f"Debug: user_info type: {type(user_info)}, content: {user_info}")
    except Exception as e:
        print(f"get_user {uid} failed via websocket: {e}")
        r = f'''{bot_name} {bot_name_en} - {one_slogan}
————————————————————
失败: 获取用户信息时出错: {e}'''
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
        return True

    if not user_info:
        r = f'''{bot_name} {bot_name_en} - {one_slogan}
————————————————————
失败: 未能获取到 {uid} 的信息，可能 {uid} 不是一个有效的用户，请稍后重试。'''
        print(f"get_user {uid} failed: no user_info returned")
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
    elif isinstance(user_info, dict) and user_info.get("user_id"):
        framework = await actions.get_version_info()
        framework = framework.data.raw
        if "NapCat" in framework.get("app_name"):
            avatar, r = parser_user_info_napcat(user_info, admins, supers, root_users)
        else:
            avatar, r = parse_user_info(user_info, admins, supers, root_users)
        print(f"get_user {uid} successfully")
        if avatar:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(avatar), Segments.Text(r)))
        else:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
        if openme:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("你打开了自己的账户。")))
    else:
        r = f'''{bot_name} {bot_name_en} - {one_slogan}
————————————————————
失败: 返回的用户信息格式不正确。'''
        print(f"get_user {uid} failed: invalid user_info format: {type(user_info)} - {user_info}")
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))

    return True


def parser_user_info_napcat(user_dict, admins, supers, root_users):
    try:
        avatar = f"https://q1.qlogo.cn/g?b=qq&nk={user_dict.get('uin', '未知')}&s=640"
        register_time = user_dict.get("regTime", "")
        try:
            register_time = datetime.fromtimestamp(register_time)
            register_time = register_time.strftime("%Y.%m.%d %H:%M:%S")
        except (ValueError, TypeError):
            register_time = "未知时间"

        is_vip = user_dict.get("is_vip", False)
        vip_level = user_dict.get("vip_level", 0)
        is_year_vip = user_dict.get("is_years_vip", False)

        status_msg = "(框架不支持)"
        if str(user_dict.get("user_id", "未知")) in root_users:
            status_user = "ROOT_User"
        elif str(user_dict.get("user_id", "未知")) in supers:
            status_user = "Super_User"
        elif str(user_dict.get("user_id", "未知")) in admins:
            status_user = "Manage_User"
        else:
            status_user = "普通用户"

        result = f"""昵称: {user_dict.get('nickname', '未知')}
状态: {status_msg}
QQ号: {user_dict.get('uin', '未知')}
QID: {user_dict.get('qid', '未知')}
性别: {'男' if user_dict.get('sex') == 'male' else '女'}
年龄: {user_dict.get('age', '未知')}
权限: {status_user}
QQ等级: {user_dict.get('qqLevel', '未知')}
个性签名: {user_dict.get('longNick', '暂无签名')}
注册时间: {register_time}
超级会员: {'是' if is_vip else '否'}
会员等级: {vip_level}
年费会员: {'是' if is_year_vip else '否'}"""

        return avatar, result

    except Exception:
        print(f"解析失败: {traceback.format_exc()}")
        return "", "无法打开该用户的账户"


def parse_user_info(user_dict, admins, supers, root_users):
    try:
        avatar = user_dict.get("avatar", "")
        register_time = user_dict.get("RegisterTime", "")
        try:
            dt = datetime.strptime(register_time, "%Y-%m-%dT%H:%M:%SZ")
            register_time = dt.strftime("%Y.%m.%d %H:%M:%S")
        except (ValueError, TypeError):
            register_time = "未知时间"

        business = user_dict.get("Business", [])
        is_vip = any(item.get("type") == 1 for item in business)
        vip_level = next((item.get("level", 0) for item in business if item.get("type") == 1), 0)
        is_year_vip = any(item.get("isyear") == 1 for item in business if item.get("type") == 1)

        status_msg = user_dict.get("status", {}).get("message", "暂无状态")
        if str(user_dict.get("user_id", "未知")) in root_users:
            status_user = "ROOT_User"
        elif str(user_dict.get("user_id", "未知")) in supers:
            status_user = "Super_User"
        elif str(user_dict.get("user_id", "未知")) in admins:
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
注册时间: {register_time}
超级会员: {'是' if is_vip else '否'}
会员等级: {vip_level}
年费会员: {'是' if is_year_vip else '否'}"""

        return avatar, result

    except Exception:
        print(f"解析失败: {traceback.format_exc()}")
        return "", "无法打开该用户的账户"
