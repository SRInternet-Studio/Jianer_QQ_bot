"""bot 业务包：从 main.py 抽出的工具/持久化/插件加载/帮助视图等模块。

为避免 import bot 时 eager 拉入整条依赖链（emoji / PIL 等），使用 PEP 562
的 __getattr__ 实现按需懒加载。
"""
import importlib

__all__ = [
    "admin_commands",
    "auth_store",
    "broadcast",
    "event_handlers",
    "feishu_bindings",
    "group_commands",
    "help_mode",
    "help_view",
    "memory_commands",
    "misc_commands",
    "plugin_ops",
    "plugin_state",
    "protocol",
    "utils",
]


def __getattr__(name):
    if name in __all__:
        module = importlib.import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
