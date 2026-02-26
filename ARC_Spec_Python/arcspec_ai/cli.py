"""命令行界面模块

提供命令行交互功能，使用分离的UI和对话逻辑。"""

import logging
from typing import Dict, Any, Optional
from .config import get_parser_by_config, list_available_parsers, get_parser_info
from .parsers import BaseParser
from .ui import TerminalUI, ChatInterface, UITheme

logger = logging.getLogger(__name__)


class CLI:
    """命令行界面类
    
    协调UI显示和对话逻辑，不直接处理用户界面。
    """
    
    def __init__(self, theme: UITheme = UITheme.DEFAULT):
        """初始化CLI
        
        Args:
            theme: UI主题
        """
        self.ui = TerminalUI()
        self.ui.config.theme = theme  # 设置主题
        self.chat_interface = ChatInterface(self.ui)
        self.parsers: Dict[str, BaseParser] = {}
        self.current_parser: Optional[BaseParser] = None
        self.current_config_name: Optional[str] = None
    
    def run(self):
        """运行CLI主程序"""
        self.ui.show_welcome()
        logger.info("AI配置管理器启动")
        
        # 加载配置文件
        self.ui.show_loading("正在加载配置文件")
        
        try:
            # 获取可用的解析器列表
            available_parsers = list_available_parsers()
            
            if not available_parsers:
                self.ui.show_error("未找到任何解析器")
                logger.error("未找到任何解析器")
                return
            
            # 加载配置文件信息
            from .utils import load_ai_configs
            configs = load_ai_configs()
            
            if not configs:
                self.ui.show_error("未找到任何配置文件")
                logger.error("未找到任何配置文件")
                return
            
            self.ui.show_success(f"成功加载 {len(configs)} 个配置文件")
            logger.info(f"成功加载 {len(configs)} 个配置文件")
            
            while True:
                self.ui.show_config_list(configs)
                
                choice = self.ui.get_user_choice(len(configs))
                
                if choice == -1:
                    self.ui.show_goodbye()
                    logger.info("用户退出程序")
                    break
                
                # 获取选中的配置
                config_names = list(configs.keys())
                selected_config_name = config_names[choice - 1]
                selected_config = configs[selected_config_name]
                
                self.ui.show_loading(f"正在初始化 {selected_config_name}")
                logger.info(f"开始初始化配置: {selected_config_name}")
                
                try:
                    parser = get_parser_by_config(selected_config)
                    self.ui.show_success(f"成功初始化 {selected_config_name}")
                    logger.info(f"成功初始化解析器: {selected_config_name}")
                    
                    # 显示模型信息
                    try:
                        model_info = parser.get_model_info()
                        self.ui.show_model_info(model_info)
                        logger.debug(f"显示模型信息: {model_info}")
                    except Exception as e:
                        warning_msg = f"无法获取模型信息: {e}"
                        self.ui.show_warning(warning_msg)
                        logger.warning(warning_msg)
                    
                    # 开始对话
                    self.chat_interface.start_chat(parser, selected_config_name, selected_config)
                    
                except Exception as e:
                    error_msg = f"初始化解析器失败: {e}"
                    self.ui.show_error(error_msg)
                    logger.error(error_msg, exc_info=True)
        
        except Exception as e:
            error_msg = f"程序运行出错: {e}"
            self.ui.show_error(error_msg)
            logger.error(error_msg, exc_info=True)


def main():
    """主函数"""
    # 配置日志 - 只输出到文件，不输出到控制台
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('arcspec_ai.log', encoding='utf-8')
        ]
    )
    
    # 为开发调试添加控制台日志（可选）
    debug_mode = False  # 可以通过环境变量或命令行参数控制
    if debug_mode:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(console_handler)
    
    cli = CLI()
    cli.run()


if __name__ == "__main__":
    main()