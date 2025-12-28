#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基本集成示例

这个示例展示了如何在其他Python项目中集成ARC_Spec_Python。
适用于简单的脚本或小型应用。
"""

import sys
import os
import logging
from pathlib import Path
from typing import Optional, Dict, Any

# 添加ARC_Spec_Python到Python路径
# 假设这个文件在ARC_Spec_Python/examples/目录下
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# 导入ARC_Spec_Python模块
try:
    from arcspec_ai.configurator import load_ai_configs, load_parsers
except ImportError as e:
    print(f"导入失败: {e}")
    print("请确保ARC_Spec_Python项目路径正确")
    sys.exit(1)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class SimpleAIClient:
    """简单的AI客户端封装"""
    
    def __init__(self, project_root: str = None):
        """
        初始化AI客户端
        
        Args:
            project_root: ARC_Spec_Python项目根目录路径
        """
        if project_root is None:
            project_root = Path(__file__).parent.parent
        else:
            project_root = Path(project_root)
            
        self.project_root = project_root
        self.config_dir = project_root / 'configs'
        self.parsers_dir = project_root / 'arcspec_ai' / 'parsers'
        
        self.configs = {}
        self.parser_registry = None
        self.current_parser = None
        self.current_config_name = None
        
    def initialize(self) -> bool:
        """
        初始化AI客户端
        
        Returns:
            bool: 初始化是否成功
        """
        try:
            # 检查目录是否存在
            if not self.config_dir.exists():
                logger.error(f"配置目录不存在: {self.config_dir}")
                return False
                
            if not self.parsers_dir.exists():
                logger.error(f"解析器目录不存在: {self.parsers_dir}")
                return False
            
            # 加载配置
            logger.info("正在加载AI配置...")
            self.configs = load_ai_configs(str(self.config_dir))
            
            if not self.configs:
                logger.warning("未找到任何配置文件")
                return False
                
            logger.info(f"成功加载 {len(self.configs)} 个配置")
            
            # 加载解析器
            logger.info("正在加载解析器...")
            self.parser_registry = load_parsers(str(self.parsers_dir))
            
            available_parsers = self.parser_registry.list_parsers()
            logger.info(f"发现 {len(available_parsers)} 个解析器: {available_parsers}")
            
            return True
            
        except Exception as e:
            logger.error(f"初始化失败: {e}")
            return False
    
    def list_configs(self) -> Dict[str, str]:
        """
        列出所有可用配置
        
        Returns:
            Dict[str, str]: 配置名称到友好名称的映射
        """
        return {
            name: config.get('FriendlyName', name)
            for name, config in self.configs.items()
        }
    
    def set_config(self, config_name: str) -> bool:
        """
        设置当前使用的配置
        
        Args:
            config_name: 配置名称
            
        Returns:
            bool: 设置是否成功
        """
        if config_name not in self.configs:
            logger.error(f"配置 '{config_name}' 不存在")
            return False
            
        config = self.configs[config_name]
        
        try:
            # 创建解析器
            parser = self.parser_registry.create_parser(
                config['ResponseType'], config
            )
            
            if parser:
                self.current_parser = parser
                self.current_config_name = config_name
                logger.info(f"成功设置配置: {config.get('FriendlyName', config_name)}")
                return True
            else:
                logger.error(f"无法创建解析器: {config['ResponseType']}")
                return False
                
        except Exception as e:
            logger.error(f"设置配置失败: {e}")
            return False
    
    def chat(self, message: str) -> Optional[str]:
        """
        发送消息并获取AI回复
        
        Args:
            message: 用户消息
            
        Returns:
            Optional[str]: AI回复，失败时返回None
        """
        if not self.current_parser:
            logger.error("请先设置配置")
            return None
            
        try:
            response = self.current_parser.parse(message)
            return response
        except Exception as e:
            logger.error(f"对话失败: {e}")
            return None
    
    def get_current_info(self) -> Optional[Dict[str, Any]]:
        """
        获取当前配置和解析器信息
        
        Returns:
            Optional[Dict[str, Any]]: 当前信息
        """
        if not self.current_parser or not self.current_config_name:
            return None
            
        config = self.configs[self.current_config_name]
        model_info = self.current_parser.get_model_info()
        
        return {
            'config_name': self.current_config_name,
            'friendly_name': config.get('FriendlyName', self.current_config_name),
            'model': config.get('Model', 'Unknown'),
            'response_type': config.get('ResponseType', 'Unknown'),
            'model_info': model_info
        }


def demo_basic_usage():
    """基本使用演示"""
    print("=== ARC_Spec_Python 基本集成演示 ===")
    
    # 创建AI客户端
    client = SimpleAIClient()
    
    # 初始化
    if not client.initialize():
        print("❌ AI客户端初始化失败")
        return
    
    print("✅ AI客户端初始化成功")
    
    # 列出可用配置
    configs = client.list_configs()
    print(f"\n📋 可用配置 ({len(configs)} 个):")
    for name, friendly_name in configs.items():
        print(f"  - {name}: {friendly_name}")
    
    # 设置第一个配置
    if configs:
        config_name = list(configs.keys())[0]
        print(f"\n🔧 设置配置: {config_name}")
        
        if client.set_config(config_name):
            print("✅ 配置设置成功")
            
            # 显示当前信息
            info = client.get_current_info()
            if info:
                print(f"\n📊 当前配置信息:")
                print(f"  配置名称: {info['config_name']}")
                print(f"  友好名称: {info['friendly_name']}")
                print(f"  模型: {info['model']}")
                print(f"  解析器类型: {info['response_type']}")
            
            # 测试对话
            print("\n💬 测试对话:")
            test_messages = [
                "你好！",
                "请简单介绍一下你自己",
                "今天是个好天气"
            ]
            
            for i, message in enumerate(test_messages, 1):
                print(f"\n[{i}] 用户: {message}")
                response = client.chat(message)
                
                if response:
                    # 限制显示长度
                    display_response = response[:200] + "..." if len(response) > 200 else response
                    print(f"[{i}] AI: {display_response}")
                else:
                    print(f"[{i}] ❌ 对话失败")
        else:
            print("❌ 配置设置失败")
    else:
        print("❌ 没有可用的配置")


def demo_multiple_configs():
    """多配置使用演示"""
    print("\n=== 多配置使用演示 ===")
    
    client = SimpleAIClient()
    
    if not client.initialize():
        print("❌ 初始化失败")
        return
    
    configs = client.list_configs()
    message = "请用一句话介绍你自己"
    
    print(f"\n📝 测试消息: {message}")
    print(f"\n🔄 使用 {len(configs)} 个配置进行测试:")
    
    for i, (config_name, friendly_name) in enumerate(configs.items(), 1):
        print(f"\n[{i}] 配置: {friendly_name}")
        
        if client.set_config(config_name):
            response = client.chat(message)
            if response:
                # 限制显示长度
                display_response = response[:150] + "..." if len(response) > 150 else response
                print(f"    回复: {display_response}")
            else:
                print("    ❌ 对话失败")
        else:
            print("    ❌ 配置设置失败")


def demo_error_handling():
    """错误处理演示"""
    print("\n=== 错误处理演示 ===")
    
    client = SimpleAIClient()
    
    # 测试未初始化的情况
    print("\n1. 测试未初始化的情况:")
    response = client.chat("Hello")
    print(f"   结果: {response}")
    
    # 初始化
    if client.initialize():
        # 测试不存在的配置
        print("\n2. 测试不存在的配置:")
        result = client.set_config("nonexistent_config")
        print(f"   结果: {result}")
        
        # 测试正常配置但异常消息
        configs = client.list_configs()
        if configs:
            config_name = list(configs.keys())[0]
            if client.set_config(config_name):
                print("\n3. 测试空消息:")
                response = client.chat("")
                print(f"   结果: {response}")


if __name__ == "__main__":
    # 运行演示
    try:
        demo_basic_usage()
        demo_multiple_configs()
        demo_error_handling()
        
        print("\n🎉 演示完成！")
        print("\n💡 提示:")
        print("  - 可以修改configs目录下的配置文件来测试不同的AI模型")
        print("  - 可以在arcspec_ai/parsers目录下添加自定义解析器")
        print("  - 查看API_USAGE.md了解更多高级用法")
        
    except KeyboardInterrupt:
        print("\n\n👋 用户中断，程序退出")
    except Exception as e:
        logger.error(f"演示过程中发生错误: {e}")
        print(f"\n❌ 演示失败: {e}")