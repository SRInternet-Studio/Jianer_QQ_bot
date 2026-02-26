from PIL import Image
from typing import Tuple, Optional, Any
import platform
import psutil
import pynvml
import io, gc, os
import edge_tts
from .user_info import get_user_info_from_websocket, get_nickname_by_userid
from urllib.parse import urlparse, urlunparse

def title() -> str:
    return r'''# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# ~      _ _                         _   _ _______  _______   _____  ~
# ~     | (_) __ _ _ __   ___ _ __  | \ | | ____\ \/ /_   _| |___ /  ~
# ~  _  | | |/ _` | '_ \ / _ \ '__| |  \| |  _|  \  /  | |     |_ \  ~
# ~ | |_| | | (_| | | | |  __/ |    | |\  | |___ /  \  | |    ___) | ~
# ~  \___/|_|\__,_|_| |_|\___|_|    |_| \_|_____/_/\_\ |_|   |____/  ~
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~'''

async def amain(TEXT, voiceColor, rate, volume, pitch):
    try:
        communicate = edge_tts.Communicate(TEXT, voiceColor, rate = rate, volume=volume, pitch=pitch)
        
        tts_num = 0
        output_path_base = r"./responseVoice"
        output_path = f"{os.path.abspath(output_path_base)}_{tts_num}.wav"
        while os.path.exists(output_path):
            tts_num += 1
            output_path = f"{os.path.abspath(output_path_base)}_{tts_num}.wav"
            
        await communicate.save(output_path)
        
        del communicate
        gc.collect()  # 强制垃圾回收
        
        return output_path
    except Exception as e:
        print(e)
        return False
    
def replace_scheme_with_http(url: str) -> str:
    parsed_url = urlparse(url)
    if parsed_url.scheme == 'https':
        parsed_url = parsed_url._replace(scheme='http')
    return urlunparse(parsed_url)

def seconds_to_hms(total_seconds):
    hours = total_seconds // 3600
    remaining_seconds = total_seconds % 3600
    minutes = remaining_seconds // 60
    seconds = remaining_seconds % 60
    return f"{hours}h, {minutes}m, {seconds}s"

def verfiy_pixiv(file_path):
    try:
        img = Image.open(file_path)
        img.verify()  # 验证图像
        img.close()
        return True
    except (IOError, SyntaxError) as e:
        print(f"Error: {e}")
        return False

def get_system_info():
    # 系统
    version_info = platform.platform()
    architecture = platform.architecture()
    cpu_count = psutil.cpu_count(logical=True)
    cpu_usage = psutil.cpu_percent(interval=1)

    # 内存
    virtual_memory = psutil.virtual_memory()
    total_memory = virtual_memory.total
    used_memory = virtual_memory.used
    memory_usage_percentage = virtual_memory.percent

    # GPU信息（使用pynvml）
    gpu_count = 0
    gpu_usage = []
    try:
        pynvml.nvmlInit()
        try:
            device_count = pynvml.nvmlDeviceGetCount()
            if device_count > 0:
                gpu_count = device_count
                for i in range(device_count):
                    handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                    utilization = pynvml.nvmlDeviceGetUtilizationRates(handle)
                    load = utilization.gpu / 100.0
                    gpu_usage.append(load)
        finally:
            pynvml.nvmlShutdown()              # 无论是否成功获取，都关闭NVML
    except pynvml.NVMLError as e:
        # 没有NVIDIA驱动/GPU，或初始化失败，忽略错误
        print(f"pynvml error: {e}")
    except Exception as e:
        # 其他意外错误
        print(f"Unexpected error in GPU detection: {e}")

    return {
        "version_info": version_info,
        "architecture": architecture,
        "cpu_count": cpu_count,
        "cpu_usage": cpu_usage,
        "total_memory": total_memory,
        "used_memory": used_memory,
        "memory_usage_percentage": memory_usage_percentage,
        "gpu_count": gpu_count,
        "gpu_usage": gpu_usage,
    }


def deal_image(i):
    img = Image.open(io.BytesIO(i))

    # 压缩图像
    buffer = io.BytesIO()
    quality = 100  # 从100开始，逐渐降低质量直到小于10MB
    max_size = 10 * 1024 * 1024  # 10MB

    # 循环压缩图像，直到达到指定大小
    while True:
        buffer.seek(0)
        img.save(buffer, format='JPEG', quality=quality)
        if buffer.tell() < max_size or quality <= 10:  # 停止条件
            break
        quality -= 5  # 每次减少质量
        
    # 最终的压缩图像存储在buffer中
    return buffer.getvalue()

async def user_info(uid, Manager, actions) -> Tuple[bool, Optional[dict]]:
    geted_user_info = get_user_info(uid, Manager, actions)
    return geted_user_info

async def get_user_info(uid, Manager, actions) -> Tuple[bool, Optional[dict]]:
    """
    获取用户信息（tools.py版本，避免与get_user_info.py中的函数冲突）
    
    Args:
        uid (int): 用户QQ号
        Manager: Manager对象
        actions: actions对象
        
    Returns:
        tuple: (是否成功, 用户信息字典或错误信息)
    """
    try:
        user_info = await get_user_info_from_websocket(uid, Manager, actions)


        if user_info and isinstance(user_info, dict):
            return True, user_info
        else:
            return False, f"无法获取用户 {uid} 的信息"
    except Exception as e:
        print(f"tools: 获取用户 {uid} 信息失败: {e}")
        return False, str(e)
    
async def get_user_nickname(uid, Manager, actions) -> str:
    """
    获取用户昵称（tools.py版本，避免与get_user_info.py中的函数冲突）
    
    Args:
        uid (int): 用户QQ号
        Manager: Manager对象
        actions: actions对象
        
    Returns:
        str: 格式化的用户昵称
    """
    try:
        nickname = await get_nickname_by_userid(uid, Manager, actions)
        if nickname and nickname != '未知用户':
            return f"{nickname}"
        else:
            return str(uid)
    except Exception as e:
        print(f"tools: 获取用户 {uid} 昵称失败: {e}")
        return str(uid)
    
async def get_user_nickname_with_userid(uid, Manager, actions) -> str:
    """
    获取用户昵称（tools.py版本，避免与get_user_info.py中的函数冲突）
    
    Args:
        uid (int): 用户QQ号
        Manager: Manager对象
        actions: actions对象
        
    Returns:
        str: 格式化的用户昵称
    """
    try:
        nickname = await get_nickname_by_userid(uid, Manager, actions)
        if nickname and nickname != '未知用户':
            return f"{nickname}({uid})"
        else:
            return str(uid)
    except Exception as e:
        print(f"tools: 获取用户 {uid} 昵称失败: {e}")
        return str(uid)
    
async def replace_at_with_nickname(message, Manager, Segments, actions) -> str:
    """
    替换消息中的@用户为用户昵称
    
    Args:
        message: 包含@用户的消息
        Segments: Segments对象
        actions: actions对象
        
    Returns:
        str: 替换后的消息
    """
    new_message = []
    for segment in message:
        if isinstance(segment, Segments.At):
            nickname = await get_user_nickname(segment.qq, Manager, actions)
            new_message.append(f"@{nickname}")
        else:
            new_message.append(str(segment))
    return "".join(new_message)