from __future__ import annotations

import sys
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = CURRENT_DIR.parent
SRC_ROOT = PLUGIN_ROOT.parent
# The vendored package lives at plugins/JianerAI/memorix. The repository root
# is three parents above PLUGIN_ROOT; adding it makes ``python -m ...scripts``
# and direct script execution resolve the same package imports.
PROJECT_ROOT = PLUGIN_ROOT.parent.parent.parent
WORKSPACE_ROOT = PROJECT_ROOT
WORKSPACE_ROOT = PROJECT_ROOT

for _path in (SRC_ROOT, PROJECT_ROOT, PLUGIN_ROOT):
    _path_str = str(_path)
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)

from plugins.JianerAI.memorix.paths import config_path, default_data_dir, resolve_repo_path as resolve_repo_path  # noqa: E402

DEFAULT_CONFIG_PATH = config_path()
DEFAULT_DATA_DIR = default_data_dir()
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "HostRuntime.db"
