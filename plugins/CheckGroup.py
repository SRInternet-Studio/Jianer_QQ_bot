import json
import uuid

import aiohttp
from jianer.plugins import PluginMetadata
from jianer.plugins.builtin.alconna import Command, UniMessage

from bot import plugin_state

__plugin_meta__ = PluginMetadata(
    name="jianerbot-plugin-check-group",
    description="查看群资料。",
    usage="{reminder}开群 【群号码】 —> 打开该群的账户 👁",
    requires={"jianerbot-plugin-alconna"},
)

_command = f"{plugin_state.get_runtime().get('reminder', '')}开群"


async def get_group_info_from_ws(group_id):
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(plugin_state.websocket_url()) as ws:
            request_id = str(uuid.uuid4())
            payload = {
                "action": "get_group_info",
                "params": {"group_id": group_id, "no_cache": True},
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


@Command(_command).handle()
@Command(f"{_command} <group>").handle()
async def check_group(event, group: str = "") -> bool | None:
    if getattr(event, "group_id", None) is None:
        return False

    runtime = plugin_state.get_runtime()
    bot_name = runtime["bot_name"]
    bot_name_en = runtime["bot_name_en"]
    one_slogan = runtime["one_slogan"]

    uid_str = group.strip() or getattr(event, "group_id", "")
    try:
        uid = int(uid_str)
    except (ValueError, TypeError):
        r = f'''{bot_name} {bot_name_en} - {one_slogan}
————————————————————
失败: {uid_str} 不是一个有效的群号码'''
        await UniMessage.send(UniMessage.text(r))
        return

    try:
        group_info = await get_group_info_from_ws(uid)
        print(f"Debug: group_info type: {type(group_info)}, content: {group_info}")
    except Exception as e:
        print(f"get_group {uid} failed via websocket: {e}")
        r = f'''{bot_name} {bot_name_en} - {one_slogan}
————————————————————
失败: 获取群信息时出错: {e}'''
        await UniMessage.send(UniMessage.text(r))
        return

    if not group_info:
        r = f'''{bot_name} {bot_name_en} - {one_slogan}
————————————————————
失败: 未能获取到 {uid} 的信息，可能 {uid} 不是一个有效的群号码，请稍后重试。'''
        print(f"get_group {uid} failed: no group_info returned")
        await UniMessage.send(UniMessage.text(r))
    elif isinstance(group_info, dict) and group_info.get("group_id"):
        r, groupimg = parse_group_info(group_info)
        print(f"get_group {uid} successfully")
        if groupimg:
            await UniMessage.send(UniMessage.image(groupimg), UniMessage.text(r))
        else:
            await UniMessage.send(UniMessage.text(r))
    else:
        r = f'''{bot_name} {bot_name_en} - {one_slogan}
————————————————————
失败: 返回的群组信息格式不正确。'''
        print(f"get_group {uid} failed: invalid group_info format: {type(group_info)} - {group_info}")
        await UniMessage.send(UniMessage.text(r))


def parse_group_info(group_dict):
    try:
        result = f"""群名称: {group_dict.get('group_name', '未知')}
群号: {group_dict.get('group_id', '未知')}
群人数: {group_dict.get('member_count', '未知')}
人数上限: {group_dict.get('max_member_count', '未知')}"""
        groupimg = f"https://p.qlogo.cn/gh/{group_dict.get('group_id', '未知')}/{group_dict.get('group_id', '未知')}/640"
        return result, groupimg

    except Exception as e:
        print(f"解析失败: {e}")
        return "", "无法打开该群的信息"
