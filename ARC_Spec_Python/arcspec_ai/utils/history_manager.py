import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class MessageRole(Enum):
    """消息角色枚举"""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    FUNCTION = "function"


@dataclass
class Message:
    """消息数据类"""
    role: MessageRole
    content: str
    tokens: Optional[int] = None
    timestamp: Optional[float] = None
    
    def to_dict(self) -> Dict[str, str]:
        """转换为字典格式"""
        return {
            "role": self.role.value,
            "content": self.content
        }


class HistoryManager:
    """历史记录管理器"""
    
    def __init__(self, max_tokens: int = 4000, max_messages: int = 50):
        """
        初始化历史记录管理器
        
        Args:
            max_tokens: 最大token数量
            max_messages: 最大消息数量
        """
        self.max_tokens = max_tokens
        self.max_messages = max_messages
        self.messages: List[Message] = []
        self.system_message: Optional[Message] = None
        
        logger.debug(f"历史记录管理器初始化，最大tokens: {max_tokens}, 最大消息数: {max_messages}")
    
    def set_system_message(self, content: str) -> None:
        """设置系统消息"""
        self.system_message = Message(
            role=MessageRole.SYSTEM,
            content=content,
            tokens=self._estimate_tokens(content)
        )
        logger.debug("系统消息已设置")
    
    def add_user_message(self, content: str) -> None:
        """添加用户消息"""
        message = Message(
            role=MessageRole.USER,
            content=content,
            tokens=self._estimate_tokens(content)
        )
        self.messages.append(message)
        logger.debug(f"添加用户消息，tokens: {message.tokens}")
        self._manage_history()
    
    def add_assistant_message(self, content: str) -> None:
        """添加助手消息"""
        message = Message(
            role=MessageRole.ASSISTANT,
            content=content,
            tokens=self._estimate_tokens(content)
        )
        self.messages.append(message)
        logger.debug(f"添加助手消息，tokens: {message.tokens}")
        self._manage_history()
    
    def get_messages_for_api(self) -> List[Dict[str, str]]:
        """获取用于API调用的消息列表"""
        api_messages = []
        
        # 添加系统消息
        if self.system_message:
            api_messages.append(self.system_message.to_dict())
        
        # 添加历史消息
        for message in self.messages:
            api_messages.append(message.to_dict())
        
        logger.debug(f"生成API消息列表，总数: {len(api_messages)}")
        return api_messages
    
    def clear_history(self) -> None:
        """清空历史记录"""
        self.messages.clear()
        logger.info("历史记录已清空")
    
    def get_total_tokens(self) -> int:
        """获取总token数量"""
        total = 0
        
        if self.system_message and self.system_message.tokens:
            total += self.system_message.tokens
        
        for message in self.messages:
            if message.tokens:
                total += message.tokens
        
        return total
    
    def get_message_count(self) -> int:
        """获取消息数量"""
        return len(self.messages)
    
    def _estimate_tokens(self, text: str) -> int:
        """估算文本的token数量
        
        这是一个简单的估算方法，实际应用中可以使用tiktoken等库进行精确计算
        """
        # 简单估算：英文约4个字符=1个token，中文约1.5个字符=1个token
        chinese_chars = sum(1 for char in text if '\u4e00' <= char <= '\u9fff')
        other_chars = len(text) - chinese_chars
        
        estimated_tokens = int(chinese_chars / 1.5 + other_chars / 4)
        return max(1, estimated_tokens)  # 至少1个token
    
    def _manage_history(self) -> None:
        """管理历史记录，确保不超过限制"""
        # 检查消息数量限制
        while len(self.messages) > self.max_messages:
            removed = self.messages.pop(0)
            logger.debug(f"移除最旧消息，角色: {removed.role.value}")
        
        # 检查token限制
        while self.get_total_tokens() > self.max_tokens and len(self.messages) > 1:
            removed = self.messages.pop(0)
            logger.debug(f"因token限制移除消息，角色: {removed.role.value}")
        
        # 确保对话的连续性：如果移除了用户消息，也要移除对应的助手回复
        self._ensure_conversation_continuity()
    
    def _ensure_conversation_continuity(self) -> None:
        """确保对话的连续性"""
        if not self.messages:
            return
        
        # 如果第一条消息是助手消息，移除它（因为缺少对应的用户消息）
        while self.messages and self.messages[0].role == MessageRole.ASSISTANT:
            removed = self.messages.pop(0)
            logger.debug(f"移除孤立的助手消息以保持对话连续性")
    
    def get_history_summary(self) -> Dict[str, Any]:
        """获取历史记录摘要"""
        return {
            "message_count": self.get_message_count(),
            "total_tokens": self.get_total_tokens(),
            "max_tokens": self.max_tokens,
            "max_messages": self.max_messages,
            "has_system_message": self.system_message is not None
        }