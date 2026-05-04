"""插件加载器：扫描 plugins/ 目录加载文件/目录形式的插件。"""
import importlib.util
import os
import sys
import traceback
import uuid
from dataclasses import dataclass, field

PLUGIN_FOLDER = "plugins"

_INCOMPATIBLE_IN_FEISHU = {
    "CheckAccount",
    "CheckGroup",
    "LikePlugin",
    "AdvancedQuote",
    "SumUp_MySQL",
}


@dataclass
class LoadResult:
    plugins: list = field(default_factory=list)
    loaded: list = field(default_factory=list)              # 内部唯一模块名（含 uuid 后缀）
    loaded_display: list = field(default_factory=list)      # 展示用名称（与 disabled 对齐）
    disabled: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    _help_lines: list = field(default_factory=list)

    @property
    def help_text(self) -> str:
        return "".join(f"\n       {line}" for line in self._help_lines)


def _register_module(module, unique_module_name: str, module_name: str, result: LoadResult, logger) -> None:
    """验证并注册一个已 exec 的插件模块到 result。"""
    if not (hasattr(module, "TRIGGHT_KEYWORD") and hasattr(module, "on_message")):
        result.failed.append(f"{module_name} (缺少 TRIGGHT_KEYWORD：触发标识符 或 on_message：触发函数后端)")
        return
    if not isinstance(module.TRIGGHT_KEYWORD, str):
        result.failed.append(f"{module_name} (TRIGGHT_KEYWORD 必须是字符串)")
        return

    result.plugins.append(module)
    result.loaded.append(unique_module_name)
    result.loaded_display.append(module_name)
    if hasattr(module, "HELP_MESSAGE") and isinstance(module.HELP_MESSAGE, str):
        for line in (ln.strip() for ln in module.HELP_MESSAGE.splitlines()):
            if line:
                result._help_lines.append(line)
    logger.info(f"已加载插件: {unique_module_name} (关键词: {module.TRIGGHT_KEYWORD})")


def _load_single(entry_path: str, module_name: str, result: LoadResult, logger) -> None:
    """加载单个插件入口（文件或 setup.py），失败时清理 sys.modules。"""
    unique_module_name = f"{module_name}_{uuid.uuid4().hex}"
    try:
        spec = importlib.util.spec_from_file_location(unique_module_name, entry_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[unique_module_name] = module
        spec.loader.exec_module(module)
        _register_module(module, unique_module_name, module_name, result, logger)
    except ImportError as e:
        result.failed.append(f"{module_name} (导入错误: {e})")
        logger.error(f"加载插件 {unique_module_name} 失败，原因是: \n{traceback.format_exc()}\n")
        sys.modules.pop(unique_module_name, None)
    except Exception as e:
        result.failed.append(f"{module_name} (其他错误: {str(e)})")
        logger.error(f"加载插件 {unique_module_name} 失败: \n{traceback.format_exc()}\n")
        sys.modules.pop(unique_module_name, None)


def load_plugins(config, logger) -> LoadResult:
    """扫描 PLUGIN_FOLDER 加载所有启用的插件，返回 LoadResult。"""
    result = LoadResult()
    protocol_now = str(config.protocol).lower()

    if not os.path.exists(PLUGIN_FOLDER):
        os.makedirs(PLUGIN_FOLDER)

    for filename in os.listdir(PLUGIN_FOLDER):
        logger.debug(f"check file or directory: {filename}")

        if filename == "__pycache__":
            logger.debug("Directory __pycache__ not load.")
            continue

        # 已禁用插件：剥离 d_ 前缀后入列，便于展示
        if filename.startswith("d_"):
            display = os.path.splitext(filename[2:])[0]
            result.disabled.append(display)
            continue

        plugin_base_name = os.path.splitext(filename)[0]
        if protocol_now == "feishu" and plugin_base_name in _INCOMPATIBLE_IN_FEISHU:
            result.disabled.append(plugin_base_name)
            logger.info(f"Feishu 模式跳过不兼容插件: {plugin_base_name}")
            continue

        plugin_path = os.path.join(PLUGIN_FOLDER, filename)

        if os.path.isdir(plugin_path):
            setup_file = os.path.join(plugin_path, "setup.py")
            if os.path.exists(setup_file):
                _load_single(setup_file, filename, result, logger)
            else:
                logger.warning(f"目录 {filename} 中缺少 setup.py 文件")
                result.failed.append(f"{filename} (入口错误: 缺少 setup.py 文件)")

        elif filename.endswith(".py") or filename.endswith(".pyw"):
            module_name = os.path.splitext(filename)[0]
            _load_single(os.path.join(PLUGIN_FOLDER, filename), module_name, result, logger)

        else:
            logger.debug(f"跳过非插件文件或目录: {filename}")

    logger.info(f"成功加载 {len(result.loaded)} 个插件")
    return result
