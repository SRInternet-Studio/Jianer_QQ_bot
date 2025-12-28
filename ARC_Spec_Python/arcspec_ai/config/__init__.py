"""配置模块

负责解析器配置和工厂创建。
注意：此模块已被重构，建议使用 arcspec_ai.configurator 模块。
"""

# 为了向后兼容，保留原有接口
from .parser_factory import get_parser_by_config, list_available_parsers, get_parser_info

# 同时提供新的configurator模块接口
from ..configurator import load_ai_configs, display_config_list, load_parsers

__all__ = [
    # 原有接口（向后兼容）
    'get_parser_by_config', 'list_available_parsers', 'get_parser_info',
    # 新接口
    'load_ai_configs', 'display_config_list', 'load_parsers'
]