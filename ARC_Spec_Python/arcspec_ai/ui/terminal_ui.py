"""终端用户界面模块

提供终端UI组件，负责用户界面的显示和交互，与日志系统分离。
"""

import sys
from typing import Dict, Any, List, Optional
from enum import Enum
from dataclasses import dataclass


class UITheme(Enum):
    """UI主题枚举"""
    DEFAULT = "default"
    MINIMAL = "minimal"
    COLORFUL = "colorful"


@dataclass
class UIConfig:
    """UI配置"""
    theme: UITheme = UITheme.DEFAULT
    show_borders: bool = True
    show_timestamps: bool = False
    max_line_width: int = 80
    indent_size: int = 4


class TerminalUI:
    """终端用户界面类
    
    负责所有终端相关的用户界面显示和交互，与日志系统完全分离。
    """
    
    def __init__(self, config: Optional[UIConfig] = None):
        """初始化终端UI
        
        Args:
            config: UI配置，如果为None则使用默认配置
        """
        self.config = config or UIConfig()
        self._input_prompt = "您: "
        self._ai_prompt_template = "{name}: "
        
    def show_welcome(self, title: str = "ARC Spec AI", subtitle: str = "AI配置管理器"):
        """显示欢迎信息
        
        Args:
            title: 主标题
            subtitle: 副标题
        """
        print(f"\n{title} - {subtitle}")
        if self.config.show_borders:
            print("=" * self.config.max_line_width)
    
    def show_loading(self, message: str = "正在加载配置文件..."):
        """显示加载信息
        
        Args:
            message: 加载消息
        """
        print(message)
    
    def show_error(self, message: str, details: Optional[str] = None):
        """显示错误信息
        
        Args:
            message: 错误消息
            details: 错误详情
        """
        print(f"❌ 错误: {message}")
        if details and self.config.theme != UITheme.MINIMAL:
            print(f"   详情: {details}")
    
    def show_warning(self, message: str):
        """显示警告信息
        
        Args:
            message: 警告消息
        """
        print(f"⚠️  警告: {message}")
    
    def show_success(self, message: str):
        """显示成功信息
        
        Args:
            message: 成功消息
        """
        print(f"✅ {message}")
    
    def show_info(self, message: str):
        """显示信息
        
        Args:
            message: 信息内容
        """
        print(f"ℹ️  {message}")
    
    def show_config_list(self, configs: Dict[str, Dict[str, Any]]) -> List[str]:
        """显示配置文件列表
        
        Args:
            configs: 配置字典
            
        Returns:
            配置名称列表
        """
        if not configs:
            self.show_warning("没有找到任何配置文件")
            return []
        
        if self.config.show_borders:
            print("\n" + "=" * self.config.max_line_width)
            print("可用的AI配置文件:")
            print("=" * self.config.max_line_width)
        else:
            print("\n可用的AI配置文件:")
        
        # 表头
        header = f"{'序号':<4} {'FriendlyName':<20} {'Model':<25} {'Introduction':<30}"
        print(header)
        
        if self.config.show_borders:
            print("-" * self.config.max_line_width)
        
        config_names = list(configs.keys())
        for i, (config_name, config_data) in enumerate(configs.items(), 1):
            friendly_name = config_data.get('FriendlyName', config_name)
            model = config_data.get('Model', 'Unknown')
            introduction = config_data.get('Introduction', 'No description')
            
            # 截断过长的文本
            if len(friendly_name) > 18:
                friendly_name = friendly_name[:15] + "..."
            if len(model) > 23:
                model = model[:20] + "..."
            if len(introduction) > 28:
                introduction = introduction[:25] + "..."
                
            print(f"{i:<4} {friendly_name:<20} {model:<25} {introduction:<30}")
        
        if self.config.show_borders:
            print("=" * self.config.max_line_width)
        
        return config_names
    
    def show_model_info(self, model_info: Dict[str, Any]):
        """显示模型信息
        
        Args:
            model_info: 模型信息字典
        """
        if self.config.show_borders:
            print("\n=== 模型信息 ===")
        else:
            print("\n模型信息:")
        
        for key, value in model_info.items():
            if key != 'history_summary':  # 历史摘要单独处理
                print(f"  {key}: {value}")
        
        if self.config.show_borders:
            print("===============")
    
    def get_user_input(self, prompt: Optional[str] = None) -> str:
        """获取用户输入
        
        Args:
            prompt: 输入提示，如果为None则使用默认提示
            
        Returns:
            用户输入的字符串
        """
        display_prompt = prompt or self._input_prompt
        return input(f"\n{display_prompt}").strip()
    
    def get_user_choice(self, max_choice: int) -> int:
        """获取用户选择
        
        Args:
            max_choice: 最大选择数
            
        Returns:
            用户选择的数字，-1表示退出
        """
        while True:
            try:
                choice = input(f"请选择配置 (1-{max_choice}) 或输入 'q' 退出: ").strip()
                
                if choice.lower() == 'q':
                    return -1
                
                choice_num = int(choice)
                if 1 <= choice_num <= max_choice:
                    return choice_num
                else:
                    self.show_error(f"请输入 1-{max_choice} 之间的数字")
            
            except ValueError:
                self.show_error("请输入有效的数字")
            except KeyboardInterrupt:
                print("\n👋 再见!")
                return -1
    
    def get_choice_input(self, prompt: str = "请选择配置 (输入序号或 'quit' 退出): ") -> str:
        """获取选择输入
        
        Args:
            prompt: 选择提示
            
        Returns:
            用户选择
        """
        return input(f"\n{prompt}").strip()
    
    def show_chat_header(self, config_name: str):
        """显示对话界面头部信息
        
        Args:
            config_name: 配置名称
        """
        print(f"\n开始与 {config_name} 对话")
        print("输入 'quit' 或 'exit' 退出对话")
        print("输入 'clear' 或 '清空' 清空对话历史")
        print("输入 'info' 查看模型信息")
        
        if self.config.show_borders:
            print("-" * 50)
    
    def show_ai_thinking(self):
        """显示AI思考状态"""
        print("\nAI正在思考...")
    
    def show_ai_response(self, config_name: str, response: str, is_stream: bool = False):
        """显示AI回复
        
        Args:
            config_name: 配置名称
            response: AI回复内容
            is_stream: 是否为流式响应
        """
        ai_prompt = self._ai_prompt_template.format(name=config_name)
        
        if is_stream:
            print(f"\n{ai_prompt}", end="", flush=True)
            # 流式响应的内容会在其他地方处理
        else:
            print(f"\n{ai_prompt}{response}")
    
    def show_stream_start(self, config_name: str):
        """显示流式响应开始
        
        Args:
            config_name: 配置名称
        """
        ai_prompt = self._ai_prompt_template.format(name=config_name)
        print(f"\n{ai_prompt}", end="", flush=True)
    
    def show_stream_end(self):
        """显示流式响应结束"""
        print()  # 换行
    
    def show_history_cleared(self):
        """显示历史记录已清空"""
        self.show_success("对话历史已清空")
    
    def show_history_not_supported(self):
        """显示不支持清空历史记录"""
        self.show_warning("当前解析器不支持清空历史记录")
    
    def show_goodbye(self):
        """显示再见信息"""
        print("\n再见！")
    
    def show_interrupted(self):
        """显示中断信息"""
        print("\n\n对话被中断")
    
    def show_program_exit(self):
        """显示程序退出信息"""
        print("\n程序退出")
    
    def show_exit(self):
        """显示退出信息"""
        print("\n👋 程序退出")
    
    def show_help(self):
        """显示帮助信息"""
        print("\n📖 可用命令:")
        print("  quit/exit - 退出对话")
        print("  clear/清空 - 清空对话历史")
        print("  info - 查看模型信息")
        print("  help - 显示此帮助信息")
    
    def show_invalid_choice(self):
        """显示无效选择信息"""
        self.show_warning("无效的选择，请输入正确的序号")
    
    def show_invalid_number(self):
        """显示无效数字信息"""
        self.show_warning("请输入有效的数字")
    
    def clear_screen(self):
        """清屏"""
        import os
        os.system('cls' if os.name == 'nt' else 'clear')
    
    def set_input_prompt(self, prompt: str):
        """设置输入提示符
        
        Args:
            prompt: 新的输入提示符
        """
        self._input_prompt = prompt
    
    def set_ai_prompt_template(self, template: str):
        """设置AI提示符模板
        
        Args:
            template: 新的AI提示符模板，应包含{name}占位符
        """
        self._ai_prompt_template = template