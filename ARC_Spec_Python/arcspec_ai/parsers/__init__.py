"""解析器模块

提供各种AI模型的解析器实现。
"""

from .base import BaseParser
from .registry import ParserRegistry, get_registry, register_parser, get_parser_class, create_parser, discover_parsers
# from .openai import OpenAIParser
# from .gemini import GeminiParser

# 自动发现并注册解析器
discover_parsers()

__all__ = [
    'BaseParser',
    'ParserRegistry', 
    'get_registry',
    'register_parser',
    'get_parser_class', 
    'create_parser',
    'discover_parsers',
    # 'OpenAIParser',
    # 'GeminiParser'
]