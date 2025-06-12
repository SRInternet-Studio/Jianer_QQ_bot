from PIL import Image
from typing import Tuple, Optional, Any
import platform
import psutil
import GPUtil
import io, gc, os
import edge_tts

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
        return output_path
    except Exception as e:
        print(e)
        return False

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

    # GPU信息（是否有）
    gpus = GPUtil.getGPUs()
    if gpus:
        gpu_count = len(gpus)
        gpu_usage = [gpu.load for gpu in gpus]
    else:
        gpu_count = 0
        gpu_usage = []

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

async def get_user_info(uid, Manager, actions) -> Tuple[bool, Optional[dict]]:
    try:
        gc.collect()
        info = Manager.Ret.fetch(await actions.custom.get_stranger_info(user_id=uid, no_cache=True))
        if 'nickname' not in info.data.raw:
            raise ValueError(f"{uid} is not a valid user ID.")
        return True, info.data.raw
    except Exception as e:
        print(f"tools: 获取用户 {uid} 信息失败: {e}")
        return False, str(uid)
    
async def get_user_nickname(uid, Manager, actions) -> str:
    s, user_info = await get_user_info(uid, Manager, actions)
    if s:
        return f"@{user_info['nickname']}({uid})"
    else:
        return str(uid)