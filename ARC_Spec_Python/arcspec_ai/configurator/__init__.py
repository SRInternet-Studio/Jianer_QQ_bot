"""Configurator模块

配置管理核心模块，包含AI配置和解析器配置管理。
"""

from . import aiconfig
from . import parser

# 导出主要接口
from .aiconfig import load as load_ai_configs, validate_config, display_config_list
from .parser import load as load_parsers, create_parser, list_parsers, get_parser_info

__all__ = [
    'aiconfig',
    'parser', 
    'load_ai_configs',
    'validate_config',
    'display_config_list',
    'load_parsers',
    'create_parser',
    'list_parsers',
    'get_parser_info'
]