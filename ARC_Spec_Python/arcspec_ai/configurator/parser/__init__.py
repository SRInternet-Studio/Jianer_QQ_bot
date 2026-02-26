"""解析器管理模块

负责动态发现、注册和管理解析器。
"""

from .manager import load, create_parser, list_parsers, get_parser_info

__all__ = ['load', 'create_parser', 'list_parsers', 'get_parser_info']