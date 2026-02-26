#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
命令行工具集成示例

这个示例展示了如何将ARC_Spec_Python集成到命令行工具中，
创建功能丰富的AI助手命令行应用。

特性:
- 完整的CLI界面
- 配置管理
- 批处理模式
- 交互模式
- 输出格式化
- 日志记录

运行方式:
    python cli_tool_integration.py --help
    python cli_tool_integration.py chat "你好"
    python cli_tool_integration.py interactive
    python cli_tool_integration.py batch input.txt
"""

import sys
import argparse
import logging
import json
import time
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime
import textwrap

# 添加ARC_Spec_Python到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# 导入ARC_Spec_Python模块
try:
    from arcspec_ai.configurator import load_ai_configs, load_parsers
except ImportError as e:
    print(f"导入ARC_Spec_Python失败: {e}")
    sys.exit(1)


class AICliTool:
    """AI命令行工具"""
    
    def __init__(self, project_root: str = None, verbose: bool = False):
        if project_root is None:
            project_root = Path(__file__).parent.parent
        else:
            project_root = Path(project_root)
            
        self.project_root = project_root
        self.config_dir = project_root / 'configs'
        self.parsers_dir = project_root / 'arcspec_ai' / 'parsers'
        
        # 设置日志
        log_level = logging.DEBUG if verbose else logging.INFO
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)
        
        # AI组件
        self.configs = {}
        self.parser_registry = None
        self.current_config = None
        self.initialized = False
    
    def initialize(self) -> bool:
        """
        初始化AI工具
        
        Returns:
            bool: 是否初始化成功
        """
        try:
            self.logger.info("初始化AI命令行工具...")
            
            # 加载配置
            self.configs = load_ai_configs(str(self.config_dir))
            if not self.configs:
                self.logger.error("未找到任何配置文件")
                return False
            
            # 加载解析器
            self.parser_registry = load_parsers(str(self.parsers_dir))
            if not self.parser_registry:
                self.logger.error("未找到任何解析器")
                return False
            
            # 设置默认配置
            self.current_config = list(self.configs.keys())[0]
            
            self.initialized = True
            self.logger.info(
                f"初始化成功，加载了 {len(self.configs)} 个配置"
            )
            
            return True
            
        except Exception as e:
            self.logger.error(f"初始化失败: {e}")
            return False
    
    def list_configs(self) -> Dict[str, Any]:
        """
        列出所有可用配置
        
        Returns:
            Dict[str, Any]: 配置信息
        """
        if not self.initialized:
            return {'error': '工具未初始化'}
        
        configs_info = {}
        for name, config in self.configs.items():
            configs_info[name] = {
                'friendly_name': config.get('FriendlyName', name),
                'model': config.get('Model', 'Unknown'),
                'response_type': config.get('ResponseType', 'Unknown'),
                'current': name == self.current_config
            }
        
        return {
            'total': len(self.configs),
            'current': self.current_config,
            'configs': configs_info
        }
    
    def set_config(self, config_name: str) -> Dict[str, Any]:
        """
        设置当前配置
        
        Args:
            config_name: 配置名称
            
        Returns:
            Dict[str, Any]: 操作结果
        """
        if not self.initialized:
            return {'success': False, 'error': '工具未初始化'}
        
        if config_name not in self.configs:
            return {
                'success': False,
                'error': f'配置 {config_name} 不存在',
                'available': list(self.configs.keys())
            }
        
        self.current_config = config_name
        config = self.configs[config_name]
        
        return {
            'success': True,
            'message': f'已切换到配置: {config.get("FriendlyName", config_name)}',
            'config': {
                'name': config_name,
                'friendly_name': config.get('FriendlyName', config_name),
                'model': config.get('Model', 'Unknown')
            }
        }
    
    def chat(self, message: str, config_name: Optional[str] = None) -> Dict[str, Any]:
        """
        发送聊天消息
        
        Args:
            message: 用户消息
            config_name: 指定配置（可选）
            
        Returns:
            Dict[str, Any]: 聊天结果
        """
        if not self.initialized:
            return {'success': False, 'error': '工具未初始化'}
        
        # 确定使用的配置
        use_config = config_name or self.current_config
        if use_config not in self.configs:
            return {
                'success': False,
                'error': f'配置 {use_config} 不存在'
            }
        
        start_time = time.time()
        
        try:
            # 获取配置和解析器
            config = self.configs[use_config]
            parser = self.parser_registry.create_parser(
                config['ResponseType'], config
            )
            
            if not parser:
                return {
                    'success': False,
                    'error': f'无法创建解析器: {config["ResponseType"]}'
                }
            
            # 执行解析
            response = parser.parse(message)
            processing_time = time.time() - start_time
            
            self.logger.info(
                f"消息处理成功，配置: {use_config}, 耗时: {processing_time:.2f}s"
            )
            
            return {
                'success': True,
                'response': response,
                'config_used': use_config,
                'config_friendly_name': config.get('FriendlyName', use_config),
                'processing_time': processing_time,
                'timestamp': datetime.now().isoformat()
            }
            
        except Exception as e:
            processing_time = time.time() - start_time
            self.logger.error(f"聊天处理失败: {e}")
            
            return {
                'success': False,
                'error': str(e),
                'config_used': use_config,
                'processing_time': processing_time
            }
    
    def batch_process(self, input_file: str, output_file: Optional[str] = None,
                     config_name: Optional[str] = None) -> Dict[str, Any]:
        """
        批处理文件
        
        Args:
            input_file: 输入文件路径
            output_file: 输出文件路径（可选）
            config_name: 指定配置（可选）
            
        Returns:
            Dict[str, Any]: 处理结果
        """
        if not self.initialized:
            return {'success': False, 'error': '工具未初始化'}
        
        input_path = Path(input_file)
        if not input_path.exists():
            return {'success': False, 'error': f'输入文件不存在: {input_file}'}
        
        try:
            # 读取输入文件
            with open(input_path, 'r', encoding='utf-8') as f:
                lines = [line.strip() for line in f if line.strip()]
            
            if not lines:
                return {'success': False, 'error': '输入文件为空'}
            
            self.logger.info(f"开始批处理 {len(lines)} 条消息...")
            
            results = []
            successful = 0
            failed = 0
            total_time = 0
            
            for i, message in enumerate(lines, 1):
                self.logger.info(f"处理第 {i}/{len(lines)} 条消息")
                
                result = self.chat(message, config_name)
                results.append({
                    'index': i,
                    'input': message,
                    'result': result
                })
                
                if result['success']:
                    successful += 1
                    total_time += result['processing_time']
                else:
                    failed += 1
                
                # 简单的进度显示
                if i % 10 == 0 or i == len(lines):
                    print(f"进度: {i}/{len(lines)} ({i/len(lines)*100:.1f}%)")
            
            # 保存结果
            if output_file:
                output_path = Path(output_file)
                with open(output_path, 'w', encoding='utf-8') as f:
                    json.dump(results, f, ensure_ascii=False, indent=2)
                self.logger.info(f"结果已保存到: {output_file}")
            
            summary = {
                'success': True,
                'total_messages': len(lines),
                'successful': successful,
                'failed': failed,
                'success_rate': successful / len(lines) * 100,
                'total_processing_time': total_time,
                'average_processing_time': total_time / max(successful, 1),
                'output_file': output_file,
                'results': results if not output_file else None
            }
            
            self.logger.info(
                f"批处理完成: {successful}/{len(lines)} 成功, "
                f"成功率: {summary['success_rate']:.1f}%"
            )
            
            return summary
            
        except Exception as e:
            self.logger.error(f"批处理失败: {e}")
            return {'success': False, 'error': str(e)}
    
    def interactive_mode(self):
        """
        交互模式
        """
        if not self.initialized:
            print("❌ 工具未初始化")
            return
        
        print("\n🤖 AI命令行工具 - 交互模式")
        print("="*50)
        print("命令:")
        print("  /help     - 显示帮助")
        print("  /configs  - 列出配置")
        print("  /use <配置名> - 切换配置")
        print("  /status   - 显示状态")
        print("  /quit     - 退出")
        print("  其他输入  - 发送给AI")
        print("="*50)
        
        # 显示当前配置
        config = self.configs[self.current_config]
        print(f"\n当前配置: {config.get('FriendlyName', self.current_config)}")
        print(f"模型: {config.get('Model', 'Unknown')}")
        
        message_count = 0
        total_time = 0
        
        try:
            while True:
                try:
                    user_input = input("\n你: ").strip()
                    
                    if not user_input:
                        continue
                    
                    # 处理命令
                    if user_input.startswith('/'):
                        command_parts = user_input[1:].split()
                        command = command_parts[0].lower()
                        
                        if command == 'help':
                            print("\n📖 可用命令:")
                            print("  /help     - 显示此帮助")
                            print("  /configs  - 列出所有配置")
                            print("  /use <配置名> - 切换到指定配置")
                            print("  /status   - 显示当前状态")
                            print("  /quit     - 退出交互模式")
                            
                        elif command == 'configs':
                            configs_info = self.list_configs()
                            print("\n📋 可用配置:")
                            for name, info in configs_info['configs'].items():
                                marker = "👉" if info['current'] else "  "
                                print(f"{marker} {name}: {info['friendly_name']} ({info['model']})")
                                
                        elif command == 'use':
                            if len(command_parts) < 2:
                                print("❌ 请指定配置名称: /use <配置名>")
                            else:
                                config_name = command_parts[1]
                                result = self.set_config(config_name)
                                if result['success']:
                                    print(f"✅ {result['message']}")
                                else:
                                    print(f"❌ {result['error']}")
                                    if 'available' in result:
                                        print(f"可用配置: {', '.join(result['available'])}")
                                        
                        elif command == 'status':
                            config = self.configs[self.current_config]
                            print(f"\n📊 当前状态:")
                            print(f"  配置: {config.get('FriendlyName', self.current_config)}")
                            print(f"  模型: {config.get('Model', 'Unknown')}")
                            print(f"  消息数: {message_count}")
                            if message_count > 0:
                                print(f"  平均处理时间: {total_time/message_count:.2f}秒")
                                
                        elif command == 'quit':
                            break
                            
                        else:
                            print(f"❌ 未知命令: {command}")
                            print("输入 /help 查看可用命令")
                        
                        continue
                    
                    # 发送消息给AI
                    result = self.chat(user_input)
                    
                    if result['success']:
                        # 格式化输出
                        response = result['response']
                        config_name = result['config_friendly_name']
                        processing_time = result['processing_time']
                        
                        print(f"\n🤖 {config_name}:")
                        
                        # 如果回复很长，进行格式化
                        if len(response) > 100:
                            wrapped = textwrap.fill(response, width=70)
                            print(wrapped)
                        else:
                            print(response)
                        
                        print(f"\n⏱️  处理时间: {processing_time:.2f}秒")
                        
                        # 更新统计
                        message_count += 1
                        total_time += processing_time
                        
                    else:
                        print(f"\n❌ 错误: {result['error']}")
                        
                except KeyboardInterrupt:
                    print("\n\n👋 用户中断")
                    break
                except Exception as e:
                    print(f"\n❌ 发生错误: {e}")
        
        finally:
            print(f"\n📊 会话统计:")
            print(f"  总消息数: {message_count}")
            if message_count > 0:
                print(f"  总处理时间: {total_time:.2f}秒")
                print(f"  平均处理时间: {total_time/message_count:.2f}秒")
            print("\n👋 交互模式结束")


def create_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器"""
    parser = argparse.ArgumentParser(
        description='ARC_Spec_Python AI命令行工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        示例:
          %(prog)s chat "你好，请介绍一下你自己"
          %(prog)s chat "写个Python函数" --config mycostom
          %(prog)s interactive
          %(prog)s batch input.txt --output results.json
          %(prog)s configs
        """)
    )
    
    parser.add_argument(
        '--project-root', '-p',
        help='项目根目录路径',
        default=None
    )
    
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='详细输出'
    )
    
    subparsers = parser.add_subparsers(
        dest='command',
        help='可用命令',
        metavar='COMMAND'
    )
    
    # chat命令
    chat_parser = subparsers.add_parser(
        'chat',
        help='发送单条消息'
    )
    chat_parser.add_argument(
        'message',
        help='要发送的消息'
    )
    chat_parser.add_argument(
        '--config', '-c',
        help='指定配置名称'
    )
    chat_parser.add_argument(
        '--format', '-f',
        choices=['text', 'json'],
        default='text',
        help='输出格式'
    )
    
    # interactive命令
    subparsers.add_parser(
        'interactive',
        help='启动交互模式'
    )
    
    # batch命令
    batch_parser = subparsers.add_parser(
        'batch',
        help='批处理文件'
    )
    batch_parser.add_argument(
        'input_file',
        help='输入文件路径（每行一条消息）'
    )
    batch_parser.add_argument(
        '--output', '-o',
        help='输出文件路径（JSON格式）'
    )
    batch_parser.add_argument(
        '--config', '-c',
        help='指定配置名称'
    )
    
    # configs命令
    configs_parser = subparsers.add_parser(
        'configs',
        help='列出所有配置'
    )
    configs_parser.add_argument(
        '--format', '-f',
        choices=['table', 'json'],
        default='table',
        help='输出格式'
    )
    
    return parser


def format_configs_table(configs_info: Dict[str, Any]) -> str:
    """格式化配置表格"""
    lines = []
    lines.append("📋 可用配置:")
    lines.append("=" * 60)
    lines.append(f"{'配置名':<15} {'友好名称':<20} {'模型':<15} {'当前':<5}")
    lines.append("-" * 60)
    
    for name, info in configs_info['configs'].items():
        current = "✓" if info['current'] else ""
        lines.append(
            f"{name:<15} {info['friendly_name']:<20} "
            f"{info['model']:<15} {current:<5}"
        )
    
    lines.append("-" * 60)
    lines.append(f"总计: {configs_info['total']} 个配置")
    lines.append(f"当前: {configs_info['current']}")
    
    return "\n".join(lines)


def main():
    """主函数"""
    parser = create_parser()
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    # 创建AI工具
    tool = AICliTool(
        project_root=args.project_root,
        verbose=args.verbose
    )
    
    # 初始化
    if not tool.initialize():
        print("❌ 初始化失败")
        sys.exit(1)
    
    try:
        if args.command == 'chat':
            # 单条消息
            result = tool.chat(args.message, args.config)
            
            if args.format == 'json':
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                if result['success']:
                    print(f"\n🤖 {result['config_friendly_name']}:")
                    print(result['response'])
                    print(f"\n⏱️  处理时间: {result['processing_time']:.2f}秒")
                else:
                    print(f"❌ 错误: {result['error']}")
        
        elif args.command == 'interactive':
            # 交互模式
            tool.interactive_mode()
        
        elif args.command == 'batch':
            # 批处理
            result = tool.batch_process(
                args.input_file,
                args.output,
                args.config
            )
            
            if result['success']:
                print(f"\n✅ 批处理完成:")
                print(f"  总消息数: {result['total_messages']}")
                print(f"  成功: {result['successful']}")
                print(f"  失败: {result['failed']}")
                print(f"  成功率: {result['success_rate']:.1f}%")
                print(f"  总处理时间: {result['total_processing_time']:.2f}秒")
                print(f"  平均处理时间: {result['average_processing_time']:.2f}秒")
                
                if result['output_file']:
                    print(f"  结果文件: {result['output_file']}")
            else:
                print(f"❌ 批处理失败: {result['error']}")
        
        elif args.command == 'configs':
            # 列出配置
            configs_info = tool.list_configs()
            
            if 'error' in configs_info:
                print(f"❌ {configs_info['error']}")
            else:
                if args.format == 'json':
                    print(json.dumps(configs_info, ensure_ascii=False, indent=2))
                else:
                    print(format_configs_table(configs_info))
    
    except KeyboardInterrupt:
        print("\n\n👋 用户中断")
    except Exception as e:
        print(f"\n❌ 发生错误: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()