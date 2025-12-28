"""解析器管理器模块

负责动态发现、注册和管理解析器。
"""

import os
import sys
import importlib
import importlib.util
import inspect
import logging
from typing import Dict, Any, List, Type, Optional
from pathlib import Path

# 导入BaseParser基类
try:
    from ...parsers.base import BaseParser
except ImportError:
    # 兼容性导入
    sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'parsers'))
    from base import BaseParser

logger = logging.getLogger(__name__)


class ParserRegistry:
    """解析器注册表
    
    负责解析器的动态注册、发现和加载。
    """
    
    def __init__(self):
        self._parsers: Dict[str, Type[BaseParser]] = {}
        self._parser_info: Dict[str, Dict[str, Any]] = {}
    
    def register(self, name: str, parser_class: Type[BaseParser], 
                 description: str = "", aliases: List[str] = None) -> None:
        """注册解析器类
        
        Args:
            name: 解析器名称
            parser_class: 解析器类
            description: 解析器描述
            aliases: 解析器别名列表
        """
        if not issubclass(parser_class, BaseParser):
            raise ValueError(f"解析器类 {parser_class.__name__} 必须继承自 BaseParser")
        
        self._parsers[name] = parser_class
        self._parser_info[name] = {
            'class': parser_class,
            'description': description or parser_class.__doc__ or "无描述",
            'aliases': aliases or [],
            'module': parser_class.__module__
        }
        
        # 注册别名
        if aliases:
            for alias in aliases:
                self._parsers[alias] = parser_class
        
        logger.info(f"已注册解析器: {name} ({parser_class.__name__})")
    
    def get_parser_class(self, name: str) -> Optional[Type[BaseParser]]:
        """获取解析器类
        
        Args:
            name: 解析器名称或别名
            
        Returns:
            解析器类，如果不存在则返回None
        """
        return self._parsers.get(name)
    
    def create_parser(self, name: str, config: Dict[str, Any]) -> Optional[BaseParser]:
        """创建解析器实例
        
        Args:
            name: 解析器名称或别名
            config: 解析器配置
            
        Returns:
            解析器实例，如果创建失败则返回None
        """
        parser_class = self.get_parser_class(name)
        if parser_class is None:
            logger.error(f"未找到解析器: {name}")
            return None
        
        try:
            return parser_class(config)
        except Exception as e:
            logger.error(f"创建解析器 {name} 实例失败: {e}")
            return None
    
    def list_parsers(self) -> List[str]:
        """列出所有已注册的解析器名称
        
        Returns:
            解析器名称列表（不包括别名）
        """
        return list(self._parser_info.keys())
    
    def get_parser_info(self, name: str) -> Optional[Dict[str, Any]]:
        """获取解析器信息
        
        Args:
            name: 解析器名称
            
        Returns:
            解析器信息字典，如果不存在则返回None
        """
        return self._parser_info.get(name)
    
    def discover_parsers(self, parser_dir: str) -> int:
        """自动发现并注册指定目录下的解析器
        
        Args:
            parser_dir: 解析器目录路径
            
        Returns:
            发现并注册的解析器数量
        """
        if not os.path.exists(parser_dir):
            logger.error(f"解析器目录 {parser_dir} 不存在")
            return 0
        
        if not os.path.isdir(parser_dir):
            logger.error(f"路径 {parser_dir} 不是一个目录")
            return 0
        
        discovered_count = 0
        logger.info(f"开始扫描解析器目录: {parser_dir}")
        
        for filename in os.listdir(parser_dir):
            if filename.endswith('.py') and not filename.startswith('__'):
                filepath = os.path.join(parser_dir, filename)
                module_name = filename[:-3]  # 移除.py扩展名
                
                try:
                    # 构建正确的模块名称，避免相对导入问题
                    full_module_name = f"arcspec_ai.parsers.{module_name}"
                    
                    # 尝试直接导入模块
                    try:
                        module = importlib.import_module(full_module_name)
                    except ImportError:
                        # 如果直接导入失败，使用文件路径导入
                        spec = importlib.util.spec_from_file_location(full_module_name, filepath)
                        if spec is None or spec.loader is None:
                            logger.warning(f"无法加载模块规范: {filepath}")
                            continue
                        
                        module = importlib.util.module_from_spec(spec)
                        # 将模块添加到sys.modules中，避免相对导入问题
                        sys.modules[full_module_name] = module
                        spec.loader.exec_module(module)
                    
                    # 查找BaseParser的子类
                    for name, obj in inspect.getmembers(module, inspect.isclass):
                        if (obj != BaseParser and 
                            issubclass(obj, BaseParser) and 
                            obj.__module__ == full_module_name):
                            
                            # 使用类名或模块名作为解析器名称
                            parser_name = getattr(obj, 'PARSER_NAME', module_name)
                            
                            # 获取描述和别名
                            description = getattr(obj, 'PARSER_DESCRIPTION', obj.__doc__ or "")
                            aliases = getattr(obj, 'PARSER_ALIASES', [])
                            
                            self.register(parser_name, obj, description, aliases)
                            discovered_count += 1
                            
                except Exception as e:
                    logger.error(f"加载解析器文件 {filename} 失败: {e}")
        
        logger.info(f"解析器发现完成，共发现 {discovered_count} 个解析器")
        return discovered_count


# 全局注册表实例
_global_registry = ParserRegistry()


def load(parser_dir: str) -> ParserRegistry:
    """加载指定目录下的所有解析器
    
    Args:
        parser_dir: 解析器文件所在目录路径
        
    Returns:
        解析器注册表实例
    """
    registry = ParserRegistry()
    registry.discover_parsers(parser_dir)
    return registry


def create_parser(parser_type: str, config: Dict[str, Any]) -> Optional[BaseParser]:
    """使用全局注册表创建解析器实例
    
    Args:
        parser_type: 解析器类型名称
        config: 解析器配置
        
    Returns:
        解析器实例，如果创建失败则返回None
    """
    return _global_registry.create_parser(parser_type, config)


def list_parsers() -> List[str]:
    """列出全局注册表中的所有解析器
    
    Returns:
        解析器名称列表
    """
    return _global_registry.list_parsers()


def get_parser_info(name: str) -> Optional[Dict[str, Any]]:
    """获取解析器信息
    
    Args:
        name: 解析器名称
        
    Returns:
        解析器信息字典
    """
    return _global_registry.get_parser_info(name)


def get_global_registry() -> ParserRegistry:
    """获取全局解析器注册表
    
    Returns:
        全局解析器注册表实例
    """
    return _global_registry