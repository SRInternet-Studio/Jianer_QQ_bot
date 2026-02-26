import json
import asyncio
import aiohttp
import datetime
import random
from typing import Optional, Any
from Hyper import Configurator
Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())
url = f"ws://{Configurator.cm.get_cfg().connection.host}:{Configurator.cm.get_cfg().connection.port}"

def send_log(level, message):
    current_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-4]
    level_icon = "ℹ️" if level == "INFO" else "⚠️" if level == "WARNING" else "❌" if level == "ERROR" else "📝"
    print(f" [{current_time}] {level_icon} {level}  {message}")
    
async def ws_custom_api(action:str,params:dict) -> dict:
    max_retries = 2
    timeout_seconds = 8.0
    random_int = random.randint(1, 114)
    async with aiohttp.ClientSession() as session:
        for attempt in range(max_retries):
            try:
                send_log("INFO", f"尝试连接 WebSocket (第 {attempt + 1} 次)")
                async with session.ws_connect(url, timeout=timeout_seconds) as ws:
                    send_log("INFO", "WebSocket 连接已建立。")
                    request_payload = {
                        "action": action,
                        "params": params,
                        "echo": f"{action}_{random_int}"
                    }
                    await ws.send_json(request_payload)
                    send_log("INFO", "已发送请求")
                    try:
                        async for msg in ws:
                            if msg.type in (aiohttp.WSMsgType.PING, aiohttp.WSMsgType.PONG):
                                continue
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                response = json.loads(msg.data)
                                if response.get("echo") == f"{action}_{random_int}":
                                    send_log("INFO", "接收到响应")
                                    return response
                                else:
                                    continue
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                send_log("ERROR", f"发生错误: {ws.exception()}")
                                return {"status": "failed", "retcode": -1, "msg": f"WebSocket错误: {str(ws.exception())}"}
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                                send_log("INFO", "WebSocket 连接已关闭。")
                                break
                    except asyncio.TimeoutError:
                        send_log("ERROR", "请求超时")
                        if attempt == max_retries - 1:
                            return {"status": "failed", "retcode": -4, "msg": "请求超时"}
                        else:
                            await asyncio.sleep(0.5)
                            continue
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                send_log("ERROR", f"连接或运行发生错误: {e}")
                if attempt == max_retries - 1:
                    return {"status": "failed", "retcode": -5, "msg": f"WebSocket连接/运行错误: {str(e)}"}
                else:
                    await asyncio.sleep(0.5)
    return {"status": "failed", "retcode": -7, "msg": "所有尝试均失败"}
