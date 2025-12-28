"""ARC Spec AI - AI配置管理和解析器框架

这是一个用于管理AI配置和解析器的Python包。
重构后的版本提供了更好的模块化架构。
"""

__version__ = "1.0.0"
__author__ = "ARC Spec Team"
__description__ = "AI配置管理和解析器框架"

# 原有接口（向后兼容）
from .config import get_parser_by_config, list_available_parsers, get_parser_info
from .utils import HistoryManager, MessageRole, Message

# 新的configurator模块接口
from .configurator import (
    load_ai_configs,
    display_config_list,
    load_parsers,
    validate_config
)

__all__ = [
    # 核心类和工具
    'BaseParser',
    'OpenAIParser', 
    'HistoryManager',
    'MessageRole',
    'Message',
    # 原有接口（向后兼容）
    'get_parser_by_config',
    'list_available_parsers',
    'get_parser_info',
    'register_parser',
    'get_parser_class',
    'create_parser',
    'discover_parsers',
    # 新的configurator模块接口
    'load_ai_configs',
    'display_config_list',
    'load_parsers',
    'validate_config'
]