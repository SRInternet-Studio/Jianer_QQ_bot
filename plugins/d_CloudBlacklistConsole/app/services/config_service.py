import os
import json
from pathlib import Path
import sys

try:
    PROJECT_ROOT_FOR_IMPORT = Path(__file__).resolve().parents[4]
    if str(PROJECT_ROOT_FOR_IMPORT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT_FOR_IMPORT))
    from Tools.log_helper import project_log
except Exception:
    def project_log(*args, **kwargs):
        print(*args, **kwargs)

# 获取项目根目录（兼容各种运行方式）
def get_project_root():
    # 方法1：通过__file__回溯
    current_path = Path(__file__).resolve()
    while current_path.name != 'app' and current_path.parent != current_path:
        current_path = current_path.parent
    return current_path.parent if current_path.name == 'app' else current_path

# 配置文件路径
PROJECT_ROOT = get_project_root()
CONFIG_FILE = PROJECT_ROOT / 'blacklist_personal.json'

def load_config():
    # 确保目录存在
    os.makedirs(PROJECT_ROOT, exist_ok=True)
    
    if not CONFIG_FILE.exists():
        with open(CONFIG_FILE, 'w') as f:
            json.dump({}, f)
        return {}
    
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_config(data):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

# 调试用路径输出
if __name__ == '__main__':
    project_log(f"Project root: {PROJECT_ROOT}")
    project_log(f"Config file: {CONFIG_FILE}")
