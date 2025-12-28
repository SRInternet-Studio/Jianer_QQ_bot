import sys
import os
import logging
import asyncio
import inspect
from typing import Optional, Dict, List, Any

# 添加 ARC_Spec_Python 到路径
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARC_SPEC_PATH = os.path.join(PROJECT_ROOT, "ARC_Spec_Python")
if ARC_SPEC_PATH not in sys.path:
    sys.path.insert(0, ARC_SPEC_PATH)

from arcspec_ai.configurator import load_ai_configs, load_parsers

logger = logging.getLogger(__name__)

class ARC_Manager:
    def __init__(self):
        self.config_dir = os.path.join(PROJECT_ROOT, "aiconfig")
        self.parsers_dir = os.path.join(PROJECT_ROOT, "parser")
        self.configs = {}
        self.parser_registry = None
        self.active_parsers = {}
        self.initialized = False

    def initialize(self):
        if self.initialized: return
        try:
            self.configs = load_ai_configs(self.config_dir)
            self.parser_registry = load_parsers(self.parsers_dir)
            self.initialized = True
            logger.info(f"ARC AI Initialized. Configs: {len(self.configs)}")
        except Exception as e:
            logger.error(f"Failed to initialize ARC AI: {e}")

    def list_configs(self) -> Dict[str, str]:
        """
        列出所有可用配置
        Returns:
            Dict[str, str]: ConfigName -> FriendlyName
        """
        if not self.initialized: self.initialize()
        return {name: config.get('FriendlyName', name) for name, config in self.configs.items()}
    
    def get_friendly_name(self, config_name: str) -> str:
        if not self.initialized: self.initialize()
        if config_name in self.configs:
            return self.configs[config_name].get('FriendlyName', config_name)
        return config_name

    def get_parser(self, config_name: str):
        if not self.initialized: self.initialize()
        
        if config_name not in self.configs:
            logger.error(f"Config {config_name} not found")
            return None
            
        if config_name in self.active_parsers:
            return self.active_parsers[config_name]
            
        config = self.configs[config_name]
        try:
            parser = self.parser_registry.create_parser(config['ResponseType'], config)
            if parser:
                self.active_parsers[config_name] = parser
                return parser
        except Exception as e:
            logger.error(f"Failed to create parser for {config_name}: {e}")
            return None
        return None

_manager = ARC_Manager()

def list_available_ais() -> Dict[str, str]:
    """
    返回 {config_name: friendly_name}
    """
    return _manager.list_configs()

def get_current_ai_name(config_name: str) -> str:
    return _manager.get_friendly_name(config_name)

async def get_response_stream(config_name: str, message: str, user_lists: Dict, uid: Any, sys_prompt: str, images: List = None):
    """
    获取 AI 回复的生成器，适配 main.py 的 handle_message_stream
    config_name: 直接传入配置文件的名称 (无后缀)
    """
    parser = _manager.get_parser(config_name)
    if not parser:
        yield f"无法加载配置: {config_name}，请检查配置是否存在。", 'message'
        return

    uid_str = str(uid)
    if uid_str not in user_lists:
        user_lists[uid_str] = []
    
    # 准备历史记录 (复制一份以免污染原始数据)
    history = list(user_lists[uid_str])
    
    # 插入系统提示词 (如果存在)
    if sys_prompt:
        history.insert(0, {"role": "system", "content": sys_prompt})

    # 调用解析器
    try:
        kwargs = {}
        if images:
             kwargs['image_urls'] = images # Gemini parser uses image_urls for URLs

        # 检查是否支持异步调用
        if hasattr(parser, '_chat_async') and inspect.iscoroutinefunction(parser._chat_async):
            response_text = await parser._chat_async(message, history=history, **kwargs)
        else:
            # 同步解析器，在线程池中运行以避免阻塞事件循环
            response_text = await asyncio.to_thread(parser.parse, message, history=history, **kwargs)
        
        # Yield 结果
        yield response_text, 'message'
        
        # 更新 user_lists (这是持久化的历史记录)
        user_lists[uid_str].append({"role": "user", "content": message})
        user_lists[uid_str].append({"role": "assistant", "content": response_text})
        
        # 限制历史记录长度
        if len(user_lists[uid_str]) > 20:
            user_lists[uid_str] = user_lists[uid_str][-20:]
            
        yield user_lists, 'user_lists'

    except Exception as e:
        logger.error(f"Error generating response: {e}")
        yield f"发生错误: {e}", 'message'
