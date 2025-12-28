"""解析器工厂模块

根据配置创建相应的解析器实例。
"""

import logging
from typing import Dict, Any, Optional
from ..parsers import BaseParser, create_parser, get_registry

logger = logging.getLogger(__name__)

def get_parser_by_config(config: Dict[str, Any]) -> BaseParser:
    """根据配置创建解析器
    
    Args:
        config: 配置字典
        
    Returns:
        解析器实例
        
    Raises:
        ValueError: 不支持的ResponseType或创建失败
    """
    response_type = config.get('ResponseType', '').lower()
    is_multimodal = config.get('it_multimodal_model', 'False').lower() == 'true'
    
    logger.info(f"创建解析器，类型: {response_type}, 多模态: {is_multimodal}")
    
    if is_multimodal:
        # 目前还没有多模态解析器，使用普通解析器
        logger.warning("注意: 多模态模型暂时使用普通解析器")
        print("注意: 多模态模型暂时使用普通解析器")
    
    # 使用动态加载机制创建解析器
    parser = create_parser(response_type, config)
    
    if parser is None:
        # 列出可用的解析器
        available_parsers = get_registry().list_parsers()
        error_msg = f"不支持的ResponseType: {response_type}。可用的解析器: {', '.join(available_parsers)}"
        logger.error(error_msg)
        raise ValueError(error_msg)
    
    return parser

def list_available_parsers() -> list:
    """列出所有可用的解析器
    
    Returns:
        解析器名称列表
    """
    return get_registry().list_parsers()

def get_parser_info() -> Dict[str, str]:
    """获取解析器信息
    
    Returns:
        解析器信息字典
    """
    registry = get_registry()
    info = {}
    
    for parser_name in registry.list_parsers():
        parser_class = registry.get_parser_class(parser_name)
        if parser_class:
            info[parser_name] = parser_class.__doc__ or "无描述"
    
    return info