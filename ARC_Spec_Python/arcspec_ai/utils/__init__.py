"""工具模块

包含历史记录管理、配置加载等工具类。
注意：配置加载功能已迁移到 arcspec_ai.configurator 模块。
"""

from .history_manager import HistoryManager, MessageRole, Message
# 为了向后兼容，保留原有的配置加载接口
from .config_loader import load_ai_configs, display_config_list

# 同时提供新的configurator模块接口
from ..configurator import load_ai_configs as new_load_ai_configs
from ..configurator import display_config_list as new_display_config_list

__all__ = [
    'HistoryManager',
    'MessageRole', 
    'Message',
    # 原有接口（向后兼容）
    'load_ai_configs',
    'display_config_list',
    # 新接口
    'new_load_ai_configs',
    'new_display_config_list'
]