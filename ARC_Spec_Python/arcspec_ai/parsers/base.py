from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional


class BaseParser(ABC):
    """解析器抽象基类，定义所有解析器必须实现的接口"""
    
    def __init__(self, config: Dict[str, Any]):
        """初始化解析器
        
        Args:
            config: 配置字典，包含API密钥、模型等信息
        """
        self.config = config
        self.validate_config()
    
    @abstractmethod
    def validate_config(self) -> None:
        """验证配置是否有效
        
        Raises:
            ValueError: 当配置无效时抛出异常
        """
        pass
    
    @abstractmethod
    def chat(self, message: str, history: Optional[List[Dict[str, str]]] = None, **kwargs) -> str:
        """发送聊天消息并获取回复
        
        Args:
            message: 用户消息
            history: 对话历史记录
            **kwargs: 其他参数
            
        Returns:
            AI的回复内容
            
        Raises:
            Exception: 当API调用失败时抛出异常
        """
        pass
    
    @abstractmethod
    def get_model_info(self) -> Dict[str, Any]:
        """获取模型信息
        
        Returns:
            包含模型信息的字典
        """
        pass
    
    def get_config_value(self, key: str, default: Any = None) -> Any:
        """安全获取配置值
        
        Args:
            key: 配置键名
            default: 默认值
            
        Returns:
            配置值或默认值
        """
        return self.config.get(key, default)
    
    def require_config_value(self, key: str) -> Any:
        """获取必需的配置值
        
        Args:
            key: 配置键名
            
        Returns:
            配置值
            
        Raises:
            ValueError: 当配置键不存在或值为空时抛出异常
        """
        value = self.config.get(key)
        if value is None:
            raise ValueError(f"Required configuration key '{key}' is missing")
        return value
    
    def parse(self, message: str, history: Optional[List[Dict[str, str]]] = None, **kwargs) -> str:
        """解析消息并获取回复（chat方法的别名，保持向后兼容性）
        
        Args:
            message: 用户消息
            history: 对话历史记录
            **kwargs: 其他参数
            
        Returns:
            AI的回复内容
        """
        return self.chat(message, history, **kwargs)