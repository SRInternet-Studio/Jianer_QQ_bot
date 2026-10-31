from PIL import Image, ImageDraw, ImageFont
from typing import Tuple, Optional, Any
import platform
import psutil
import io, gc, os
import math
from pathlib import Path
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
    return await get_user_info(uid, Manager, actions)

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
        success, user_info = await get_user_info_from_websocket(uid, Manager, actions)
        if success and isinstance(user_info, dict):
            return True, user_info
        return False, f"无法获取用户 {uid} 的信息"
    except Exception as e:
        print(f"tools: 获取用户 {uid} 信息失败: {e}")
        return False, str(e)
    
def _get_sender_display_name(sender: Any) -> Optional[str]:
    if sender is None:
        return None

    for field in ("nickname", "card", "user_id"):
        value = (
            sender.get(field)
            if isinstance(sender, dict)
            else getattr(sender, field, None)
        )
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


async def get_user_nickname(uid, Manager, actions, *, sender=None) -> str:
    """
    获取用户昵称（tools.py版本，避免与get_user_info.py中的函数冲突）
    
    Args:
        uid (int): 用户QQ号
        Manager: Manager对象
        actions: actions对象
        sender: 当前事件的发送者资料，仅在协议查询失败时作为回退
        
    Returns:
        str: 格式化的用户昵称
    """
    try:
        nickname = await get_nickname_by_userid(uid, Manager, actions)
        if nickname and nickname != '未知用户':
            return f"{nickname}"
        else:
            return _get_sender_display_name(sender) or str(uid)
    except Exception as e:
        print(f"tools: 获取用户 {uid} 昵称失败: {e}")
        return _get_sender_display_name(sender) or str(uid)
    
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

async def create_help_message_image_async(help_text: str, background_path: str = "assets/bg.jpeg") -> Optional[str]:
    paths = await create_help_message_images_async(help_text, background_path)
    return paths[0] if paths else None

async def create_help_message_images_async(help_text: str, background_path: str = "assets/bg.jpeg") -> list[str]:
    """按 16:9 横排纵列分页渲染帮助内容，返回顺序分页图片路径。"""
    try:
        if not help_text:
            return []
        if not os.path.isabs(background_path):
            background_path = os.path.abspath(background_path)
        if not os.path.isfile(background_path):
            return []

        import html
        from Tools.site_catch import Catcher

        out_dir = os.path.abspath(os.path.join("static", "help_cards"))
        os.makedirs(out_dir, exist_ok=True)
        digest = hashlib.md5(help_text.encode("utf-8")).hexdigest()
        for path in tuple(Path(out_dir).glob(f"help_{digest}*")):
            os.remove(path)

        lines = [line.strip() for line in help_text.strip().split("\n") if line.strip()]
        title = html.escape(lines[0]) if lines else "帮助"
        command_lines = []
        other_lines = []
        for index, line in enumerate(lines[1:] if lines else []):
            if "->" in line:
                cmd, desc = line.split("->", 1)
                command_lines.append((cmd.strip(), desc.strip()))
            elif "—>" in line:
                cmd, desc = line.split("—>", 1)
                command_lines.append((cmd.strip(), desc.strip()))
            elif index == 0:
                other_lines.append(line)
            else:
                other_lines.append(line)

        command_count_per_page = 6
        if not command_lines:
            command_pages = [[]]
        else:
            page_count = math.ceil(len(command_lines) / command_count_per_page)
            per_page = math.ceil(len(command_lines) / page_count)
            command_pages = [
                command_lines[start:start + per_page]
                for start in range(0, len(command_lines), per_page)
            ]

        paths = []
        bg_url = "file:///" + background_path.replace("\\", "/")

        for page_index, page_commands in enumerate(command_pages):
            rows = max(1, math.ceil(len(page_commands) / 2)) if page_commands else 1
            command_html = []
            for cmd, desc in page_commands:
                command_html.append(
                    f"""
                    <div class="command-card">
                        <div class="cmd-badge">{html.escape(cmd)}</div>
                        <div class="cmd-desc">{html.escape(desc)}</div>
                    </div>
                    """
                )
            commands_html = "\n".join(command_html)
            other_html = "\n".join(
                f'<div class="text-line">{html.escape(line)}</div>'
                for line in other_lines
            ) if page_index == 0 else ""
            page_foot = ""
            if len(command_pages) > 1:
                page_foot = f'<div class="page-foot">{page_index + 1} / {len(command_pages)}</div>'

            html_content = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>help_page_{page_index}</title>
    <style>
        html {{ width: 1920px; height: 1080px; }}
        body {{
            margin: 0;
            padding: 0;
            width: 1920px;
            height: 1080px;
            overflow: hidden;
            display: flex;
            justify-content: center;
            align-items: center;
            background-color: #000;
            background-image: url('{bg_url}');
            background-size: cover;
            background-position: center;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            color: #fff;
        }}
        .screen {{
            width: 1560px;
            height: 860px;
            background: rgba(18, 18, 20, 0.54);
            backdrop-filter: blur(34px) saturate(120%);
            -webkit-backdrop-filter: blur(34px) saturate(120%);
            border: 1px solid rgba(255,255,255,0.16);
            border-radius: 28px;
            box-shadow: 0 28px 58px rgba(0,0,0,0.38);
            padding: 32px 48px 36px;
            box-sizing: border-box;
            display: flex;
            flex-direction: column;
        }}
        .title {{
            min-height: 52px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 36px;
            font-weight: 600;
            letter-spacing: 1px;
            overflow-wrap: anywhere;
        }}
        .other {{ display: flex; flex-direction: column; gap: 6px; margin: 6px 0 12px; }}
        .text-line {{
            font-size: 21px;
            color: rgba(255,255,255,0.8);
            text-align: center;
            overflow-wrap: anywhere;
        }}
        .commands {{
            flex: 1;
            min-height: 0;
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            grid-template-rows: repeat({rows}, minmax(0, 1fr));
            gap: 16px;
            align-content: space-evenly;
        }}
        .command-card {{
            min-width: 0;
            min-height: 0;
            display: grid;
            grid-template-columns: 280px minmax(0, 1fr);
            align-items: center;
            gap: 18px;
            background: rgba(255,255,255,0.1);
            border: 1px solid rgba(255,255,255,0.13);
            border-radius: 18px;
            padding: 18px 22px;
            box-sizing: border-box;
            overflow: hidden;
        }}
        .cmd-badge, .cmd-desc {{
            flex: 1;
            font-size: 20px;
            line-height: 1.45;
            padding: 10px 12px;
            overflow-wrap: anywhere;
        }}
        .cmd-badge {{
            flex: none;
            max-width: 300px;
            background: rgba(255,255,255,0.12);
            border-radius: 12px;
            text-align: center;
        }}
        .cmd-desc {{
            color: rgba(255,255,255,0.86);
        }}
        .page-foot {{
            position: fixed;
            right: 46px;
            bottom: 32px;
            font-size: 20px;
            color: rgba(255,255,255,0.78);
        }}
    </style>
</head>
<body>
    <div class="screen">
        <div class="title">{title}</div>
        <div class="other">{other_html}</div>
        <div class="commands">{commands_html}</div>
    </div>
    {page_foot}
</body>
</html>
"""

            temps_dir = os.path.abspath("temps")
            os.makedirs(temps_dir, exist_ok=True)
            temp_html_path = os.path.join(temps_dir, f"help_{digest}_{page_index}.html")
            with open(temp_html_path, "w", encoding="utf-8") as f:
                f.write(html_content)

            catcher = await Catcher.init()
            try:
                res_path = await catcher.catch(
                    f"file:///{temp_html_path.replace(chr(92), '/')}", (1920, 1080)
                )
            finally:
                await catcher.quit()
                if os.path.exists(temp_html_path):
                    os.remove(temp_html_path)

            out_path = os.path.join(out_dir, f"help_{digest}_{page_index}.jpg")
            with Image.open(res_path) as img:
                img.convert("RGB").save(out_path, format="JPEG", quality=92)
            os.remove(res_path)
            paths.append(out_path)

        return paths
    except Exception as e:
        import traceback
        traceback.print_exc()
        return []
