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

async def create_help_message_image_async(help_text: str, background_path: str = "assets/bg.jpeg") -> Optional[str]:
    try:
        if not help_text:
            return None
        if not os.path.isabs(background_path):
            background_path = os.path.abspath(background_path)
        if not os.path.isfile(background_path):
            return None

        out_dir = os.path.abspath(os.path.join("static", "help_cards"))
        os.makedirs(out_dir, exist_ok=True)
        digest = hashlib.md5(help_text.encode("utf-8")).hexdigest()
        out_path = os.path.join(out_dir, f"help_{digest}.jpg")
        
        # 缓存机制：如果已经生成过该图片，直接返回
        if os.path.exists(out_path):
            return out_path

        import html
        from Tools.site_catch import Catcher

        safe_text = html.escape(help_text)
        bg_url = "file:///" + background_path.replace("\\", "/")

        # 文本解析逻辑：极简玻璃卡片式UI
        lines = help_text.strip().split('\n')
        html_lines = []
        
        in_commands = False
        
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
                
            is_cmd = False
            cmd, desc = None, None
            if '—>' in line:
                cmd, desc = line.split('—>', 1)
                is_cmd = True
            elif '->' in line:
                cmd, desc = line.split('->', 1)
                is_cmd = True
                
            if is_cmd:
                if not in_commands:
                    html_lines.append('<div class="commands-container">')
                    in_commands = True
                
                cmd = html.escape(cmd.strip())
                desc = html.escape(desc.strip())
                html_lines.append(f'''
                <div class="command-item">
                    <div class="cmd-badge">{cmd}</div>
                    <div class="cmd-desc">{desc}</div>
                </div>
                ''')
            else:
                if in_commands:
                    html_lines.append('</div>')
                    in_commands = False
                    
                if line.startswith('注：') or line.startswith('注意：'):
                    html_lines.append(f'<div class="notice-box">{html.escape(line)}</div>')
                elif '示例' in line or '举个' in line:
                    html_lines.append(f'<div class="example-box">{html.escape(line)}</div>')
                else:
                    if i == 0:
                        html_lines.append(f'<div class="title-line">{html.escape(line)}</div>')
                    elif i == len(lines) - 1:
                        html_lines.append(f'<div class="footer-line">{html.escape(line)}</div>')
                    else:
                        html_lines.append(f'<div class="text-line">{html.escape(line)}</div>')
                        
        if in_commands:
            html_lines.append('</div>')

        parsed_html_content = '\n'.join(html_lines)

        html_content = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>help_{digest}</title>
    <style>
        body {{
            margin: 0;
            padding: 0;
            width: 1080px;
            min-height: 1080px;
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
        .container {{
            width: 860px;
            margin: 80px auto;
            background: rgba(20, 20, 20, 0.5);
            backdrop-filter: blur(40px) saturate(120%);
            -webkit-backdrop-filter: blur(40px) saturate(120%);
            border-radius: 32px;
            border: 1px solid rgba(255, 255, 255, 0.15);
            box-shadow: 0 30px 60px rgba(0, 0, 0, 0.4);
            padding: 70px 80px;
            box-sizing: border-box;
        }}
        .content {{
            display: flex;
            flex-direction: column;
        }}
        .title-line {{
            font-size: 38px;
            font-weight: 600;
            margin-bottom: 15px;
            text-align: center;
            letter-spacing: 2px;
            color: #fff;
        }}
        .notice-box {{
            font-size: 20px;
            color: rgba(255, 255, 255, 0.6);
            margin-bottom: 45px;
            text-align: center;
            font-weight: 400;
            letter-spacing: 1px;
        }}
        .commands-container {{
            display: flex;
            flex-direction: column;
            gap: 20px;
            margin-bottom: 45px;
        }}
        .command-item {{
            display: flex;
            align-items: center;
        }}
        .cmd-badge {{
            font-size: 22px;
            font-weight: 500;
            color: #fff;
            width: 280px;
            flex-shrink: 0;
            background: rgba(255, 255, 255, 0.1);
            padding: 12px 20px;
            border-radius: 12px;
            text-align: right;
            margin-right: 24px;
            box-sizing: border-box;
            letter-spacing: 1px;
        }}
        .cmd-desc {{
            font-size: 22px;
            color: rgba(255, 255, 255, 0.85);
            font-weight: 400;
            line-height: 1.5;
            flex: 1;
        }}
        .example-box {{
            font-size: 20px;
            color: rgba(255, 255, 255, 0.6);
            text-align: center;
            margin-bottom: 30px;
            font-style: italic;
        }}
        .footer-line {{
            text-align: center;
            font-size: 24px;
            color: rgba(255, 255, 255, 0.9);
            font-weight: 500;
            margin-top: 10px;
            letter-spacing: 1px;
        }}
        .text-line {{
            font-size: 22px;
            color: rgba(255, 255, 255, 0.8);
            margin-bottom: 16px;
            text-align: center;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="content">{parsed_html_content}</div>
    </div>
</body>
</html>
"""
        temps_dir = os.path.abspath("temps")
        os.makedirs(temps_dir, exist_ok=True)
        temp_html_path = os.path.join(temps_dir, f"help_{digest}.html")
        with open(temp_html_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        catcher = await Catcher.init()
        try:
            # 使用 (1080, 0) 让高度自适应，但由于body有min-height 1080，效果会很好看
            res_path = await catcher.catch(f"file:///{temp_html_path.replace(chr(92), '/')}", (0, 0))
            if os.path.exists(res_path):
                # 转为JPEG并保存，减小体积
                img = Image.open(res_path).convert("RGB")
                img.save(out_path, format="JPEG", quality=92)
                os.remove(res_path)
        finally:
            await catcher.quit()
            if os.path.exists(temp_html_path):
                os.remove(temp_html_path)

        return out_path if os.path.exists(out_path) else None
    except Exception as e:
        import traceback
        traceback.print_exc()
        return None
