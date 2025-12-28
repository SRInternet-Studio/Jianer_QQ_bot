"""解析器注册表模块

提供解析器的动态注册、发现和加载功能。
"""

import logging
import importlib
import inspect
from typing import Dict, Type, List, Optional
from .base import BaseParser

logger = logging.getLogger(__name__)

class ParserRegistry:
    """解析器注册表
    
    负责管理所有可用的解析器类，支持动态注册和发现。
    """
    
    def __init__(self):
        self._parsers: Dict[str, Type[BaseParser]] = {}
        self._aliases: Dict[str, str] = {}
        
    def register(self, name: str, parser_class: Type[BaseParser], aliases: Optional[List[str]] = None):
        """注册解析器
        
        Args:
            name: 解析器名称
            parser_class: 解析器类
            aliases: 别名列表
        """
        if not issubclass(parser_class, BaseParser):
            raise ValueError(f"解析器类 {parser_class.__name__} 必须继承自 BaseParser")
            
        self._parsers[name.lower()] = parser_class
        logger.info(f"注册解析器: {name} -> {parser_class.__name__}")
        
        # 注册别名
        if aliases:
            for alias in aliases:
                self._aliases[alias.lower()] = name.lower()
                logger.debug(f"注册别名: {alias} -> {name}")
    
    def get_parser_class(self, name: str) -> Optional[Type[BaseParser]]:
        """获取解析器类
        
        Args:
            name: 解析器名称或别名
            
        Returns:
            解析器类，如果不存在则返回None
        """
        name = name.lower()
        
        # 先检查别名
        if name in self._aliases:
            name = self._aliases[name]
            
        return self._parsers.get(name)
    
    def create_parser(self, name: str, config: Dict) -> Optional[BaseParser]:
        """创建解析器实例
        
        Args:
            name: 解析器名称或别名
            config: 配置字典
            
        Returns:
            解析器实例，如果创建失败则返回None
        """
        parser_class = self.get_parser_class(name)
        if parser_class is None:
            logger.error(f"未找到解析器: {name}")
            return None
            
        try:
            parser = parser_class(config)
            logger.info(f"成功创建解析器实例: {name} -> {parser_class.__name__}")
            return parser
        except Exception as e:
            logger.error(f"创建解析器实例失败: {name} -> {e}")
            return None
    
    def list_parsers(self) -> List[str]:
        """列出所有已注册的解析器
        
        Returns:
            解析器名称列表
        """
        return list(self._parsers.keys())
    
    def discover_parsers(self, module_name: str = "arcspec_ai.parsers"):
        """自动发现并注册解析器
        
        Args:
            module_name: 要搜索的模块名称
        """
        try:
            module = importlib.import_module(module_name)
            logger.info(f"开始在模块 {module_name} 中发现解析器")
            
            # 遍历模块中的所有属性
            for name in dir(module):
                obj = getattr(module, name)
                
                # 检查是否为BaseParser的子类（但不是BaseParser本身）
                if (inspect.isclass(obj) and 
                    issubclass(obj, BaseParser) and 
                    obj is not BaseParser):
                    
                    # 自动注册解析器
                    parser_name = name.lower().replace('parser', '')
                    if parser_name not in self._parsers:
                        self.register(parser_name, obj)
                        
        except ImportError as e:
            logger.error(f"无法导入模块 {module_name}: {e}")
        except Exception as e:
            logger.error(f"发现解析器时出错: {e}")

# 全局解析器注册表实例
_registry = ParserRegistry()

def get_registry() -> ParserRegistry:
    """获取全局解析器注册表
    
    Returns:
        解析器注册表实例
    """
    return _registry

def register_parser(name: str, parser_class: Type[BaseParser], aliases: Optional[List[str]] = None):
    """注册解析器的便捷函数
    
    Args:
        name: 解析器名称
        parser_class: 解析器类
        aliases: 别名列表
    """
    _registry.register(name, parser_class, aliases)

def get_parser_class(name: str) -> Optional[Type[BaseParser]]:
    """获取解析器类的便捷函数
    
    Args:
        name: 解析器名称或别名
        
    Returns:
        解析器类，如果不存在则返回None
    """
    return _registry.get_parser_class(name)

def create_parser(name: str, config: Dict) -> Optional[BaseParser]:
    """创建解析器实例的便捷函数
    
    Args:
        name: 解析器名称或别名
        config: 配置字典
        
    Returns:
        解析器实例，如果创建失败则返回None
    """
    return _registry.create_parser(name, config)

def discover_parsers():
    """自动发现解析器的便捷函数"""
    _registry.discover_parsers()