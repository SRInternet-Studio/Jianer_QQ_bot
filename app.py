import os
import sys
import json
import copy
import logging
import shutil
import tempfile
import subprocess
import uuid
import time
import platform
import signal
import zipfile
import re
import shlex
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from flask import Flask, request, jsonify, Response, send_from_directory
from flask_cors import CORS
import base64
import requests
from requests.exceptions import RequestException, ConnectionError, Timeout, HTTPError
from urllib.parse import urlparse, urljoin
try:
    from flask_sock import Sock
except Exception:
    Sock = None

print("Ver.260226-r")

# In standalone mode, all necessary packages are in the same directory
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

# Ensure prerequisites folder and current.json exist
prereq_dir = os.path.join(current_dir, 'prerequisites')
if not os.path.exists(prereq_dir):
    os.makedirs(prereq_dir)
current_json = os.path.join(prereq_dir, 'current.json')
if not os.path.exists(current_json):
    with open(current_json, 'w', encoding='utf-8') as f:
        json.dump({}, f)

# Try importing tools
try:
    import prerequisites.prerequisite as presets_tool
    from wizardTools import PluginsManager
    from wizardTools.GithubTools import get_github_instance, get_repo, get_folder_list, download_folder, get_file_content, check_file_exists
except ImportError as e:
    print(f"Warning: Could not import some modules: {e}")

app = Flask(__name__, static_folder='dist')
CORS(app)
sock = Sock(app) if Sock is not None else None

# Global state
install_progress = {}
install_progress_lock = threading.Lock()
napcat_progress = {}
napcat_processes = {}
jianer_processes = {}
jianer_logs_buffer = {}

# Configuration - Look for files in the current directory
CONFIG_PATH = os.path.join(current_dir, 'config.json')
APPSETTINGS_PATH = os.path.join(current_dir, 'appsettings.json')
WEBUI_CONFIG_PATH = os.path.join(current_dir, 'webui.json')
INSTANCES_PATH = os.path.join(current_dir, 'instances.json')

# Path to Jianer_QQ_bot related directories (now local)
jianer_bot_path = current_dir

DEFAULT_CONFIG = {
    "owner": [],
    "Connection": {
        "host": "127.0.0.1",
        "port": 5004
    },
    "Log_level": "INFO",
    "black_list": [],
    "Others": {
        "gemini_key": "",
        "openai_key": "",
        "deepseek_key": "",
        "default_mode": "DeepSeek",
        "bot_name": "简儿",
        "bot_name_en": "Jianer",
        "ROOT_User": [],
        "Auto_approval": [],
        "reminder": "/",
        "slogan": "",
        "TTS": {
            "voiceColor": "zh-CN-XiaoyiNeural",
            "rate": "+0%",
            "volume": "+0%",
            "pitch": "+0Hz"
        },
        "compliment": [],
        "poke_rejection_phrases": []
    }
}

DEFAULT_APPSETTINGS = {
    "Account": {
        "Uin": 0,
        "Protocol": "Linux"
    },
    "Implementations": [
        {
            "Host": "127.0.0.1",
            "Port": 5004
        }
    ],
    "SignServerUrl": ""
}

def load_json(path, default=None):
    fallback = {} if default is None else copy.deepcopy(default)
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error reading {path}: {e}")
            return fallback
    return fallback

def save_json(path, data):
    try:
        # Ensure directory exists
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Error writing {path}: {e}")
        return False

def ensure_config_files():
    if not os.path.exists(CONFIG_PATH):
        print(f"Creating default config at {CONFIG_PATH}")
        save_json(CONFIG_PATH, DEFAULT_CONFIG)
    
    if not os.path.exists(APPSETTINGS_PATH):
        print(f"Creating default appsettings at {APPSETTINGS_PATH}")
        save_json(APPSETTINGS_PATH, DEFAULT_APPSETTINGS)

# Ensure core config exists during app startup.
ensure_config_files()

@app.route('/api/settings', methods=['GET'])
def get_settings():
    # Ensure files exist before reading
    ensure_config_files()
    
    config = load_json(CONFIG_PATH, DEFAULT_CONFIG)
    appsettings = load_json(APPSETTINGS_PATH, DEFAULT_APPSETTINGS)
    
    # Load presets
    try:
        presets_data = presets_tool.read_presets()
    except Exception:
        presets_data = {}
        
    return jsonify({
        'config': config,
        'appsettings': appsettings,
        'presets': presets_data
    })

@app.route('/api/settings', methods=['POST'])
def save_settings_route():
    data = request.json
    if 'config' in data:
        save_json(CONFIG_PATH, data['config'])
    if 'appsettings' in data:
        save_json(APPSETTINGS_PATH, data['appsettings'])
    if 'presets' in data:
        r = save_presets_impl(data.get('presets'))
        if not r.get('ok', False):
            return jsonify(r), 400
    return jsonify({'ok': True, 'status': 'success'})

def save_presets_impl(presets_payload):
    try:
        if presets_payload is None:
            presets_payload = {}

        if isinstance(presets_payload, list):
            mapped = {}
            for item in presets_payload:
                if not isinstance(item, dict):
                    continue
                pid = str(item.get('id') or '').strip()
                if not pid:
                    continue
                mapped[pid] = item
            presets_payload = mapped

        if not isinstance(presets_payload, dict):
            return {'ok': False, 'error': 'Invalid presets payload'}

        new_presets = presets_payload or {}
        existing = {}
        try:
            existing = presets_tool.read_presets() or {}
        except Exception:
            existing = {}
        if 'Normal' in existing and 'Normal' not in new_presets:
            new_presets['Normal'] = existing['Normal']

        normed = {}
        for k, v in new_presets.items():
            if not isinstance(v, dict):
                v = {}
            name = str(v.get('name', '')).strip()
            info = str(v.get('info', '')).strip()
            uid = v.get('uid') or []
            if not isinstance(uid, list):
                uid = [uid]
            norm_uid = []
            for u in uid:
                try:
                    s = str(u).strip()
                    if s and s.isdigit():
                        norm_uid.append(int(s))
                except Exception:
                    continue
            path = str(v.get('path') or f'{k}.txt').strip()
            if not path:
                path = f'{k}.txt'
            normed[k] = {'name': name, 'info': info, 'uid': norm_uid, 'path': path}

        presets_tool.write_presets(normed)
        try:
            base = os.path.join(current_dir, 'prerequisites')
            os.makedirs(base, exist_ok=True)
            for k, v in normed.items():
                p = os.path.join(base, v['path'])
                if not os.path.exists(p):
                    open(p, 'a', encoding='utf-8').close()
        except Exception:
            pass

        return {'ok': True}
    except Exception as e:
        return {'ok': False, 'error': str(e)}

@app.route('/api/presets/save', methods=['POST'])
def save_presets_route():
    data = request.json or {}
    r = save_presets_impl(data.get('presets'))
    if not r.get('ok', False):
        return jsonify({'ok': False, 'error': {'type': 'bad_request', 'message': r.get('error', '保存失败')}}), 400
    return jsonify({'ok': True})

@app.route('/api/voices', methods=['GET'])
def get_voices_route():
    # This might need to be async or cached as edge_tts list_voices is async
    # For now returning empty or implementation needed
    # We can run the async function in a sync wrapper
    import asyncio
    try:
        import edge_tts
    except Exception as e:
        return jsonify({'voices': [], 'error': f'edge_tts not available: {e}'})
    
    async def _get_voices():
        try:
            voices = await edge_tts.list_voices()
            sorted_voices = sorted(voices, key=lambda v: v['ShortName'])
            print(f"Fetched {len(sorted_voices)} voices from edge-tts")
            return sorted_voices
        except Exception as e:
            print(f"Error getting voices: {e}")
            return []

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        voices = loop.run_until_complete(_get_voices())
        loop.close()
        return jsonify({'voices': voices})
    except Exception as e:
        return jsonify({'voices': [], 'error': str(e)})

@app.route('/api/github/proxy', methods=['GET'])
def get_github_proxy_route():
    webui_config = load_json(WEBUI_CONFIG_PATH)
    base = webui_config.get('github_proxy_base', 'https://aki.ae-3803.com/')
    enabled = webui_config.get('github_proxy_enabled', True)
    return jsonify({
        'ok': True,
        'base': base,
        'enabled': enabled,
        'github_proxy_base': base,
        'github_proxy_enabled': enabled
    })

@app.route('/api/github/proxy', methods=['POST'])
def set_github_proxy_route():
    data = request.json
    webui_config = load_json(WEBUI_CONFIG_PATH)
    base = data.get('base', data.get('github_proxy_base', 'https://aki.ae-3803.com/'))
    enabled = data.get('enabled', data.get('github_proxy_enabled', True))
    webui_config['github_proxy_base'] = base
    webui_config['github_proxy_enabled'] = bool(enabled)
    save_json(WEBUI_CONFIG_PATH, webui_config)
    return jsonify({'ok': True})

@app.route('/api/presets', methods=['GET'])
def list_presets_route():
    try:
        presets = presets_tool.read_presets()
        return jsonify(presets)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/presets/<id>/content', methods=['GET'])
def get_preset_content_route(id):
    try:
        print(f"Fetching content for preset: {id}")
        presets = presets_tool.read_presets()
        
        # If ID is not in current.json, it's likely a new preset not yet saved.
        # Return empty content instead of 404 to allow UI to open the editor.
        if not presets or id not in presets:
            print(f"Preset {id} not in current.json, returning empty content for new preset.")
            return jsonify({'content': '', 'is_new': True})
        
        filename = presets[id].get('path', f'{id}.txt')
        preset_path = os.path.join(current_dir, 'prerequisites', filename)
        
        if not os.path.exists(preset_path):
            print(f"File not found: {preset_path}, returning empty content.")
            return jsonify({'content': '', 'warning': 'File missing'})
            
        with open(preset_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return jsonify({'content': content})
    except Exception as e:
        print(f"Error fetching preset {id}: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/presets/<id>', methods=['PUT'])
def save_preset_content_route(id):
    try:
        content = request.json.get('content', '')
        print(f"Saving content for preset: {id}")
        presets = presets_tool.read_presets()
        
        if not presets:
            presets = {}
            
        if id not in presets:
            # If not found, create a entry for it
            presets[id] = {
                "name": "新预设",
                "uid": [],
                "info": "",
                "path": f"{id}.txt"
            }
            presets_tool.write_presets(presets)
            
        filename = presets[id].get('path', f'{id}.txt')
        preset_dir = os.path.join(current_dir, 'prerequisites')
        os.makedirs(preset_dir, exist_ok=True)
        preset_path = os.path.join(preset_dir, filename)
        
        with open(preset_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return jsonify({'status': 'success', 'ok': True})
    except Exception as e:
        print(f"Error saving preset {id}: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/presets/<id>', methods=['DELETE'])
def delete_preset_route(id):
    try:
        if id == 'Normal':
            return jsonify({'ok': False, 'error': {'type': 'forbidden', 'message': '默认预设不可删除'}}), 403

        presets = {}
        try:
            presets = presets_tool.read_presets() or {}
        except Exception:
            presets = {}

        if id not in presets:
            return jsonify({'ok': True, 'deleted': False})

        filename = str((presets.get(id) or {}).get('path') or f'{id}.txt').strip()
        if not filename:
            filename = f'{id}.txt'

        base_dir = os.path.abspath(os.path.join(current_dir, 'prerequisites'))
        os.makedirs(base_dir, exist_ok=True)
        target_path = os.path.abspath(os.path.join(base_dir, filename))
        if not target_path.startswith(base_dir + os.sep):
            return jsonify({'ok': False, 'error': {'type': 'invalid_path', 'message': '预设文件路径非法'}}), 400

        presets.pop(id, None)
        presets_tool.write_presets(presets)

        deleted_file = False
        if os.path.exists(target_path):
            try:
                os.remove(target_path)
                deleted_file = True
            except Exception:
                deleted_file = False

        return jsonify({'ok': True, 'deleted': True, 'deleted_file': deleted_file})
    except Exception as e:
        return jsonify({'ok': False, 'error': {'type': 'unknown_error', 'message': str(e)}}), 500

@app.route('/api/plugins/local', methods=['GET'])
def plugins_local_route():
    try:
        # Temporarily patch PLUGIN_FOLDER for PluginsManager if needed
        # Or just reimplement listing since PluginsManager relies on global var
        plugins_dir = os.path.join(jianer_bot_path, 'plugins')
        if not os.path.exists(plugins_dir):
            return jsonify({'plugins': []})
            
        plugins = []
        for name in os.listdir(plugins_dir):
            if name == "__pycache__": continue
            path = os.path.join(plugins_dir, name)
            
            plugin_info = {
                'name': name,
                'enabled': not name.startswith('d_'),
                'description': 'Local Plugin', # Placeholder
                'version': '1.0.0', # Placeholder
                'author': 'Unknown' # Placeholder
            }
            
            # Try to read manifest or docstring if possible?
            # For now just basic info
            plugins.append(plugin_info)
            
        return jsonify({'plugins': plugins})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/github/token', methods=['GET'])
def get_github_token_route():
    webui_config = load_json(WEBUI_CONFIG_PATH)
    return jsonify({'token': webui_config.get('github_token', '')})

@app.route('/api/github/token', methods=['POST'])
def set_github_token_route():
    token = request.json.get('token', '')
    webui_config = load_json(WEBUI_CONFIG_PATH)
    webui_config['github_token'] = token
    save_json(WEBUI_CONFIG_PATH, webui_config)
    return jsonify({'ok': True})

# --- Plugin Management ---

def get_github_client():
    webui_config = load_json(WEBUI_CONFIG_PATH)
    token = os.getenv('SETUPWIZARD_GITHUB_TOKEN') or os.getenv('GITHUB_TOKEN') or webui_config.get('github_token')
    # Use a default token if none provided? Or let it be None (unauthenticated, lower rate limit)
    # The original code had a hardcoded token, maybe we should respect that if needed, 
    # but for now let's rely on user provided token or None.
    return get_github_instance(token if token else None)

def with_github_proxy(url: str) -> str:
    cfg = load_json(WEBUI_CONFIG_PATH)
    base = cfg.get('github_proxy_base', 'https://aki.ae-3803.com/')
    enabled = cfg.get('github_proxy_enabled', True)
    if enabled and base:
        # Ensure base ends with a single slash
        if not base.endswith('/'):
            base = base + '/'
        return f"{base}{url}"
    return url

def github_headers():
    cfg = load_json(WEBUI_CONFIG_PATH)
    token = os.getenv('SETUPWIZARD_GITHUB_TOKEN') or os.getenv('GITHUB_TOKEN') or cfg.get('github_token')
    headers = {'Accept': 'application/vnd.github.v3+json'}
    if token:
        headers['Authorization'] = f"token {token}"
    return headers

def smart_request(url, headers=None, timeout=15):
    """
    Tries to fetch the URL using the configured proxy.
    If that fails, falls back to direct connection.
    """
    proxied_url = with_github_proxy(url)
    
    last_error = None
    
    # If proxy is enabled and changes the URL
    if proxied_url != url:
        try:
            print(f"[SmartRequest] Trying proxy: {proxied_url}")
            r = requests.get(proxied_url, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            last_error = e
            print(f"[SmartRequest] Proxy failed ({e}), falling back to direct: {url}")
    
    # Direct connection (fallback or if proxy disabled)
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception as e:
        if last_error:
            # If both failed, prioritize showing the proxy error if applicable, 
            # or maybe combine them. But usually connection error to direct is what matters if proxy failed.
            # Actually, let's just return the direct error but maybe log more context.
            print(f"[SmartRequest] Direct connection failed: {e}")
        raise e

def format_network_error(e):
    """
    Returns a user-friendly error message for network exceptions.
    """
    msg = str(e)
    if isinstance(e, Timeout):
        return "连接超时，请检查网络或代理设置。"
    if isinstance(e, ConnectionError) or "NameResolutionError" in msg or "Connection refused" in msg or "Max retries exceeded" in msg:
        return "无法连接到 GitHub。请检查您的网络连接，或在设置中配置有效的 GitHub 代理。"
    if isinstance(e, HTTPError):
        return f"服务器返回错误: {e.response.status_code if e.response else '未知'}"
    return f"网络请求失败: {msg}"

def json_error(error_type: str, message: str, status_code: int = 500, context: dict = None, upstream_status: int = None):
    payload = {
        'ok': False,
        'error': {
            'type': error_type,
            'message': message
        }
    }
    if context:
        payload['context'] = context
    if upstream_status is not None:
        payload['error']['upstream_status'] = upstream_status
    return jsonify(payload), status_code

def _resolve_python_for_pip(preferred=None):
    candidates = []
    for item in [preferred, sys.executable, shutil.which('python3'), shutil.which('python')]:
        if item and item not in candidates:
            candidates.append(item)

    for py_exec in candidates:
        try:
            probe = subprocess.run(
                [py_exec, '-m', 'pip', '--version'],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False
            )
            if probe.returncode == 0:
                return py_exec
        except Exception:
            continue

    raise RuntimeError('No usable Python with pip found. Please configure a valid Python/venv.')

def pip_install_requirements(req_path, python_executable=None):
    if not os.path.exists(req_path):
        return True
    cfg = load_json(WEBUI_CONFIG_PATH, {})
    index_url = cfg.get('pypi_index_url') or cfg.get('index_url') or ''
    py_exec = _resolve_python_for_pip(python_executable)
    command = [
        py_exec, "-m", "pip", "install",
        "-r", req_path,
        "--disable-pip-version-check",
        "--no-warn-script-location"
    ]
    if index_url:
        command.extend(["-i", index_url])
        host = urlparse(index_url).hostname
        if host:
            command.extend(["--trusted-host", host])

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False
    )
    if result.returncode == 0:
        return True

    output = result.stdout or ''
    # Debian/Ubuntu may enforce externally-managed environments without this flag.
    if 'externally-managed-environment' in output:
        retry_cmd = command + ['--break-system-packages']
        retry = subprocess.run(
            retry_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False
        )
        if retry.returncode == 0:
            return True
        raise subprocess.CalledProcessError(retry.returncode, retry_cmd, output=retry.stdout)

    raise subprocess.CalledProcessError(result.returncode, command, output=output)
    return True

def download_folder_via_api(folder_path, local_root):
    api_url = f"https://api.github.com/repos/IntelliMarkets/Jianer_Plugins_Index/contents/{folder_path}"
    r = smart_request(api_url, headers=github_headers(), timeout=15)
    data = r.json()
    for item in data:
        item_type = item.get('type')
        item_path = item.get('path')
        if not item_path:
            continue
        if item_type == 'dir':
            os.makedirs(os.path.join(local_root, item_path), exist_ok=True)
            download_folder_via_api(item_path, local_root)
        elif item_type == 'file':
            file_url = item.get('download_url')
            if not file_url:
                continue
            fr = smart_request(file_url, timeout=30)
            local_path = os.path.join(local_root, item_path)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "wb") as f:
                f.write(fr.content)

@app.route('/api/plugins/remote', methods=['GET'])
def plugins_remote_route():
    cfg = load_json(WEBUI_CONFIG_PATH)
    proxy_base = cfg.get('github_proxy_base', 'https://aki.ae-3803.com/')
    proxy_enabled = bool(cfg.get('github_proxy_enabled', True))
    ctx = {
        'github_proxy_enabled': proxy_enabled,
        'github_proxy_base': proxy_base
    }
    try:
        url = "https://api.github.com/repos/IntelliMarkets/Jianer_Plugins_Index/contents/"
        r = smart_request(url, headers=github_headers(), timeout=15)
        try:
            data = r.json()
        except Exception:
            return json_error('upstream_invalid_json', '上游返回内容无法解析为 JSON', 502, context=ctx, upstream_status=getattr(r, 'status_code', None))
        if not isinstance(data, list):
            return json_error('upstream_invalid_shape', '上游返回数据结构不符合预期', 502, context=ctx, upstream_status=getattr(r, 'status_code', None))
        folders = [item['name'] for item in data if item.get('type') == 'dir']
        return jsonify({'ok': True, 'plugins': folders})
    except requests.exceptions.Timeout:
        return json_error('upstream_timeout', '连接上游超时', 504, context=ctx)
    except requests.exceptions.ConnectionError as e:
        return json_error('upstream_request_error', format_network_error(e), 502, context=ctx)
    except requests.exceptions.HTTPError as e:
        upstream_status = getattr(getattr(e, 'response', None), 'status_code', None)
        if upstream_status in (401, 403):
            return json_error('github_auth', 'GitHub 鉴权失败（Token 无效或额度受限）', upstream_status, context=ctx, upstream_status=upstream_status)
        return json_error('upstream_http_error', '上游请求失败', 502, context=ctx, upstream_status=upstream_status)
    except requests.exceptions.RequestException as e:
        return json_error('upstream_request_error', format_network_error(e), 502, context=ctx)
    except Exception as e:
        return json_error('unknown_error', str(e), 500, context=ctx)

@app.route('/api/plugins/infos', methods=['GET'])
def plugin_infos_route():
    names = request.args.get('names', '').split(',')
    names = [n.strip() for n in names if n and n.strip()]
    
    results = {}
    
    try:
        def fetch_intro(name):
            try:
                readme_url = f"https://api.github.com/repos/IntelliMarkets/Jianer_Plugins_Index/contents/{name}/README.md"
                rr = smart_request(readme_url, headers=github_headers(), timeout=15)
                intro = ""
                if rr.status_code == 200:
                    body = rr.json()
                    content = body.get('content', '')
                    if content:
                        intro = base64.b64decode(content).decode('utf-8', errors='ignore')
                return name, {'intro': intro}
            except Exception:
                # If individual fetch fails, just set empty
                return name, {'intro': ""}

        if not names:
            return jsonify(results)

        max_workers = min(10, max(1, len(names)))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(fetch_intro, name) for name in names]
            for future in as_completed(futures):
                name, intro_data = future.result()
                results[name] = intro_data
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/plugins/info/<name>', methods=['GET'])
def plugin_info_route(name):
    try:
        def fetch_file(path):
            u = f"https://api.github.com/repos/IntelliMarkets/Jianer_Plugins_Index/contents/{path}"
            try:
                rr = smart_request(u, headers=github_headers(), timeout=15)
                if rr.status_code == 200:
                    body = rr.json()
                    content = body.get('content', '')
                    if content:
                        return base64.b64decode(content).decode('utf-8', errors='ignore')
            except Exception:
                pass
            return ""
        intro = fetch_file(f"{name}/README.md")
        depend = fetch_file(f"{name}/requirements.txt")
        agreement = fetch_file(f"{name}/LICENSE")
        return jsonify({'intro': intro, 'depend': depend, 'agreement': agreement})
    except requests.exceptions.RequestException as e:
        return jsonify({'error': format_network_error(e)}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def install_plugin_task(name):
    global install_progress
    with install_progress_lock:
        install_progress[name] = {'status': 'starting', 'percent': 0, 'message': 'Starting...'}
    
    try:
        with install_progress_lock:
            install_progress[name]['status'] = 'downloading'
            install_progress[name]['message'] = 'Downloading files...'
            install_progress[name]['percent'] = 20
        
        plugins_dir = os.path.join(jianer_bot_path, 'plugins')
        local_path = os.path.join(plugins_dir, name)
        
        download_folder_via_api(name, plugins_dir)
        
        with install_progress_lock:
            install_progress[name]['percent'] = 80
            install_progress[name]['status'] = 'installing'
            install_progress[name]['message'] = 'Installing dependencies...'
        
        # Check for requirements.txt and install
        req_path = os.path.join(local_path, 'requirements.txt')
        if os.path.exists(req_path):
            pip_install_requirements(req_path)
            
        with install_progress_lock:
            install_progress[name]['percent'] = 100
            install_progress[name]['status'] = 'done'
            install_progress[name]['message'] = 'Installation complete.'
        
    except requests.exceptions.RequestException as e:
        with install_progress_lock:
            install_progress[name]['status'] = 'error'
            install_progress[name]['message'] = format_network_error(e)
    except Exception as e:
        with install_progress_lock:
            install_progress[name]['status'] = 'error'
            install_progress[name]['message'] = str(e)

@app.route('/api/plugins/install', methods=['POST'])
def plugin_install_route():
    name = request.json.get('name')
    if not name:
        return jsonify({'ok': False, 'error': 'No name provided'}), 400
        
    with install_progress_lock:
        if name in install_progress and install_progress[name].get('status') in ['starting', 'downloading', 'installing']:
            return jsonify({'ok': False, 'error': 'Already installing'}), 400
        
    thread = threading.Thread(target=install_plugin_task, args=(name,))
    thread.start()
    
    return jsonify({'ok': True})

@app.route('/api/plugins/progress/<name>', methods=['GET'])
def plugin_progress_route(name):
    with install_progress_lock:
        if name in install_progress:
            return jsonify(install_progress[name])
    return jsonify({'status': 'unknown', 'percent': 0, 'message': ''})

if sock is not None:
    @sock.route('/api/plugins/progress/ws/<name>')
    def plugin_progress_ws(ws, name):
        last_payload = None
        try:
            while True:
                with install_progress_lock:
                    payload_obj = install_progress.get(name) or {'status': 'unknown', 'percent': 0, 'message': ''}
                payload = json.dumps(payload_obj, ensure_ascii=False)
                if payload != last_payload:
                    ws.send(payload)
                    last_payload = payload
                status = payload_obj.get('status')
                if status in ('done', 'error'):
                    break
                time.sleep(0.25)
        except Exception:
            return

@app.route('/api/plugins/remove', methods=['POST'])
def plugin_remove_route():
    name = request.json.get('name')
    plugins_dir = os.path.join(jianer_bot_path, 'plugins')
    plugin_path = os.path.join(plugins_dir, name)
    
    import shutil
    try:
        if os.path.exists(plugin_path):
            if os.path.isdir(plugin_path):
                shutil.rmtree(plugin_path)
            else:
                os.remove(plugin_path)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/plugins/toggle', methods=['POST'])
def plugin_toggle_route():
    name = request.json.get('name')
    plugins_dir = os.path.join(jianer_bot_path, 'plugins')
    
    # Check if it's currently enabled (normal name) or disabled (d_ prefix)
    # This logic depends on how PluginsManager works. 
    # Based on PluginsManager.py: "if plugin_name.startswith("d_"): plugin_name = plugin_name[2:]"
    
    # We need to find the actual file/folder
    target = None
    is_disabled = False
    
    if os.path.exists(os.path.join(plugins_dir, name)):
        target = os.path.join(plugins_dir, name)
        is_disabled = False
    elif os.path.exists(os.path.join(plugins_dir, 'd_' + name)):
        target = os.path.join(plugins_dir, 'd_' + name)
        is_disabled = True
    elif os.path.exists(os.path.join(plugins_dir, name + '.py')):
        target = os.path.join(plugins_dir, name + '.py')
        is_disabled = False
    elif os.path.exists(os.path.join(plugins_dir, 'd_' + name + '.py')):
        target = os.path.join(plugins_dir, 'd_' + name + '.py')
        is_disabled = True
        
    if not target:
        return jsonify({'ok': False, 'error': 'Plugin not found'}), 404
        
    try:
        dirname = os.path.dirname(target)
        basename = os.path.basename(target)
        
        if is_disabled:
            # Enable it: remove d_
            new_name = basename[2:]
        else:
            # Disable it: add d_
            new_name = 'd_' + basename
            
        os.rename(target, os.path.join(dirname, new_name))
        return jsonify({'ok': True, 'state': 'enabled' if is_disabled else 'disabled'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/plugins/state/<name>', methods=['GET'])
def plugin_state_route(name):
    plugins_dir = os.path.join(jianer_bot_path, 'plugins')
    # Simple check
    enabled = True
    if os.path.exists(os.path.join(plugins_dir, 'd_' + name)) or \
       os.path.exists(os.path.join(plugins_dir, 'd_' + name + '.py')):
        enabled = False
    return jsonify({'enabled': enabled})

@app.route('/api/plugins/states', methods=['GET'])
def plugin_states_route():
    names = request.args.get('names', '').split(',')
    plugins_dir = os.path.join(jianer_bot_path, 'plugins')
    result = {}
    for name in names:
        if not name: continue
        enabled = True
        if os.path.exists(os.path.join(plugins_dir, 'd_' + name)) or \
           os.path.exists(os.path.join(plugins_dir, 'd_' + name + '.py')):
            enabled = False
        result[name] = enabled
    return jsonify(result)

@app.route('/api/plugins/check-updates', methods=['GET'])
def plugin_check_updates_route():
    # Placeholder for update check
    return jsonify({})

@app.route('/api/pip/config', methods=['GET'])
def get_pip_config_route():
    webui_config = load_json(WEBUI_CONFIG_PATH)
    index_url = webui_config.get('pypi_index_url', webui_config.get('index_url', ''))
    return jsonify({'ok': True, 'pypi_index_url': index_url})

@app.route('/api/pip/config', methods=['POST'])
def set_pip_config_route():
    data = request.json or {}
    index_url = data.get('pypi_index_url', data.get('index_url', ''))
    webui_config = load_json(WEBUI_CONFIG_PATH)
    webui_config['pypi_index_url'] = index_url
    save_json(WEBUI_CONFIG_PATH, webui_config)
    return jsonify({'ok': True})

# --- Jianer Management ---

jianer_process = None
jianer_logs = []
jianer_logs_lock = threading.Lock()
jianer_logs_cv = threading.Condition(jianer_logs_lock)

def _append_jianer_log_line(line):
    if line is None:
        return
    text = str(line)
    with jianer_logs_lock:
        jianer_logs.append(text)
        if len(jianer_logs) > 2000:
            del jianer_logs[:-2000]
        try:
            jianer_logs_cv.notify_all()
        except Exception:
            pass

def _get_jianer_logs_snapshot():
    with jianer_logs_lock:
        return list(jianer_logs)

def _jianer_log_reader(proc):
    try:
        for line in iter(proc.stdout.readline, ''):
            if not line:
                break
            _append_jianer_log_line(line)
    except Exception:
        pass

if sock is not None:
    @sock.route('/api/jianer/logs/ws')
    def jianer_logs_ws(ws):
        cursor = 0
        try:
            snapshot = _get_jianer_logs_snapshot()
            cursor = len(snapshot)
            ws.send(json.dumps({'type': 'snapshot', 'logs': snapshot}))
            while True:
                chunk = []
                with jianer_logs_cv:
                    jianer_logs_cv.wait_for(lambda: len(jianer_logs) > cursor, timeout=30)
                    total = len(jianer_logs)
                    if total > cursor:
                        chunk = jianer_logs[cursor:total]
                        cursor = total
                if chunk:
                    for line in chunk:
                        ws.send(json.dumps({'type': 'lines', 'logs': [line]}, ensure_ascii=False))
        except Exception:
            return

def _normalize_jianer_venv_path(venv_path):
    p = str(venv_path or '').strip()
    if not p:
        return ''
    return os.path.abspath(os.path.expanduser(p))

def _resolve_venv_python(venv_path):
    normalized = _normalize_jianer_venv_path(venv_path)
    if not normalized:
        return sys.executable

    if os.path.isfile(normalized):
        return normalized

    candidates = [
        os.path.join(normalized, 'Scripts', 'python.exe'),
        os.path.join(normalized, 'Scripts', 'python'),
        os.path.join(normalized, 'bin', 'python'),
        os.path.join(normalized, 'bin', 'python3'),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate

    raise FileNotFoundError('Invalid venv path: python executable not found')

def _resolve_venv_bin_dir(venv_path):
    normalized = _normalize_jianer_venv_path(venv_path)
    if not normalized:
        return ''
    if os.path.isfile(normalized):
        return os.path.dirname(normalized)

    candidates = [
        os.path.join(normalized, 'Scripts'),
        os.path.join(normalized, 'bin'),
    ]
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return ''

def _detect_bash_flavor(bash_path):
    if _detect_platform() != 'windows':
        return 'posix'
    p = str(bash_path or '').lower().replace('/', '\\')
    if '\\windows\\system32\\bash.exe' in p:
        return 'wsl'
    return 'msys'

def _path_for_bash(path_value, bash_flavor='msys'):
    p = str(path_value or '').strip()
    if not p:
        return ''
    if _detect_platform() != 'windows':
        return p
    # Convert "C:\a\b\python.exe" for different bash implementations.
    p = p.replace('\\', '/')
    m = re.match(r'^([A-Za-z]):/(.*)$', p)
    if m:
        drive = m.group(1).lower()
        tail = m.group(2)
        if bash_flavor == 'wsl':
            return f"/mnt/{drive}/{tail}"
        return f"/{drive}/{tail}"
    return p

def _rewrite_python_cmd_for_bash(cmd_str, python_exec, bash_flavor='msys'):
    text = str(cmd_str or '').strip()
    if not text:
        return text
    if not python_exec:
        return text
    if not re.match(r'^\s*(python|python3|py)\b', text):
        return text
    py_for_bash = shlex.quote(_path_for_bash(python_exec, bash_flavor=bash_flavor))
    def repl(_m):
        rest = text[len(_m.group(0)):]
        if re.match(r'^\s+-u(\s|$)', rest):
            return py_for_bash
        return f"{py_for_bash} -u"
    return re.sub(r'^\s*(python|python3|py)\b', repl, text, count=1)

def _rewrite_python_cmd_native(cmd_str, python_exec):
    text = str(cmd_str or '').strip()
    if not text:
        return text
    if not python_exec:
        return text
    if not re.match(r'^\s*(python|python3|py)\b', text):
        return text
    py_exec_quoted = f'"{python_exec}"'
    def repl(_m):
        rest = text[len(_m.group(0)):]
        if re.match(r'^\s+-u(\s|$)', rest):
            return py_exec_quoted
        return f"{py_exec_quoted} -u"
    return re.sub(r'^\s*(python|python3|py)\b', repl, text, count=1)

def _detect_shell_mode(shell_mode):
    raw = str(shell_mode or '').strip().lower()
    if raw in ('bash', 'cmd', 'powershell'):
        return raw
    if _detect_platform() == 'windows':
        return 'cmd'
    return 'bash'

def _ensure_jianer_workdir_defaults(cwd):
    created = []
    config_path = os.path.join(cwd, 'config.json')
    if not os.path.exists(config_path):
        if not save_json(config_path, DEFAULT_CONFIG):
            raise OSError(f'Failed to create config.json in {cwd}')
        created.append(config_path)

    appsettings_path = os.path.join(cwd, 'appsettings.json')
    if not os.path.exists(appsettings_path):
        if not save_json(appsettings_path, DEFAULT_APPSETTINGS):
            raise OSError(f'Failed to create appsettings.json in {cwd}')
        created.append(appsettings_path)

    return created

@app.route('/api/jianer/config', methods=['GET'])
def jianer_config_get_route():
    cfg = load_json(WEBUI_CONFIG_PATH, {})
    return jsonify({
        'command': cfg.get('jianer_command', 'python main.py'),
        'cwd': cfg.get('jianer_cwd', os.getcwd()),
        'venv': cfg.get('jianer_venv', ''),
        'shell': cfg.get('jianer_shell', 'auto')
    })

@app.route('/api/jianer/config', methods=['POST'])
def jianer_config_set_route():
    data = request.json or {}
    cfg = load_json(WEBUI_CONFIG_PATH, {})
    cfg['jianer_command'] = str(data.get('command', 'python main.py') or 'python main.py').strip() or 'python main.py'
    cfg['jianer_cwd'] = str(data.get('cwd', os.getcwd()) or os.getcwd()).strip() or os.getcwd()
    cfg['jianer_venv'] = str(data.get('venv', '') or '').strip()
    cfg['jianer_shell'] = str(data.get('shell', 'auto') or 'auto').strip().lower()
    save_json(WEBUI_CONFIG_PATH, cfg)
    return jsonify({'ok': True})

@app.route('/api/jianer/status', methods=['GET'])
def jianer_status_route():
    global jianer_process
    running = bool(jianer_process and jianer_process.poll() is None)
    return jsonify({'running': running})

@app.route('/api/jianer/start', methods=['POST'])
def jianer_start_route():
    global jianer_process
    
    if jianer_process and jianer_process.poll() is None:
        return jsonify({'ok': False, 'error': 'Already running'})
        
    cfg = load_json(WEBUI_CONFIG_PATH, {})
    cmd_str = str(cfg.get('jianer_command', 'python main.py') or 'python main.py').strip() or 'python main.py'
    cwd = str(cfg.get('jianer_cwd', os.getcwd()) or os.getcwd()).strip() or os.getcwd()
    venv_path = str(cfg.get('jianer_venv', '') or '').strip()
    shell_mode = _detect_shell_mode(cfg.get('jianer_shell', 'auto'))
    
    if not os.path.exists(cwd):
        return jsonify({'ok': False, 'error': 'Working directory not found'})
    try:
        _ensure_jianer_workdir_defaults(cwd)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Failed to initialize config files: {e}'})

    try:
        plat = _detect_platform()
        popen_kwargs = {}
        py_exec = _resolve_venv_python(venv_path)
        venv_bin_dir = _resolve_venv_bin_dir(venv_path)
        env = os.environ.copy()
        if venv_bin_dir:
            env['PATH'] = venv_bin_dir + os.pathsep + env.get('PATH', '')
        if venv_path:
            env['VIRTUAL_ENV'] = _normalize_jianer_venv_path(venv_path)
        env['JIANER_PYTHON'] = py_exec
        env['PYTHONUNBUFFERED'] = '1'

        if plat == 'windows':
            try:
                popen_kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
            except Exception:
                pass
        else:
            popen_kwargs['start_new_session'] = True

        if shell_mode == 'bash':
            bash_path = shutil.which('bash')
            if not bash_path:
                return jsonify({'ok': False, 'error': 'bash not found in PATH'})
            bash_flavor = _detect_bash_flavor(bash_path)
            bash_cmd = _rewrite_python_cmd_for_bash(cmd_str, py_exec, bash_flavor=bash_flavor)
            bash_cwd = _path_for_bash(cwd, bash_flavor=bash_flavor)
            wrapped_cmd = f'cd {shlex.quote(bash_cwd)} && {bash_cmd}' if bash_cwd else bash_cmd
            proc = subprocess.Popen(
                [bash_path, '-lc', wrapped_cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                env=env,
                text=True,
                encoding='utf-8',
                errors='replace',
                **popen_kwargs
            )
        elif shell_mode == 'powershell':
            ps_cmd = _rewrite_python_cmd_native(cmd_str, py_exec)
            ps_path = shutil.which('pwsh') or shutil.which('powershell')
            if not ps_path:
                return jsonify({'ok': False, 'error': 'powershell not found in PATH'})
            proc = subprocess.Popen(
                [ps_path, '-NoProfile', '-Command', ps_cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                env=env,
                text=True,
                encoding='utf-8',
                errors='replace',
                **popen_kwargs
            )
        else:
            # Windows default: cmd. Non-Windows default won't enter here.
            native_cmd = _rewrite_python_cmd_native(cmd_str, py_exec)
            if plat == 'windows':
                proc = subprocess.Popen(
                    native_cmd,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=cwd,
                    env=env,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    **popen_kwargs
                )
            else:
                # Safety fallback for non-Windows.
                bash_path = shutil.which('bash') or '/bin/bash'
                proc = subprocess.Popen(
                    [bash_path, '-lc', cmd_str],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=cwd,
                    env=env,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    **popen_kwargs
                )
            
        # If process exits immediately, return detailed error to UI.
        time.sleep(0.7)
        exit_code = proc.poll()
        if exit_code is not None:
            detail = ''
            try:
                out, _ = proc.communicate(timeout=0.3)
                detail = (out or '').strip()
            except subprocess.TimeoutExpired:
                detail = ''
            except Exception:
                detail = ''
            if detail:
                with jianer_logs_lock:
                    jianer_logs.clear()
                    jianer_logs.append(detail + '\n')
            jianer_process = None
            return jsonify({
                'ok': False,
                'error': f'Process exited immediately (code {exit_code})',
                'detail': detail[-2000:] if detail else ''
            })

        jianer_process = proc
        with jianer_logs_lock:
            jianer_logs.clear()
        t = threading.Thread(target=_jianer_log_reader, args=(proc,), daemon=True)
        t.start()
        
        return jsonify({'ok': True, 'python': py_exec, 'shell': shell_mode})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

@app.route('/api/jianer/install-requirements', methods=['POST'])
def jianer_install_requirements_route():
    cfg = load_json(WEBUI_CONFIG_PATH, {})
    cwd = str(cfg.get('jianer_cwd', os.getcwd()) or os.getcwd()).strip() or os.getcwd()
    venv_path = str(cfg.get('jianer_venv', '') or '').strip()

    if not os.path.exists(cwd):
        return jsonify({'ok': False, 'error': 'Working directory not found'}), 400
    try:
        _ensure_jianer_workdir_defaults(cwd)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Failed to initialize config files: {e}'}), 500

    req_path = os.path.join(cwd, 'requirements.txt')
    if not os.path.isfile(req_path):
        return jsonify({'ok': False, 'error': 'requirements.txt not found in working directory'}), 404

    try:
        py_exec = _resolve_venv_python(venv_path)
    except FileNotFoundError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

    try:
        pip_install_requirements(req_path, python_executable=py_exec)
        return jsonify({'ok': True, 'python': py_exec, 'requirements': req_path})
    except subprocess.CalledProcessError as e:
        detail = str((getattr(e, 'output', '') or '')).strip()
        payload = {'ok': False, 'error': f'pip install failed: {e}'}
        if detail:
            payload['detail'] = detail[-4000:]
        return jsonify(payload), 500
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/jianer/stop', methods=['POST'])
def jianer_stop_route():
    global jianer_process
    if not jianer_process:
        return jsonify({'ok': False, 'error': 'Not running'})
    if jianer_process.poll() is not None:
        jianer_process = None
        return jsonify({'ok': False, 'error': 'Not running'})

    ok = _terminate_process(jianer_process)
    if ok:
        jianer_process = None
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'Failed to stop'})

@app.route('/api/jianer/logs', methods=['GET'])
def jianer_logs_route():
    return jsonify({'logs': _get_jianer_logs_snapshot()})

def instance_progress_route(id):
    return jsonify({'percent': 100, 'status': 'done', 'message': 'Ready'})

@app.route('/api/instances/jianer-config/<id>', methods=['GET'])
def instance_jianer_config_get(id):
    # This should probably read the config of that specific instance
    # For now, return the global config as a fallback or mock
    config = load_json(CONFIG_PATH, DEFAULT_CONFIG)
    return jsonify(config)

def _cmd_exists(name: str) -> bool:
    return bool(shutil.which(name))

def _detect_platform():
    if os.environ.get('TERMUX_VERSION') or 'com.termux' in (os.environ.get('PREFIX') or ''):
        return 'termux'
    sysname = platform.system().lower()
    if 'windows' in sysname:
        return 'windows'
    if 'darwin' in sysname:
        return 'macos'
    return 'linux'

def _is_admin():
    plat = _detect_platform()
    if plat == 'windows':
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    try:
        return os.geteuid() == 0
    except Exception:
        return False

def _has_sudo():
    return _cmd_exists('sudo')

def _job_log_append(job_id: str, line: str):
    job = napcat_progress.get(job_id)
    if not job:
        return
    logs = job.get('logs')
    if not isinstance(logs, list):
        logs = []
        job['logs'] = logs
    logs.append(line)
    if len(logs) > 2000:
        del logs[:len(logs) - 2000]

def _job_set(job_id: str, **kwargs):
    job = napcat_progress.get(job_id)
    if not job:
        return
    job.update(kwargs)

NAPCAT_ONEKEY_ZIP_URL = 'https://github.com/NapNeko/NapCatQQ/releases/latest/download/NapCat.Shell.Windows.OneKey.zip'
NAPCAT_ONEKEY_DIRNAME = 'NapCatQQ'

def _napcat_recommended_bases():
    return [
        'https://nclatest.znin.net/NapNeko/NapCat-Installer/main/script/',
        'https://raw.githubusercontent.com/NapNeko/NapCat-Installer/main/script/'
    ]

def _normalize_napcat_base(base: str):
    base = str(base or '').strip()
    if not base:
        raise ValueError('Empty base')
    p = urlparse(base)
    if p.scheme != 'https':
        raise ValueError('Only https is allowed')
    if p.query or p.fragment:
        raise ValueError('Query/fragment not allowed')
    if not base.endswith('/'):
        base = base + '/'
    ok_suffixes = [
        '/NapNeko/NapCat-Installer/main/script/',
        '/NapNeko/NapCat-Installer/refs/heads/main/script/',
        '/NapNeko/NapCat-Installer/refs/heads/master/script/'
    ]
    if not any(base.endswith(s) for s in ok_suffixes):
        raise ValueError('Base must end with NapCat-Installer script path')
    return base

def _napcat_allowed_bases():
    cfg = load_json(WEBUI_CONFIG_PATH, {})
    bases = []
    for b in (cfg.get('napcat_installer_bases') or []):
        try:
            bases.append(_normalize_napcat_base(b))
        except Exception:
            continue
    for b in _napcat_recommended_bases():
        try:
            bases.append(_normalize_napcat_base(b))
        except Exception:
            continue
    uniq = []
    seen = set()
    for b in bases:
        if b not in seen:
            uniq.append(b)
            seen.add(b)
    return uniq

def _napcat_installer_base():
    cfg = load_json(WEBUI_CONFIG_PATH, {})
    base = cfg.get('napcat_installer_base') or ''
    try:
        base = _normalize_napcat_base(base) if base else ''
    except Exception:
        base = ''
    allowed = _napcat_allowed_bases()
    if base and base in allowed:
        return base
    return allowed[0] if allowed else _normalize_napcat_base(_napcat_recommended_bases()[0])

def _assert_whitelisted_url(u: str):
    u = str(u or '').strip()
    if not u:
        raise ValueError('Empty url')
    p = urlparse(u)
    if p.scheme != 'https':
        raise ValueError('Only https is allowed')
    if u == NAPCAT_ONEKEY_ZIP_URL:
        return
    allowed_bases = _napcat_allowed_bases()
    if not any(u.startswith(b) for b in allowed_bases):
        raise ValueError('Url not allowed')

def _download_to(url: str, dest: str):
    _assert_whitelisted_url(url)
    r = requests.get(url, stream=True, timeout=30)
    r.raise_for_status()
    with open(dest, 'wb') as f:
        for chunk in r.iter_content(chunk_size=1024 * 64):
            if chunk:
                f.write(chunk)

def _download_to_atomic(url: str, dest: str):
    tmp = dest + '.tmp'
    _download_to(url, tmp)
    os.replace(tmp, dest)

def _extract_zip_safe(zip_path: str, dest_dir: str):
    dest_dir = os.path.abspath(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path, 'r') as zf:
        for info in zf.infolist():
            name = info.filename
            if not name or name.endswith('/'):
                continue
            out_path = os.path.abspath(os.path.join(dest_dir, name))
            if not (out_path == dest_dir or out_path.startswith(dest_dir + os.sep)):
                raise ValueError('Unsafe zip entry')
        zf.extractall(dest_dir)

def _as_bool(v, default=False):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ['1', 'true', 'yes', 'y', 'on']:
        return True
    if s in ['0', 'false', 'no', 'n', 'off']:
        return False
    return default

def _validate_payload(payload: dict, platform_name: str):
    action = (payload.get('action') or 'auto').strip().lower()
    if action not in ['auto', 'script']:
        raise ValueError('Invalid action')

    docker = _as_bool(payload.get('docker'), platform_name == 'docker')
    cli = _as_bool(payload.get('cli'), False)
    force = _as_bool(payload.get('force'), False)
    confirm = _as_bool(payload.get('confirm'), True)

    qq = str(payload.get('qq') or '').strip()
    if docker and not qq.isdigit():
        raise ValueError('QQ is required for docker mode')

    mode = str(payload.get('mode') or 'ws').strip().lower()
    allowed_modes = ['ws', 'reverse_ws', 'reverse_http']
    if docker and mode not in allowed_modes:
        raise ValueError('Invalid mode')

    proxy = None
    if 'proxy' in payload and payload.get('proxy') is not None and str(payload.get('proxy')).strip() != '':
        try:
            proxy = int(payload.get('proxy'))
        except Exception:
            raise ValueError('Invalid proxy')
        if docker:
            if proxy < 0 or proxy > 7:
                raise ValueError('Invalid proxy')
        else:
            if proxy < 0 or proxy > 5:
                raise ValueError('Invalid proxy')

    use_sudo = _as_bool(payload.get('use_sudo'), True)
    if platform_name in ['windows', 'termux']:
        use_sudo = False

    install_path = str(payload.get('install_path') or '').strip()

    return {
        'action': action,
        'docker': docker,
        'cli': cli,
        'force': force,
        'confirm': confirm,
        'qq': qq,
        'mode': mode,
        'proxy': proxy,
        'use_sudo': use_sudo,
        'install_path': install_path
    }

def _build_script_text(platform_name: str, params: dict):
    base = _napcat_installer_base()
    install_sh = urljoin(base, 'install.sh')
    install_ps1 = urljoin(base, 'install.ps1')
    install_termux = urljoin(base, 'install.termux.sh')
    
    install_dir_cmd = ''
    if params.get('install_path'):
        # Escape path for shell
        path = params['install_path']
        if platform_name == 'windows':
            install_dir_cmd = f'mkdir "{path}" -Force; cd "{path}"; '
        else:
            install_dir_cmd = f'mkdir -p "{path}" && cd "{path}" && '

    if platform_name == 'windows':
        lines = []
        lines.append('powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference=\'Stop\'; $dir=Join-Path (Get-Location) \'' + NAPCAT_ONEKEY_DIRNAME + '\'; New-Item -ItemType Directory -Force -Path $dir | Out-Null; $zip=Join-Path $dir \'NapCat.Shell.Windows.OneKey.zip\'; Invoke-WebRequest -Uri \'' + NAPCAT_ONEKEY_ZIP_URL + '\' -OutFile $zip; Expand-Archive -Path $zip -DestinationPath $dir -Force; $exe=Join-Path $dir \'NapCatInstaller.exe\'; if (!(Test-Path $exe)) { throw \'NapCatInstaller.exe not found\' }; Start-Process -FilePath $exe -Verb RunAs -Wait"')
        return '\n'.join(lines)

    if platform_name == 'termux':
        return f'{install_dir_cmd}curl -o napcat.termux.sh {install_termux} && bash napcat.termux.sh'

    args = []
    args.extend(['--docker', 'y' if params['docker'] else 'n'])
    if params['docker']:
        args.extend(['--qq', f"\"{params['qq']}\""])
        args.extend(['--mode', params['mode']])
        if params.get('proxy') is not None and params['proxy'] > 0:
            args.extend(['--proxy', str(params['proxy'])])
        if params['confirm']:
            args.append('--confirm')
    else:
        args.extend(['--cli', 'y' if params['cli'] else 'n'])
        if params.get('proxy') is not None and params['proxy'] > 0:
            args.extend(['--proxy', str(params['proxy'])])
        if params['force']:
            args.append('--force')

    prefix = 'sudo ' if params.get('use_sudo') else ''
    return f'{install_dir_cmd}curl -o napcat.sh {install_sh} && {prefix}bash napcat.sh ' + ' '.join(args)

def _build_exec(platform_name: str, params: dict, workdir: str):
    base = _napcat_installer_base()
    if platform_name == 'windows':
        script_url = urljoin(base, 'install.ps1')
        script_path = os.path.join(workdir, 'install.ps1')
        _download_to(script_url, script_path)
        ps = shutil.which('pwsh') or shutil.which('powershell') or 'powershell'
        return [ps, '-ExecutionPolicy', 'ByPass', '-File', script_path, '-verb', 'runas']

    if platform_name == 'termux':
        script_url = urljoin(base, 'install.termux.sh')
        script_path = os.path.join(workdir, 'install.termux.sh')
        _download_to(script_url, script_path)
        return ['bash', script_path]

    script_url = urljoin(base, 'install.sh')
    script_path = os.path.join(workdir, 'install.sh')
    _download_to(script_url, script_path)
    os.chmod(script_path, 0o755)

    args = []
    args.extend(['--docker', 'y' if params['docker'] else 'n'])
    if params['docker']:
        args.extend(['--qq', params['qq']])
        args.extend(['--mode', params['mode']])
        if params.get('proxy') is not None and params['proxy'] > 0:
            args.extend(['--proxy', str(params['proxy'])])
        if params['confirm']:
            args.append('--confirm')
    else:
        args.extend(['--cli', 'y' if params['cli'] else 'n'])
        if params.get('proxy') is not None and params['proxy'] > 0:
            args.extend(['--proxy', str(params['proxy'])])
        if params['force']:
            args.append('--force')

    if params.get('use_sudo') and not _is_admin() and _has_sudo():
        return ['sudo', 'bash', script_path, *args]
    return ['bash', script_path, *args]

def _run_job(job_id: str, platform_name: str, params: dict):
    _job_set(job_id, status='preparing', percent=5, message='Preparing...')
    try:
        if platform_name == 'windows':
            _run_job_windows_onekey(job_id, params)
            return

        install_path = params.get('install_path')
        temp_dir = None
        workdir = ''

        if install_path:
            try:
                os.makedirs(install_path, exist_ok=True)
                workdir = install_path
            except Exception as e:
                _job_set(job_id, status='error', percent=100, message=f'Failed to create directory: {e}')
                return
        else:
            temp_dir = tempfile.TemporaryDirectory(prefix='napcat-')
            workdir = temp_dir.name

        try:
            _job_set(job_id, status='downloading', percent=15, message='Downloading installer...')
            cmd = _build_exec(platform_name, params, workdir)
            _job_set(job_id, status='running', percent=30, message='Running installer...')

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=workdir,
                text=True,
                encoding='utf-8',
                errors='replace'
            )
            napcat_processes[job_id] = proc

            for line in proc.stdout:
                _job_log_append(job_id, line.rstrip('\n'))

            rc = proc.wait()
            napcat_processes.pop(job_id, None)
            if rc == 0:
                _job_set(job_id, status='done', percent=100, message='Done')
                # Save install path to config if successful
                if install_path:
                    cfg = load_json(WEBUI_CONFIG_PATH, {})
                    cfg['napcat_install_path'] = install_path
                    save_json(WEBUI_CONFIG_PATH, cfg)
            else:
                _job_set(job_id, status='error', percent=100, message=f'Failed with code {rc}')
        finally:
            if temp_dir:
                temp_dir.cleanup()

    except Exception as e:
        napcat_processes.pop(job_id, None)
        _job_set(job_id, status='error', percent=100, message=str(e))

def _job_is_canceled(job_id: str) -> bool:
    job = napcat_progress.get(job_id) or {}
    return job.get('status') == 'canceled'

def _find_first_file(root_dir: str, filename: str):
    for root, dirs, files in os.walk(root_dir):
        if filename in files:
            return os.path.join(root, filename)
    return None

def _run_job_windows_onekey(job_id: str, params: dict):
    base_dir = os.path.abspath(os.path.join(os.getcwd(), NAPCAT_ONEKEY_DIRNAME))
    ts = time.strftime('%Y%m%d%H%M%S')
    if os.path.exists(base_dir) and os.listdir(base_dir):
        try:
            os.replace(base_dir, base_dir + '.bak-' + ts)
        except Exception:
            shutil.rmtree(base_dir, ignore_errors=True)
    os.makedirs(base_dir, exist_ok=True)

    zip_path = os.path.join(base_dir, 'NapCat.Shell.Windows.OneKey.zip')
    _job_set(job_id, status='downloading', percent=15, message='Downloading OneKey zip...')
    _job_log_append(job_id, f'[download] {NAPCAT_ONEKEY_ZIP_URL}')
    _job_log_append(job_id, f'[download] -> {zip_path}')
    _download_to_atomic(NAPCAT_ONEKEY_ZIP_URL, zip_path)
    if _job_is_canceled(job_id):
        return

    _job_set(job_id, status='extracting', percent=30, message='Extracting OneKey zip...')
    _extract_zip_safe(zip_path, base_dir)
    if _job_is_canceled(job_id):
        return

    exe_path = _find_first_file(base_dir, 'NapCatInstaller.exe')
    if not exe_path or not os.path.exists(exe_path):
        raise ValueError('NapCatInstaller.exe not found after extract')

    _job_set(job_id, status='running', percent=55, message='Running NapCatInstaller.exe...')
    ps = shutil.which('pwsh') or shutil.which('powershell') or 'powershell'
    cmd = [ps, '-NoProfile', '-ExecutionPolicy', 'ByPass', '-Command', f'Start-Process -FilePath "{exe_path}" -Verb RunAs -Wait']
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=os.path.dirname(exe_path),
        text=True,
        encoding='utf-8',
        errors='replace'
    )
    napcat_processes[job_id] = proc
    for line in proc.stdout:
        _job_log_append(job_id, line.rstrip('\n'))
    rc = proc.wait()
    napcat_processes.pop(job_id, None)
    if rc != 0:
        raise ValueError(f'NapCatInstaller.exe exited with code {rc}')

    if _job_is_canceled(job_id):
        return

    install_dir = None
    for c in ['napcat.bat', 'launcher.bat', 'launcher-win10.bat', 'NapCatWinBootMain.exe']:
        p = _find_first_file(base_dir, c)
        if p:
            install_dir = os.path.dirname(p)
            break
    if not install_dir:
        install_dir = base_dir
    _job_log_append(job_id, f'[install_path] {install_dir}')

    cfg = load_json(WEBUI_CONFIG_PATH, {})
    cfg['napcat_install_path'] = install_dir
    save_json(WEBUI_CONFIG_PATH, cfg)
    _job_set(job_id, status='done', percent=100, message='Done')

@app.route('/api/system/info', methods=['GET'])
def system_info_route():
    plat = _detect_platform()
    return jsonify({
        'platform': plat,
        'system': platform.system(),
        'release': platform.release(),
        'machine': platform.machine(),
        'python': sys.version.split(' ')[0],
        'is_admin': _is_admin(),
        'has_sudo': _has_sudo(),
        'commands': {
            'curl': _cmd_exists('curl'),
            'wget': _cmd_exists('wget'),
            'bash': _cmd_exists('bash'),
            'docker': _cmd_exists('docker'),
            'powershell': _cmd_exists('powershell') or _cmd_exists('pwsh')
        }
    })

@app.route('/api/napcat/config', methods=['GET'])
def napcat_config_get_route():
    cfg = load_json(WEBUI_CONFIG_PATH, {})
    custom = []
    for b in (cfg.get('napcat_installer_bases') or []):
        try:
            custom.append(_normalize_napcat_base(b))
        except Exception:
            continue
    recommended = [_normalize_napcat_base(b) for b in _napcat_recommended_bases()]
    return jsonify({
        'ok': True,
        'installer_base': _napcat_installer_base(),
        'bases': _napcat_allowed_bases(),
        'custom_bases': custom,
        'recommended_bases': recommended
    })

@app.route('/api/napcat/config', methods=['POST'])
def napcat_config_set_route():
    data = request.json or {}
    cfg = load_json(WEBUI_CONFIG_PATH, {})
    raw_bases = cfg.get('napcat_installer_bases') or []
    if not isinstance(raw_bases, list):
        raw_bases = []
    bases = []
    for b in raw_bases:
        try:
            bases.append(_normalize_napcat_base(b))
        except Exception:
            continue
    recommended = [_normalize_napcat_base(b) for b in _napcat_recommended_bases()]

    remove_base = str(data.get('remove_base') or '').strip()
    if remove_base:
        try:
            remove_base = _normalize_napcat_base(remove_base)
        except Exception:
            remove_base = ''
    if remove_base:
        bases = [b for b in bases if str(b).strip() != remove_base]
        if str(cfg.get('napcat_installer_base') or '').strip() == remove_base:
            cfg.pop('napcat_installer_base', None)

    base = str(data.get('installer_base') or '').strip()
    if base:
        try:
            base = _normalize_napcat_base(base)
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)}), 400
        if base not in bases and base not in recommended:
            bases.append(base)
        cfg['napcat_installer_base'] = base
    elif 'installer_base' in data:
        cfg.pop('napcat_installer_base', None)

    cfg['napcat_installer_bases'] = bases
    save_json(WEBUI_CONFIG_PATH, cfg)
    return jsonify({'ok': True, 'installer_base': _napcat_installer_base(), 'bases': _napcat_allowed_bases()})

@app.route('/api/system/open-dialog', methods=['POST'])
def system_open_dialog_route():
    try:
        import tkinter as tk
        from tkinter import filedialog
        
        # Create a hidden root window
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True) # Bring to front
        
        # Try to set icon
        try:
            icon_path = os.path.join(current_dir, 'public', 'logo.png')
            if os.path.exists(icon_path):
                img = tk.PhotoImage(file=icon_path)
                root.iconphoto(False, img)
        except Exception:
            pass
        
        # Open directory dialog
        path = filedialog.askdirectory(title="选择 NapCat 安装目录")
        
        root.destroy()
        
        if path:
            # Convert to standard path format
            path = os.path.normpath(path)
            return jsonify({'ok': True, 'path': path})
        else:
            return jsonify({'ok': False, 'error': 'Canceled'})
            
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

@app.route('/api/napcat/deploy', methods=['POST'])
def napcat_deploy_route():
    payload = request.json or {}
    platform_name = (payload.get('platform') or 'auto').strip().lower()
    detected = _detect_platform()
    if platform_name == 'auto':
        platform_name = detected

    if platform_name not in ['windows', 'linux', 'macos', 'docker', 'termux']:
        return jsonify({'ok': False, 'error': 'Invalid platform'}), 400

    if platform_name == 'docker':
        payload = dict(payload)
        payload['docker'] = True

    try:
        params = _validate_payload(payload, platform_name)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

    script = _build_script_text(platform_name, params)
    if params['action'] == 'script':
        return jsonify({'ok': True, 'platform': platform_name, 'script': script})

    if platform_name in ['linux', 'macos', 'docker'] and params.get('use_sudo') and not _is_admin() and not _has_sudo():
        return jsonify({'ok': True, 'platform': platform_name, 'script': script, 'downgraded': True, 'message': 'sudo not available'})

    job_id = uuid.uuid4().hex
    napcat_progress[job_id] = {
        'job_id': job_id,
        'platform': platform_name,
        'status': 'queued',
        'percent': 0,
        'message': 'Queued',
        'script': script,
        'logs': [],
        'created_at': int(time.time())
    }

    import threading
    t = threading.Thread(target=_run_job, args=(job_id, platform_name, params), daemon=True)
    t.start()
    return jsonify({'ok': True, 'job_id': job_id, 'platform': platform_name, 'script': script})

@app.route('/api/napcat/progress/<job_id>', methods=['GET'])
def napcat_progress_route(job_id):
    job = napcat_progress.get(job_id)
    if not job:
        return jsonify({'ok': False, 'error': 'Not found'}), 404
    return jsonify(job)

@app.route('/api/napcat/cancel', methods=['POST'])
def napcat_cancel_route():
    job_id = (request.json or {}).get('job_id') or ''
    job_id = str(job_id)
    if not job_id:
        return jsonify({'ok': False, 'error': 'No job_id'}), 400
    proc = napcat_processes.get(job_id)
    if proc and proc.poll() is None:
        try:
            _terminate_process(proc)
        except Exception:
            pass
        napcat_processes.pop(job_id, None)
        _job_set(job_id, status='canceled', percent=100, message='Canceled')
        return jsonify({'ok': True})
    if job_id in napcat_progress and napcat_progress[job_id].get('status') in ['queued', 'preparing', 'downloading', 'extracting', 'running']:
        _job_set(job_id, status='canceled', percent=100, message='Canceled')
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'Not running'}), 400

# --- NapCat Management ---
napcat_running_process = None
napcat_logs_buffer = []

def _napcat_log_reader(proc):
    global napcat_logs_buffer
    try:
        for line in iter(proc.stdout.readline, ''):
            if not line: break
            napcat_logs_buffer.append(line)
            if len(napcat_logs_buffer) > 2000:
                napcat_logs_buffer.pop(0)
    except Exception:
        pass

def _terminate_process(proc):
    if not proc:
        return True
    if proc.poll() is not None:
        return True

    plat = _detect_platform()
    if plat == 'windows':
        try:
            subprocess.run(
                ['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False
            )
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=3)
        except Exception:
            pass
        return proc.poll() is not None

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=3)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=3)
        except Exception:
            pass
    return proc.poll() is not None

@app.route('/api/napcat/status', methods=['GET'])
def napcat_status_route():
    global napcat_running_process
    running = False
    if napcat_running_process and napcat_running_process.poll() is None:
        running = True
    
    cfg = load_json(WEBUI_CONFIG_PATH, {})
    install_path = cfg.get('napcat_install_path', '')
    
    return jsonify({'running': running, 'install_path': install_path})

@app.route('/api/napcat/start', methods=['POST'])
def napcat_start_route():
    global napcat_running_process, napcat_logs_buffer
    if napcat_running_process and napcat_running_process.poll() is None:
        return jsonify({'ok': False, 'error': 'Already running'})
    
    cfg = load_json(WEBUI_CONFIG_PATH, {})
    install_path = cfg.get('napcat_install_path')
    if not install_path or not os.path.exists(install_path):
        return jsonify({'ok': False, 'error': 'Install path not configured or not found'})

    plat = _detect_platform()
    cmd = []
    if plat == 'windows':
        # Look for napcat.bat (one-key) or launcher.bat (manual shell)
        cmd_path = None
        # Priority: napcat.bat -> launcher.bat -> launcher-win10.bat -> NapCatWinBootMain.exe
        candidates = ['napcat.bat', 'launcher.bat', 'launcher-win10.bat', 'NapCatWinBootMain.exe']
        
        for c in candidates:
            p = os.path.join(install_path, c)
            if os.path.exists(p):
                cmd_path = p
                break
        
        if not cmd_path:
             # Try one level deeper
             for root, dirs, files in os.walk(install_path):
                 for c in candidates:
                    if c in files:
                        cmd_path = os.path.join(root, c)
                        break
                 if cmd_path: break

        if not cmd_path:
            return jsonify({'ok': False, 'error': 'NapCat startup script or NapCatWinBootMain.exe not found'})
            
        # Determine working directory before modifying cmd list
        startup_cwd = os.path.dirname(cmd_path)
        
        cmd = [cmd_path]
    else:
        # Linux: use xvfb-run with QQ binary under NapCat install path
        qq_bin = os.path.join(install_path, 'opt', 'QQ', 'qq')
        if not os.path.exists(qq_bin):
            return jsonify({'ok': False, 'error': f'QQ binary not found at {qq_bin}'})
        cmd = ['xvfb-run', '-a', qq_bin, '--no-sandbox']
        startup_cwd = os.path.dirname(qq_bin)

    try:
        napcat_logs_buffer = [] # Clear logs on restart
        # For Windows bat files, we might need shell=True or use cmd /c, but subprocess.Popen with full path usually works.
        # However, bat files need to be executed by cmd.exe if shell=False, or just Popen them directly if .bat is associated.
        # Safe way for bat: ['cmd', '/c', cmd_path]
        
        if plat == 'windows' and cmd[0].endswith('.bat'):
            cmd = ['cmd', '/c', cmd[0]]
            
        popen_kwargs = {}
        if plat == 'windows':
            try:
                popen_kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
            except Exception:
                pass
        else:
            popen_kwargs['start_new_session'] = True

        napcat_running_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=startup_cwd,
            text=True,
            encoding='utf-8',
            errors='replace',
            **popen_kwargs
        )
        
        # Start log reader thread
        t = threading.Thread(target=_napcat_log_reader, args=(napcat_running_process,), daemon=True)
        t.start()
        
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

@app.route('/api/napcat/stop', methods=['POST'])
def napcat_stop_route():
    global napcat_running_process
    if not napcat_running_process:
        return jsonify({'ok': False, 'error': 'Not running'})
    if napcat_running_process.poll() is not None:
        napcat_running_process = None
        return jsonify({'ok': False, 'error': 'Not running'})

    ok = _terminate_process(napcat_running_process)
    if ok:
        napcat_running_process = None
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'Failed to stop'})

@app.route('/api/napcat/logs', methods=['GET'])
def napcat_logs_view_route():
    global napcat_logs_buffer
    return jsonify({'logs': napcat_logs_buffer})

@app.route('/api/napcat/webui', methods=['GET'])
def napcat_webui_info_route():
    cfg = load_json(WEBUI_CONFIG_PATH, {})
    install_path = cfg.get('napcat_install_path')
    
    if not install_path or not os.path.exists(install_path):
        return jsonify({'ok': False, 'error': 'Install path not found'})

    # Try to find webui.json
    # It could be in install_path/config/webui.json or install_path/NapCat/config/webui.json
    candidates = [
        os.path.join(install_path, 'config', 'webui.json'),
        os.path.join(install_path, 'NapCat', 'config', 'webui.json')
    ]
    
    # Also search recursively if not found immediately (depth 2)
    if not any(os.path.exists(p) for p in candidates):
        for root, dirs, files in os.walk(install_path):
            if 'webui.json' in files:
                candidates.append(os.path.join(root, 'webui.json'))
                break
                
    webui_config = None
    for p in candidates:
        if os.path.exists(p):
            webui_config = load_json(p)
            break
            
    if not webui_config:
        return jsonify({'ok': False, 'error': 'webui.json not found'})
        
    port = webui_config.get('port', 6099)
    token = webui_config.get('token', '')
    fallback_host = (request.host.split(':')[0] or '127.0.0.1')
    host = str(webui_config.get('host') or '').strip() or fallback_host
    
    if host in ['0.0.0.0', '::', '127.0.0.1', 'localhost', '']:
        host = fallback_host
        
    url = f"http://{host}:{port}/webui?token={token}"
    return jsonify({'ok': True, 'url': url, 'token': token, 'port': port})

@app.route('/api/napcat/path', methods=['POST'])
def napcat_set_path_route():
    path = (request.json or {}).get('path')
    if not path:
        return jsonify({'ok': False, 'error': 'No path provided'})
        
    if not os.path.exists(path):
        return jsonify({'ok': False, 'error': 'Path does not exist'})
        
    # Verify it looks like a NapCat installation
    # Check for NapCatWinBootMain.exe, napcat.sh, launcher.sh, napcat.bat, launcher.bat, config folder
    is_valid = False
    checks = ['napcat.bat', 'launcher.bat', 'launcher-win10.bat', 'napcat.sh', 'launcher.sh', 'NapCatWinBootMain.exe', 'config', 'NapCat']
    for c in checks:
        if os.path.exists(os.path.join(path, c)):
            is_valid = True
            break
            
    if not is_valid:
        # One more check: maybe they selected the NapCat folder inside the install dir
        parent = os.path.dirname(path)
        for c in checks:
            if os.path.exists(os.path.join(parent, c)):
                # If valid, maybe we should use parent? But let's trust user for now or just warn
                pass
        
    cfg = load_json(WEBUI_CONFIG_PATH, {})
    cfg['napcat_install_path'] = path
    save_json(WEBUI_CONFIG_PATH, cfg)
    
    return jsonify({'ok': True})

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_frontend(path):
    if path != "" and os.path.exists(os.path.join(app.static_folder, path)):
        return send_from_directory(app.static_folder, path)
    
    # If API path is not matched by other routes, return 404
    if path.startswith('api/'):
        return jsonify({'error': 'Not Found'}), 404
        
    # Otherwise return index.html for SPA routing
    return send_from_directory(app.static_folder, 'index.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
