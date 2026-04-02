from PIL import Image, ImageDraw, ImageFont
from typing import Tuple, Optional, Any
import platform
import psutil
import io, gc, os
import shutil, subprocess
import warnings
import edge_tts
import hashlib
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
    pynvml_module = None
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                category=FutureWarning,
                message=".*pynvml package is deprecated.*"
            )
            import pynvml as pynvml_module
    except Exception:
        pynvml_module = None

    if pynvml_module is not None:
        try:
            pynvml_module.nvmlInit()
            try:
                device_count = pynvml_module.nvmlDeviceGetCount()
                if device_count > 0:
                    gpu_count = device_count
                    for i in range(device_count):
                        handle = pynvml_module.nvmlDeviceGetHandleByIndex(i)
                        utilization = pynvml_module.nvmlDeviceGetUtilizationRates(handle)
                        load = utilization.gpu / 100.0
                        gpu_usage.append(load)
            finally:
                pynvml_module.nvmlShutdown()
        except Exception as e:
            print(f"pynvml error: {e}")

    # GPU信息（是否有）
    try:
        if shutil.which("nvidia-smi"):
            output = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                encoding='utf-8',
                stderr=subprocess.DEVNULL
            )
            gpu_usage = [float(x.strip()) / 100.0 for x in output.strip().split('\n') if x.strip()]
            gpu_count = len(gpu_usage)
        else:
            gpu_count = 0
            gpu_usage = []
    except Exception as e:
        print(f"Error getting GPU info: {e}")
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


def create_help_message_image(help_text: str, background_path: str = "assets/bg.jpeg") -> Optional[str]:
    try:
        if not help_text:
            return None
        if not os.path.isabs(background_path):
            background_path = os.path.abspath(background_path)
        if not os.path.isfile(background_path):
            return None

        image = Image.open(background_path).convert("RGB")
        target_width = 1920
        target_height = 1080
        image = image.resize((target_width, target_height), Image.Resampling.LANCZOS)

        overlay = Image.new("RGBA", (target_width, target_height), (255, 255, 255, 0))
        draw = ImageDraw.Draw(overlay)

        panel_left = 40
        panel_top = 36
        panel_right = target_width - 40
        panel_bottom = target_height - 36
        panel_radius = 28
        draw.rounded_rectangle(
            (panel_left, panel_top, panel_right, panel_bottom),
            radius=panel_radius,
            fill=(255, 255, 255, 220),
            outline=(255, 255, 255, 245),
            width=2,
        )

        font_candidates = [
            r"C:\Windows\Fonts\msyh.ttc",
            r"C:\Windows\Fonts\msyhbd.ttc",
            r"C:\Windows\Fonts\simhei.ttf",
            r"C:\Windows\Fonts\simsun.ttc",
        ]
        font = None
        for fp in font_candidates:
            if os.path.isfile(fp):
                try:
                    font = ImageFont.truetype(fp, 28)
                    break
                except Exception:
                    pass
        if font is None:
            font = ImageFont.load_default()

        text_padding_x = 36
        text_padding_y = 30
        text_x = panel_left + text_padding_x
        text_y = panel_top + text_padding_y
        max_width = (panel_right - panel_left) - text_padding_x * 2
        line_spacing = 12

        wrapped_lines = []
        for raw_line in str(help_text).splitlines():
            raw_line = raw_line.rstrip()
            if raw_line == "":
                wrapped_lines.append("")
                continue
            current = ""
            for ch in raw_line:
                probe = current + ch
                if draw.textlength(probe, font=font) <= max_width:
                    current = probe
                else:
                    if current:
                        wrapped_lines.append(current)
                    current = ch
            if current:
                wrapped_lines.append(current)

        line_height = font.getbbox("简A")[3] - font.getbbox("简A")[1]
        max_lines = int(((panel_bottom - panel_top) - text_padding_y * 2) / (line_height + line_spacing))
        if len(wrapped_lines) > max_lines and max_lines > 0:
            wrapped_lines = wrapped_lines[:max_lines]
            if wrapped_lines[-1]:
                last = wrapped_lines[-1]
                while last and draw.textlength(last + "...", font=font) > max_width:
                    last = last[:-1]
                wrapped_lines[-1] = (last or "") + "..."

        cursor_y = text_y
        for line in wrapped_lines:
            draw.text((text_x, cursor_y), line, fill=(31, 58, 78, 255), font=font)
            cursor_y += line_height + line_spacing

        merged = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
        out_dir = os.path.abspath(os.path.join("static", "help_cards"))
        os.makedirs(out_dir, exist_ok=True)
        digest = hashlib.md5(help_text.encode("utf-8")).hexdigest()
        out_path = os.path.join(out_dir, f"help_{digest}.jpg")
        merged.save(out_path, format="JPEG", quality=92)
        return out_path
    except Exception:
        return None
