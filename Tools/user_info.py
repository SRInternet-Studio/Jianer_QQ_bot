import json
import asyncio
import uuid
import aiohttp
from aiohttp import ClientTimeout

# WebSocket URL配置
with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)
WEBSOCKET_URL = f"ws://{config['Connection']['host']}:{config['Connection']['port']}"
print(f"WEBSOCKET: {WEBSOCKET_URL} connected")

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
        # 设置超时时间：连接超时10秒，总超时30秒
        timeout = ClientTimeout(total=30, connect=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(WEBSOCKET_URL) as ws:
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


