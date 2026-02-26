#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
桌面GUI应用集成示例

这个示例展示了如何将ARC_Spec_Python集成到桌面GUI应用中，
使用tkinter创建用户友好的AI助手桌面应用。

特性:
- 现代化GUI界面
- 多配置支持
- 聊天历史记录
- 实时状态显示
- 设置管理
- 主题切换

依赖:
    pip install tkinter (通常Python自带)

运行方式:
    python desktop_gui_app.py
"""

import sys
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog
import threading
import queue
import json
import time
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime
import logging

# 添加ARC_Spec_Python到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# 导入ARC_Spec_Python模块
try:
    from arcspec_ai.configurator import load_ai_configs, load_parsers
except ImportError as e:
    print(f"导入ARC_Spec_Python失败: {e}")
    sys.exit(1)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class AIBackend:
    """AI后端处理类"""
    
    def __init__(self, project_root: str = None):
        if project_root is None:
            project_root = Path(__file__).parent.parent
        else:
            project_root = Path(project_root)
            
        self.project_root = project_root
        self.config_dir = project_root / 'configs'
        self.parsers_dir = project_root / 'parsers'
        
        self.configs = {}
        self.parser_registry = None
        self.parsers_cache = {}
        self.initialized = False
    
    def initialize(self) -> Dict[str, Any]:
        """
        初始化AI后端
        
        Returns:
            Dict[str, Any]: 初始化结果
        """
        try:
            logger.info("初始化AI后端...")
            
            # 加载配置
            self.configs = load_ai_configs(str(self.config_dir))
            if not self.configs:
                raise Exception("未找到任何配置文件")
            
            # 加载解析器
            self.parser_registry = load_parsers(str(self.parsers_dir))
            if not self.parser_registry:
                raise Exception("未找到任何解析器")
            
            # 预加载解析器
            self._preload_parsers()
            
            self.initialized = True
            
            logger.info(f"AI后端初始化成功，加载了 {len(self.configs)} 个配置")
            
            return {
                'success': True,
                'message': f'成功加载 {len(self.configs)} 个配置',
                'configs': list(self.configs.keys())
            }
            
        except Exception as e:
            logger.error(f"初始化失败: {e}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def _preload_parsers(self):
        """预加载所有解析器"""
        for config_name, config in self.configs.items():
            try:
                parser = self.parser_registry.create_parser(
                    config['ResponseType'], config
                )
                if parser:
                    self.parsers_cache[config_name] = parser
                    logger.debug(f"预加载解析器: {config_name}")
            except Exception as e:
                logger.error(f"预加载解析器 {config_name} 失败: {e}")
    
    def chat(self, message: str, config_name: str) -> Dict[str, Any]:
        """
        处理聊天消息
        
        Args:
            message: 用户消息
            config_name: 配置名称
            
        Returns:
            Dict[str, Any]: 处理结果
        """
        if not self.initialized:
            return {'success': False, 'error': 'AI后端未初始化'}
        
        if config_name not in self.configs:
            return {'success': False, 'error': f'配置 {config_name} 不存在'}
        
        start_time = time.time()
        
        try:
            parser = self.parsers_cache.get(config_name)
            if not parser:
                return {'success': False, 'error': f'解析器 {config_name} 不可用'}
            
            response = parser.parse(message)
            processing_time = time.time() - start_time
            
            return {
                'success': True,
                'response': response,
                'config_used': config_name,
                'processing_time': processing_time,
                'timestamp': datetime.now().isoformat()
            }
            
        except Exception as e:
            processing_time = time.time() - start_time
            logger.error(f"聊天处理失败: {e}")
            
            return {
                'success': False,
                'error': str(e),
                'processing_time': processing_time
            }
    
    def get_config_info(self, config_name: str) -> Dict[str, Any]:
        """获取配置信息"""
        if config_name not in self.configs:
            return {'error': f'配置 {config_name} 不存在'}
        
        config = self.configs[config_name]
        return {
            'name': config_name,
            'friendly_name': config.get('FriendlyName', config_name),
            'model': config.get('Model', 'Unknown'),
            'response_type': config.get('ResponseType', 'Unknown'),
            'description': config.get('Description', '无描述')
        }


class ChatMessage:
    """聊天消息类"""
    
    def __init__(self, content: str, is_user: bool, timestamp: datetime = None,
                 config_used: str = None, processing_time: float = None):
        self.content = content
        self.is_user = is_user
        self.timestamp = timestamp or datetime.now()
        self.config_used = config_used
        self.processing_time = processing_time
    
    def to_display_text(self) -> str:
        """转换为显示文本"""
        time_str = self.timestamp.strftime('%H:%M:%S')
        
        if self.is_user:
            return f"[{time_str}] 你: {self.content}"
        else:
            config_info = f" ({self.config_used})" if self.config_used else ""
            time_info = f" [{self.processing_time:.2f}s]" if self.processing_time else ""
            return f"[{time_str}] AI{config_info}: {self.content}{time_info}"


class AIDesktopApp:
    """AI桌面应用主类"""
    
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("ARC_Spec_Python AI助手")
        self.root.geometry("800x600")
        self.root.minsize(600, 400)
        
        # 设置图标（如果有的话）
        try:
            # self.root.iconbitmap('icon.ico')
            pass
        except:
            pass
        
        # AI后端
        self.ai_backend = AIBackend()
        
        # 应用状态
        self.current_config = tk.StringVar()
        self.status_text = tk.StringVar(value="未初始化")
        self.is_processing = tk.BooleanVar(value=False)
        
        # 聊天历史
        self.chat_history: List[ChatMessage] = []
        
        # 线程通信
        self.message_queue = queue.Queue()
        self.worker_thread = None
        
        # 创建界面
        self.create_widgets()
        self.create_menu()
        
        # 绑定事件
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        # 启动初始化
        self.initialize_ai()
    
    def create_menu(self):
        """创建菜单栏"""
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)
        
        # 文件菜单
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="文件", menu=file_menu)
        file_menu.add_command(label="导出聊天记录", command=self.export_chat_history)
        file_menu.add_command(label="清空聊天记录", command=self.clear_chat_history)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self.on_closing)
        
        # 设置菜单
        settings_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="设置", menu=settings_menu)
        settings_menu.add_command(label="重新初始化", command=self.reinitialize_ai)
        settings_menu.add_command(label="配置信息", command=self.show_config_info)
        
        # 帮助菜单
        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="帮助", menu=help_menu)
        help_menu.add_command(label="关于", command=self.show_about)
    
    def create_widgets(self):
        """创建界面组件"""
        # 主框架
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # 顶部控制栏
        control_frame = ttk.Frame(main_frame)
        control_frame.pack(fill=tk.X, pady=(0, 10))
        
        # 配置选择
        ttk.Label(control_frame, text="配置:").pack(side=tk.LEFT, padx=(0, 5))
        self.config_combo = ttk.Combobox(
            control_frame, 
            textvariable=self.current_config,
            state="readonly",
            width=20
        )
        self.config_combo.pack(side=tk.LEFT, padx=(0, 10))
        
        # 状态显示
        ttk.Label(control_frame, text="状态:").pack(side=tk.LEFT, padx=(0, 5))
        status_label = ttk.Label(
            control_frame, 
            textvariable=self.status_text,
            foreground="blue"
        )
        status_label.pack(side=tk.LEFT, padx=(0, 10))
        
        # 重新初始化按钮
        ttk.Button(
            control_frame,
            text="重新初始化",
            command=self.reinitialize_ai
        ).pack(side=tk.RIGHT)
        
        # 聊天显示区域
        chat_frame = ttk.LabelFrame(main_frame, text="聊天记录")
        chat_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        
        self.chat_display = scrolledtext.ScrolledText(
            chat_frame,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=('Consolas', 10),
            bg='#f8f9fa',
            fg='#212529'
        )
        self.chat_display.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # 输入区域
        input_frame = ttk.LabelFrame(main_frame, text="消息输入")
        input_frame.pack(fill=tk.X)
        
        # 输入框和按钮的容器
        input_container = ttk.Frame(input_frame)
        input_container.pack(fill=tk.X, padx=5, pady=5)
        
        # 消息输入框
        self.message_entry = tk.Text(
            input_container,
            height=3,
            wrap=tk.WORD,
            font=('Consolas', 10)
        )
        self.message_entry.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))
        
        # 按钮框架
        button_frame = ttk.Frame(input_container)
        button_frame.pack(side=tk.RIGHT, fill=tk.Y)
        
        # 发送按钮
        self.send_button = ttk.Button(
            button_frame,
            text="发送",
            command=self.send_message,
            width=8
        )
        self.send_button.pack(pady=(0, 5))
        
        # 清空按钮
        ttk.Button(
            button_frame,
            text="清空",
            command=self.clear_input,
            width=8
        ).pack()
        
        # 绑定回车键发送
        self.message_entry.bind('<Control-Return>', lambda e: self.send_message())
        
        # 进度条（初始隐藏）
        self.progress_frame = ttk.Frame(main_frame)
        self.progress_bar = ttk.Progressbar(
            self.progress_frame,
            mode='indeterminate'
        )
        self.progress_bar.pack(fill=tk.X, padx=5, pady=5)
    
    def initialize_ai(self):
        """初始化AI后端"""
        self.status_text.set("正在初始化...")
        self.show_progress(True)
        
        def init_worker():
            result = self.ai_backend.initialize()
            self.message_queue.put(('init_complete', result))
        
        self.worker_thread = threading.Thread(target=init_worker, daemon=True)
        self.worker_thread.start()
        
        # 启动消息处理
        self.process_queue()
    
    def reinitialize_ai(self):
        """重新初始化AI后端"""
        if self.is_processing.get():
            messagebox.showwarning("警告", "正在处理中，请稍后再试")
            return
        
        # 重置状态
        self.ai_backend = AIBackend()
        self.config_combo['values'] = []
        self.current_config.set('')
        
        # 重新初始化
        self.initialize_ai()
    
    def process_queue(self):
        """处理消息队列"""
        try:
            while True:
                message_type, data = self.message_queue.get_nowait()
                
                if message_type == 'init_complete':
                    self.on_init_complete(data)
                elif message_type == 'chat_complete':
                    self.on_chat_complete(data)
                    
        except queue.Empty:
            pass
        
        # 继续处理队列
        self.root.after(100, self.process_queue)
    
    def on_init_complete(self, result: Dict[str, Any]):
        """初始化完成处理"""
        self.show_progress(False)
        
        if result['success']:
            self.status_text.set("已就绪")
            
            # 更新配置列表
            configs = result['configs']
            self.config_combo['values'] = configs
            if configs:
                self.current_config.set(configs[0])
            
            # 启用界面
            self.send_button.config(state=tk.NORMAL)
            self.message_entry.config(state=tk.NORMAL)
            
            # 添加欢迎消息
            welcome_msg = ChatMessage(
                f"AI助手已就绪！加载了 {len(configs)} 个配置。",
                is_user=False
            )
            self.add_chat_message(welcome_msg)
            
        else:
            self.status_text.set("初始化失败")
            messagebox.showerror("错误", f"初始化失败: {result['error']}")
    
    def send_message(self):
        """发送消息"""
        if self.is_processing.get():
            return
        
        message = self.message_entry.get(1.0, tk.END).strip()
        if not message:
            return
        
        config_name = self.current_config.get()
        if not config_name:
            messagebox.showwarning("警告", "请选择配置")
            return
        
        # 添加用户消息到聊天记录
        user_msg = ChatMessage(message, is_user=True)
        self.add_chat_message(user_msg)
        
        # 清空输入框
        self.clear_input()
        
        # 设置处理状态
        self.is_processing.set(True)
        self.send_button.config(state=tk.DISABLED)
        self.status_text.set("正在处理...")
        self.show_progress(True)
        
        # 启动处理线程
        def chat_worker():
            result = self.ai_backend.chat(message, config_name)
            self.message_queue.put(('chat_complete', result))
        
        self.worker_thread = threading.Thread(target=chat_worker, daemon=True)
        self.worker_thread.start()
    
    def on_chat_complete(self, result: Dict[str, Any]):
        """聊天完成处理"""
        self.is_processing.set(False)
        self.send_button.config(state=tk.NORMAL)
        self.status_text.set("已就绪")
        self.show_progress(False)
        
        if result['success']:
            # 添加AI回复到聊天记录
            ai_msg = ChatMessage(
                result['response'],
                is_user=False,
                config_used=result['config_used'],
                processing_time=result['processing_time']
            )
            self.add_chat_message(ai_msg)
        else:
            # 添加错误消息
            error_msg = ChatMessage(
                f"错误: {result['error']}",
                is_user=False
            )
            self.add_chat_message(error_msg)
    
    def add_chat_message(self, message: ChatMessage):
        """添加聊天消息到显示区域"""
        self.chat_history.append(message)
        
        # 更新显示
        self.chat_display.config(state=tk.NORMAL)
        
        # 添加消息
        display_text = message.to_display_text()
        self.chat_display.insert(tk.END, display_text + "\n\n")
        
        # 设置颜色
        if message.is_user:
            # 用户消息使用蓝色
            start_line = self.chat_display.index(tk.END + "-2l linestart")
            end_line = self.chat_display.index(tk.END + "-2l lineend")
            self.chat_display.tag_add("user", start_line, end_line)
            self.chat_display.tag_config("user", foreground="#0066cc")
        else:
            # AI消息使用绿色
            start_line = self.chat_display.index(tk.END + "-2l linestart")
            end_line = self.chat_display.index(tk.END + "-2l lineend")
            self.chat_display.tag_add("ai", start_line, end_line)
            self.chat_display.tag_config("ai", foreground="#009900")
        
        # 滚动到底部
        self.chat_display.see(tk.END)
        self.chat_display.config(state=tk.DISABLED)
    
    def clear_input(self):
        """清空输入框"""
        self.message_entry.delete(1.0, tk.END)
    
    def clear_chat_history(self):
        """清空聊天记录"""
        if messagebox.askyesno("确认", "确定要清空聊天记录吗？"):
            self.chat_history.clear()
            self.chat_display.config(state=tk.NORMAL)
            self.chat_display.delete(1.0, tk.END)
            self.chat_display.config(state=tk.DISABLED)
    
    def export_chat_history(self):
        """导出聊天记录"""
        if not self.chat_history:
            messagebox.showinfo("提示", "没有聊天记录可导出")
            return
        
        filename = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("JSON文件", "*.json"), ("所有文件", "*.*")]
        )
        
        if filename:
            try:
                if filename.endswith('.json'):
                    # 导出为JSON格式
                    data = []
                    for msg in self.chat_history:
                        data.append({
                            'content': msg.content,
                            'is_user': msg.is_user,
                            'timestamp': msg.timestamp.isoformat(),
                            'config_used': msg.config_used,
                            'processing_time': msg.processing_time
                        })
                    
                    with open(filename, 'w', encoding='utf-8') as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                else:
                    # 导出为文本格式
                    with open(filename, 'w', encoding='utf-8') as f:
                        f.write(f"ARC_Spec_Python AI助手聊天记录\n")
                        f.write(f"导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                        f.write("=" * 50 + "\n\n")
                        
                        for msg in self.chat_history:
                            f.write(msg.to_display_text() + "\n\n")
                
                messagebox.showinfo("成功", f"聊天记录已导出到: {filename}")
                
            except Exception as e:
                messagebox.showerror("错误", f"导出失败: {e}")
    
    def show_config_info(self):
        """显示配置信息"""
        config_name = self.current_config.get()
        if not config_name:
            messagebox.showwarning("警告", "请选择配置")
            return
        
        config_info = self.ai_backend.get_config_info(config_name)
        if 'error' in config_info:
            messagebox.showerror("错误", config_info['error'])
            return
        
        info_text = f"""配置信息:

名称: {config_info['name']}
友好名称: {config_info['friendly_name']}
模型: {config_info['model']}
响应类型: {config_info['response_type']}
描述: {config_info['description']}"""
        
        messagebox.showinfo("配置信息", info_text)
    
    def show_about(self):
        """显示关于信息"""
        about_text = """ARC_Spec_Python AI助手

一个基于ARC_Spec_Python的桌面AI助手应用。

特性:
• 多配置支持
• 实时聊天
• 历史记录
• 导出功能

版本: 1.0.0
作者: ARC_Spec_Python Team"""
        
        messagebox.showinfo("关于", about_text)
    
    def show_progress(self, show: bool):
        """显示/隐藏进度条"""
        if show:
            self.progress_frame.pack(fill=tk.X, pady=(0, 5))
            self.progress_bar.start()
        else:
            self.progress_bar.stop()
            self.progress_frame.pack_forget()
    
    def on_closing(self):
        """关闭应用"""
        if self.is_processing.get():
            if not messagebox.askyesno("确认", "正在处理中，确定要退出吗？"):
                return
        
        self.root.quit()
        self.root.destroy()
    
    def run(self):
        """运行应用"""
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self.on_closing()


def main():
    """主函数"""
    try:
        app = AIDesktopApp()
        app.run()
    except Exception as e:
        logger.error(f"应用启动失败: {e}")
        messagebox.showerror("错误", f"应用启动失败: {e}")


if __name__ == '__main__':
    main()