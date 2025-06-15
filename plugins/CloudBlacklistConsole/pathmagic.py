import sys
from pathlib import Path

def _find_root():
    # 通过__file__回溯
    current = Path(__file__).resolve()
    while current.parent != current and not (current / "setup.py").exists():
        current = current.parent
    
    # 通过cwd判断
    if (Path.cwd() / "config").exists():
        return Path.cwd()
    
    return current

# 获取根目录路径
ROOT = _find_root()

# 添加插件根目录到系统路径
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 添加子目录到系统路径（用于独立调试）
for subdir in ["config", "core"]:
    path = ROOT / subdir
    if path.exists() and str(path) not in sys.path:
        sys.path.insert(0, str(path))