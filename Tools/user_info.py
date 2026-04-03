import json
import asyncio
import aiohttp
from aiohttp import ClientTimeout

# WebSocket URL配置
with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

protocol = config.get("protocol", "OneBot")
connections = config.get("Connections", {})
conn_config = connections.get(protocol, connections.get("OneBot", {}))
is_milky = str(protocol).lower() == "milky"
ws_path = "/event" if is_milky else ""
WEBSOCKET_URL = f"ws://{conn_config.get('host', '127.0.0.1')}:{conn_config.get('port', 8080)}{ws_path}"
WS_HEADERS = {}
if conn_config.get("auth"):
    WS_HEADERS["Authorization"] = f"Bearer {conn_config.get('auth')}"


async def get_user_info_from_websocket(user_id, Manager=None, actions=None):
    """
    通过WebSocket获取用户信息
    
    Args:
        user_id (int): 用户QQ号
        Manager: Manager对象（保持兼容性，实际不使用）
        actions: actions对象（保持兼容性，实际不使用）
        
    Returns:
        tuple: (是否成功, 用户信息字典)
    """
    try:
        if actions is not None:
            try:
                result = await actions.get_stranger_info(user_id=user_id)
                data = getattr(result, "data", None)
                raw = getattr(data, "raw", None)
                if isinstance(raw, dict) and raw:
                    return True, raw
            except Exception:
                pass
        # 设置超时时间：连接超时10秒，总超时30秒
        timeout = ClientTimeout(total=30, connect=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            connect_kwargs = {"headers": WS_HEADERS} if WS_HEADERS else {}
            async with session.ws_connect(WEBSOCKET_URL, **connect_kwargs) as ws:
                # 构造请求数据
                request_data = {
                    "action": "get_stranger_info",
                    "params": {
                        "user_id": user_id
                    },
                    "echo": f"get_user_info_{user_id}"
                }
                
                # 发送请求
                await ws.send_str(json.dumps(request_data))
                
                # 接收响应，设置接收超时
                try:
                    async with asyncio.timeout(20):  # 20秒接收超时
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                response = json.loads(msg.data)
                                if response.get('echo') == f"get_user_info_{user_id}":
                                    if response.get('status') == 'ok':
                                        return True, response.get('data')
                                    else:
                                        print(f"获取用户信息失败: {response.get('message', '未知错误')}")
                                        return False, None
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                print(f'WebSocket错误: {ws.exception()}')
                                return False, None
                except asyncio.TimeoutError:
                    print(f"接收用户信息超时 (用户ID: {user_id})")
                    return False, None
                        
    except Exception as e:
        print(f"连接WebSocket时出错: {e}")
        return False, None

async def get_nickname_by_userid(user_id, Manager=None, actions=None):
    """
    根据用户ID获取昵称
    
    Args:
        user_id (int): 用户QQ号
        Manager: Manager对象（保持兼容性，实际不使用）
        actions: actions对象（保持兼容性，实际不使用）
        
    Returns:
        str: 用户昵称，如果获取失败返回'未知用户'
    """
    success, user_info = await get_user_info_from_websocket(user_id, Manager, actions)
    if success and user_info and isinstance(user_info, dict):
        # 尝试多个可能的昵称字段
        nickname = user_info.get('nickname') or user_info.get('nick') or '未知用户'
        return f"{nickname}"
    else:
        return '未知用户'

