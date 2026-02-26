"""对话界面模块

提供对话逻辑处理，与UI显示分离。
"""

import logging
from typing import Dict, Any, Optional
from ..parsers import BaseParser
from .terminal_ui import TerminalUI

logger = logging.getLogger(__name__)


class ChatInterface:
    """对话界面类
    
    负责处理对话逻辑，与UI显示分离。
    """
    
    def __init__(self, ui: TerminalUI):
        """初始化对话界面
        
        Args:
            ui: 终端UI实例
        """
        self.ui = ui
        self._running = False
    
    def start_chat(self, parser: BaseParser, config_name: str, config_data: Dict[str, Any] = None):
        """开始对话会话
        
        Args:
            parser: 解析器实例
            config_name: 配置名称
            config_data: 配置数据字典
        """
        # 获取显示名称
        if config_data:
            friendly_name = config_data.get('FriendlyName', config_name)
            model = config_data.get('Model', 'Unknown')
            display_name = f"{friendly_name} ({model})"
        else:
            display_name = config_name
            
        self.ui.show_chat_header(display_name)
        logger.info(f"开始与 {display_name} 的对话会话")
        
        self._running = True
        
        while self._running:
            try:
                user_input = self.ui.get_user_input()
                
                if not self._handle_special_commands(user_input, parser, display_name):
                    continue
                
                if not user_input:
                    continue
                
                # 处理正常的对话输入
                self._handle_chat_message(user_input, parser, display_name)
                
            except KeyboardInterrupt:
                self.ui.show_interrupted()
                logger.info("对话被用户中断")
                break
            except Exception as e:
                error_msg = f"发生错误: {e}"
                self.ui.show_error(error_msg)
                logger.error(error_msg, exc_info=True)
    
    def _handle_special_commands(self, user_input: str, parser: BaseParser, display_name: str) -> bool:
        """处理特殊命令
        
        Args:
            user_input: 用户输入
            parser: 解析器实例
            display_name: 显示名称
            
        Returns:
            True表示继续处理，False表示跳过后续处理
        """
        if user_input.lower() in ['quit', 'exit', '退出']:
            self.ui.show_goodbye()
            logger.info(f"结束与 {display_name} 的对话会话")
            self._running = False
            return False
        
        elif user_input.lower() in ['clear', '清空']:
            self._handle_clear_history(parser)
            return False
        
        elif user_input.lower() == 'info':
            self._handle_show_info(parser)
            return False
        
        elif user_input.lower() == 'help':
            self._handle_show_help()
            return False
        
        return True
    
    def _handle_clear_history(self, parser: BaseParser):
        """处理清空历史记录命令
        
        Args:
            parser: 解析器实例
        """
        if hasattr(parser, 'clear_history'):
            parser.clear_history()
            self.ui.show_history_cleared()
            logger.info("对话历史已清空")
        else:
            self.ui.show_history_not_supported()
            logger.warning("解析器不支持清空历史记录功能")
    
    def _handle_show_info(self, parser: BaseParser):
        """处理显示模型信息命令
        
        Args:
            parser: 解析器实例
        """
        try:
            model_info = parser.get_model_info()
            self.ui.show_model_info(model_info)
            logger.info("显示了模型信息")
        except Exception as e:
            error_msg = f"获取模型信息失败: {e}"
            self.ui.show_error(error_msg)
            logger.error(error_msg)
    
    def _handle_show_help(self):
        """处理显示帮助命令"""
        help_text = """
可用命令:
  quit/exit/退出 - 退出对话
  clear/清空     - 清空对话历史
  info          - 查看模型信息
  help          - 显示此帮助信息
        """
        print(help_text)
        logger.info("显示了帮助信息")
    
    def _handle_chat_message(self, user_input: str, parser: BaseParser, display_name: str):
        """处理对话消息
        
        Args:
            user_input: 用户输入
            parser: 解析器实例
            display_name: 显示名称
        """
        self.ui.show_ai_thinking()
        logger.debug(f"用户输入: {user_input}")
        
        try:
            # 检查是否为流式响应
            model_info = parser.get_model_info()
            is_stream = model_info.get('stream_enabled', False)
            
            if is_stream:
                # 流式响应
                self.ui.show_stream_start(display_name)
                response = parser.chat(user_input)
                self.ui.show_stream_end()
            else:
                # 标准响应
                response = parser.chat(user_input)
                self.ui.show_ai_response(display_name, response)
            
            logger.debug(f"AI回复: {response}")
            
        except Exception as e:
            error_msg = f"获取AI回复失败: {e}"
            self.ui.show_error(error_msg)
            logger.error(error_msg, exc_info=True)
    
    def stop_chat(self):
        """停止对话"""
        self._running = False
    
    def is_running(self) -> bool:
        """检查对话是否正在运行
        
        Returns:
            True表示正在运行，False表示已停止
        """
        return self._running