import openai
import logging
from typing import Dict, Any, List, Optional
from .base import BaseParser
from ..utils import HistoryManager

# 配置日志
logger = logging.getLogger(__name__)


class OpenAIParser(BaseParser):
    """OpenAI API解析器"""
    
    def __init__(self, config: Dict[str, Any]):
        """初始化OpenAI解析器
        
        Args:
            config: 配置字典，包含API密钥、模型等信息
        """
        super().__init__(config)
        
        # 使用安全的配置访问方式
        api_key = self.require_config_value('APIKey')
        base_url = self.get_config_value('BaseURL', 'https://api.openai.com/v1')
        
        self.client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url
        )
        
        self.model = self.require_config_value('Model')
        self.temperature = self.get_config_value('Temperature', 0.5)
        self.max_tokens = self.get_config_value('MaxTokens', 1000)
        self.top_p = self.get_config_value('TopP', 1)
        self.personality = self.get_config_value('Personality', '')
        self.if_return_none = self.get_config_value('if_return_none', '模型没有返回任何内容')
        
        # 其他参数
        self.other_params = self.get_config_value('other', {})
        self.extra_body = self.get_config_value('Extra_Body', {})
        
        # 初始化历史记录管理器
        max_history_tokens = self.get_config_value('max_history_tokens', 3000)
        max_history_messages = self.get_config_value('max_history_messages', 20)
        self.history_manager = HistoryManager(
            max_tokens=max_history_tokens,
            max_messages=max_history_messages
        )
        
        # 设置系统人格
        if self.personality:
            self.history_manager.set_system_message(self.personality)
        
        logger.info(f"OpenAI解析器初始化完成，模型: {self.model}")
    
    def validate_config(self) -> None:
        """验证配置是否有效"""
        required_keys = ['APIKey', 'Model']
        for key in required_keys:
            if not self.get_config_value(key):
                raise ValueError(f"配置中缺少必需的键: {key}")
    
    def chat(self, message: str, history: Optional[List[Dict[str, str]]] = None, **kwargs) -> str:
        """
        发送聊天消息并获取回复
        
        Args:
            message: 用户消息
            history: 对话历史记录
            **kwargs: 其他参数
            
        Returns:
            AI的回复内容
        """
        try:
            logger.debug(f"开始处理聊天请求，消息长度: {len(message)}")
            
            # 构建消息列表
            messages = []
            
            # 添加系统人格设定
            if self.personality:
                messages.append({
                    "role": "system",
                    "content": self.personality
                })
                logger.debug("已添加系统人格设定")
            
            # 添加对话历史（改进的历史记录管理将在后续实现）
            if history:
                # 临时保持原有逻辑，后续会重构
                recent_history = history[-10:]  # 只保留最近10条记录
                for entry in recent_history:
                    if isinstance(entry, dict) and 'role' in entry and 'content' in entry:
                        messages.append(entry)
                logger.debug(f"已添加历史记录，条数: {len(recent_history)}")
            
            # 添加当前用户消息
            messages.append({
                "role": "user",
                "content": message
            })
            
            # 准备API调用参数
            api_params = {
                "model": self.model,
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "top_p": self.top_p,
                **self.other_params
            }
            
            # 添加额外的body参数
            if self.extra_body:
                api_params.update(self.extra_body)
            
            # 检查是否启用流式响应（从other参数或直接配置中获取）
            is_stream = self.other_params.get('stream', self.get_config_value('stream', False))
            if is_stream:
                api_params['stream'] = True
                logger.debug("启用流式响应模式")
                return self._handle_stream_response(api_params)
            else:
                logger.debug("使用标准响应模式")
                # 调用OpenAI API
                response = self.client.chat.completions.create(**api_params)
                
                # 提取回复内容
                if response.choices and response.choices[0].message:
                    response_content = response.choices[0].message.content or self.if_return_none
                    logger.info(f"API调用成功，回复长度: {len(response_content)}")
                else:
                    logger.warning("API返回空内容")
                    response_content = self.if_return_none
            
            # 将助手回复添加到历史管理器
            if response_content and response_content != self.if_return_none:
                self.history_manager.add_assistant_message(response_content)
            
            return response_content.strip() if response_content != self.if_return_none else response_content
                    
        except Exception as e:
            logger.error(f"API调用失败: {str(e)}")
            raise e
    
    def _handle_stream_response(self, api_params: dict) -> str:
        """
        处理流式响应
        
        Args:
            api_params: API调用参数
            
        Returns:
            完整的AI回复内容
        """
        try:
            logger.debug("开始处理流式响应")
            
            # 创建流式响应
            stream = self.client.chat.completions.create(**api_params)
            
            full_response = ""
            print("\n", end="", flush=True)  # 换行准备显示流式内容
            
            for chunk in stream:
                # 检查chunk是否有choices属性且不为空
                if hasattr(chunk, 'choices') and chunk.choices and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    if hasattr(delta, 'content') and delta.content is not None:
                        content = delta.content
                        full_response += content
                        # 实时显示内容
                        print(content, end="", flush=True)
            
            print()  # 流式输出结束后换行
            
            logger.info(f"流式响应完成，总长度: {len(full_response)}")
            return full_response.strip() if full_response else self.if_return_none
            
        except Exception as e:
            logger.error(f"流式响应错误: {str(e)}")
            raise e
    
    def clear_history(self) -> None:
        """清空对话历史记录"""
        self.history_manager.clear_history()
        # 重新设置系统人格
        if self.personality:
            self.history_manager.set_system_message(self.personality)
        logger.info("对话历史记录已清空")
    
    def get_history_summary(self) -> Dict[str, Any]:
        """获取历史记录摘要"""
        return self.history_manager.get_history_summary()
    
    def get_model_info(self) -> Dict[str, Any]:
        """
        获取模型信息
        
        Returns:
            包含模型信息的字典
        """
        info = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "top_p": self.top_p,
            "personality": self.personality,
            "base_url": str(self.client.base_url),
            "extra_params": self.other_params,
            "extra_body": self.extra_body,
            "stream_enabled": self.other_params.get('stream', self.get_config_value('stream', False)),
            "multimodal": self.get_config_value('it_multimodal_model', False),
            "history_summary": self.get_history_summary()
        }
        logger.debug(f"获取模型信息: {info['model']}")
        return info