#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Flask Web应用集成示例

这个示例展示了如何将ARC_Spec_Python集成到Flask Web应用中，
创建一个简单的Web聊天界面。

安装依赖:
    pip install flask flask-cors

运行方式:
    python flask_web_app.py
    然后访问 http://localhost:5000
"""

import sys
import os
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime

# Flask相关导入
try:
    from flask import Flask, request, jsonify, render_template_string
    from flask_cors import CORS
except ImportError:
    print("请安装Flask依赖: pip install flask flask-cors")
    sys.exit(1)

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

# 创建Flask应用
app = Flask(__name__)
CORS(app)  # 允许跨域请求

# 全局AI客户端
ai_client = None


class FlaskAIClient:
    """Flask应用的AI客户端"""
    
    def __init__(self, project_root: str = None):
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
        self.chat_history = []
        
    def initialize(self) -> Dict[str, Any]:
        """
        初始化AI客户端
        
        Returns:
            Dict[str, Any]: 初始化结果
        """
        try:
            # 加载配置
            self.configs = load_ai_configs(str(self.config_dir))
            if not self.configs:
                return {
                    'success': False,
                    'error': '未找到任何配置文件',
                    'configs': []
                }
            
            # 加载解析器
            self.parser_registry = load_parsers(str(self.parsers_dir))
            
            # 设置默认配置
            default_config = list(self.configs.keys())[0]
            self.set_config(default_config)
            
            return {
                'success': True,
                'message': f'成功加载 {len(self.configs)} 个配置',
                'configs': self.get_config_list(),
                'current_config': default_config
            }
            
        except Exception as e:
            logger.error(f"初始化失败: {e}")
            return {
                'success': False,
                'error': str(e),
                'configs': []
            }
    
    def get_config_list(self) -> list:
        """获取配置列表"""
        return [
            {
                'name': name,
                'friendly_name': config.get('FriendlyName', name),
                'model': config.get('Model', 'Unknown'),
                'response_type': config.get('ResponseType', 'Unknown')
            }
            for name, config in self.configs.items()
        ]
    
    def set_config(self, config_name: str) -> Dict[str, Any]:
        """
        设置当前配置
        
        Args:
            config_name: 配置名称
            
        Returns:
            Dict[str, Any]: 设置结果
        """
        if config_name not in self.configs:
            return {
                'success': False,
                'error': f'配置 {config_name} 不存在'
            }
        
        try:
            config = self.configs[config_name]
            parser = self.parser_registry.create_parser(
                config['ResponseType'], config
            )
            
            if parser:
                self.current_parser = parser
                self.current_config_name = config_name
                # 清空聊天历史
                self.chat_history = []
                
                return {
                    'success': True,
                    'message': f'成功切换到配置: {config.get("FriendlyName", config_name)}',
                    'config': {
                        'name': config_name,
                        'friendly_name': config.get('FriendlyName', config_name),
                        'model': config.get('Model', 'Unknown')
                    }
                }
            else:
                return {
                    'success': False,
                    'error': f'无法创建解析器: {config["ResponseType"]}'
                }
                
        except Exception as e:
            logger.error(f"设置配置失败: {e}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def chat(self, message: str) -> Dict[str, Any]:
        """
        发送消息并获取回复
        
        Args:
            message: 用户消息
            
        Returns:
            Dict[str, Any]: 聊天结果
        """
        if not self.current_parser:
            return {
                'success': False,
                'error': '请先设置配置'
            }
        
        if not message.strip():
            return {
                'success': False,
                'error': '消息不能为空'
            }
        
        try:
            # 记录用户消息
            user_entry = {
                'type': 'user',
                'message': message,
                'timestamp': datetime.now().isoformat()
            }
            self.chat_history.append(user_entry)
            
            # 获取AI回复
            response = self.current_parser.parse(message)
            
            # 记录AI回复
            ai_entry = {
                'type': 'ai',
                'message': response,
                'timestamp': datetime.now().isoformat(),
                'config': self.current_config_name
            }
            self.chat_history.append(ai_entry)
            
            return {
                'success': True,
                'response': response,
                'history_length': len(self.chat_history)
            }
            
        except Exception as e:
            logger.error(f"对话失败: {e}")
            error_entry = {
                'type': 'error',
                'message': f'对话失败: {str(e)}',
                'timestamp': datetime.now().isoformat()
            }
            self.chat_history.append(error_entry)
            
            return {
                'success': False,
                'error': str(e)
            }
    
    def get_chat_history(self) -> list:
        """获取聊天历史"""
        return self.chat_history
    
    def clear_history(self) -> Dict[str, Any]:
        """清空聊天历史"""
        self.chat_history = []
        return {
            'success': True,
            'message': '聊天历史已清空'
        }
    
    def get_status(self) -> Dict[str, Any]:
        """获取当前状态"""
        if not self.current_parser or not self.current_config_name:
            return {
                'initialized': False,
                'current_config': None
            }
        
        config = self.configs[self.current_config_name]
        return {
            'initialized': True,
            'current_config': {
                'name': self.current_config_name,
                'friendly_name': config.get('FriendlyName', self.current_config_name),
                'model': config.get('Model', 'Unknown'),
                'response_type': config.get('ResponseType', 'Unknown')
            },
            'total_configs': len(self.configs),
            'history_length': len(self.chat_history)
        }


# Web界面HTML模板
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ARC_Spec_Python Web Chat</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
        }
        
        .container {
            width: 90%;
            max-width: 800px;
            background: white;
            border-radius: 15px;
            box-shadow: 0 20px 40px rgba(0,0,0,0.1);
            overflow: hidden;
        }
        
        .header {
            background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
            color: white;
            padding: 20px;
            text-align: center;
        }
        
        .config-selector {
            padding: 15px;
            background: #f8f9fa;
            border-bottom: 1px solid #dee2e6;
        }
        
        .config-selector select {
            width: 100%;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 5px;
            font-size: 14px;
        }
        
        .chat-container {
            height: 400px;
            overflow-y: auto;
            padding: 20px;
            background: #f8f9fa;
        }
        
        .message {
            margin-bottom: 15px;
            display: flex;
            align-items: flex-start;
        }
        
        .message.user {
            justify-content: flex-end;
        }
        
        .message-content {
            max-width: 70%;
            padding: 12px 16px;
            border-radius: 18px;
            word-wrap: break-word;
        }
        
        .message.user .message-content {
            background: #007bff;
            color: white;
        }
        
        .message.ai .message-content {
            background: white;
            border: 1px solid #dee2e6;
            color: #333;
        }
        
        .message.error .message-content {
            background: #f8d7da;
            border: 1px solid #f5c6cb;
            color: #721c24;
        }
        
        .input-container {
            padding: 20px;
            background: white;
            border-top: 1px solid #dee2e6;
        }
        
        .input-group {
            display: flex;
            gap: 10px;
        }
        
        .input-group input {
            flex: 1;
            padding: 12px;
            border: 1px solid #ddd;
            border-radius: 25px;
            font-size: 14px;
            outline: none;
        }
        
        .input-group input:focus {
            border-color: #007bff;
        }
        
        .btn {
            padding: 12px 20px;
            border: none;
            border-radius: 25px;
            cursor: pointer;
            font-size: 14px;
            transition: all 0.3s;
        }
        
        .btn-primary {
            background: #007bff;
            color: white;
        }
        
        .btn-primary:hover {
            background: #0056b3;
        }
        
        .btn-secondary {
            background: #6c757d;
            color: white;
            margin-left: 10px;
        }
        
        .btn-secondary:hover {
            background: #545b62;
        }
        
        .status {
            padding: 10px 20px;
            background: #e9ecef;
            font-size: 12px;
            color: #6c757d;
        }
        
        .loading {
            opacity: 0.6;
            pointer-events: none;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🤖 ARC_Spec_Python Web Chat</h1>
            <p>基于ARC_Spec_Python的Web聊天界面</p>
        </div>
        
        <div class="config-selector">
            <select id="configSelect">
                <option value="">选择AI配置...</option>
            </select>
        </div>
        
        <div class="chat-container" id="chatContainer">
            <div class="message ai">
                <div class="message-content">
                    👋 欢迎使用ARC_Spec_Python Web Chat！<br>
                    请先选择一个AI配置，然后开始对话。
                </div>
            </div>
        </div>
        
        <div class="input-container">
            <div class="input-group">
                <input type="text" id="messageInput" placeholder="输入您的消息..." disabled>
                <button class="btn btn-primary" id="sendBtn" disabled>发送</button>
                <button class="btn btn-secondary" id="clearBtn">清空</button>
            </div>
        </div>
        
        <div class="status" id="status">
            正在初始化...
        </div>
    </div>

    <script>
        class WebChatClient {
            constructor() {
                this.configSelect = document.getElementById('configSelect');
                this.chatContainer = document.getElementById('chatContainer');
                this.messageInput = document.getElementById('messageInput');
                this.sendBtn = document.getElementById('sendBtn');
                this.clearBtn = document.getElementById('clearBtn');
                this.status = document.getElementById('status');
                
                this.init();
            }
            
            async init() {
                try {
                    const response = await fetch('/api/init', { method: 'POST' });
                    const data = await response.json();
                    
                    if (data.success) {
                        this.loadConfigs(data.configs);
                        this.updateStatus(`✅ 初始化成功，加载了 ${data.configs.length} 个配置`);
                    } else {
                        this.updateStatus(`❌ 初始化失败: ${data.error}`);
                    }
                } catch (error) {
                    this.updateStatus(`❌ 连接失败: ${error.message}`);
                }
                
                this.bindEvents();
            }
            
            loadConfigs(configs) {
                this.configSelect.innerHTML = '<option value="">选择AI配置...</option>';
                
                configs.forEach(config => {
                    const option = document.createElement('option');
                    option.value = config.name;
                    option.textContent = `${config.friendly_name} (${config.model})`;
                    this.configSelect.appendChild(option);
                });
            }
            
            bindEvents() {
                this.configSelect.addEventListener('change', (e) => {
                    if (e.target.value) {
                        this.setConfig(e.target.value);
                    }
                });
                
                this.sendBtn.addEventListener('click', () => this.sendMessage());
                this.clearBtn.addEventListener('click', () => this.clearHistory());
                
                this.messageInput.addEventListener('keypress', (e) => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault();
                        this.sendMessage();
                    }
                });
            }
            
            async setConfig(configName) {
                try {
                    this.setLoading(true);
                    const response = await fetch('/api/config', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ config_name: configName })
                    });
                    
                    const data = await response.json();
                    
                    if (data.success) {
                        this.messageInput.disabled = false;
                        this.sendBtn.disabled = false;
                        this.updateStatus(`✅ 当前配置: ${data.config.friendly_name}`);
                        this.clearChatContainer();
                        this.addMessage('ai', `🎉 已切换到配置: ${data.config.friendly_name}\n现在可以开始对话了！`);
                    } else {
                        this.updateStatus(`❌ 配置设置失败: ${data.error}`);
                    }
                } catch (error) {
                    this.updateStatus(`❌ 请求失败: ${error.message}`);
                } finally {
                    this.setLoading(false);
                }
            }
            
            async sendMessage() {
                const message = this.messageInput.value.trim();
                if (!message) return;
                
                this.addMessage('user', message);
                this.messageInput.value = '';
                this.setLoading(true);
                
                try {
                    const response = await fetch('/api/chat', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ message: message })
                    });
                    
                    const data = await response.json();
                    
                    if (data.success) {
                        this.addMessage('ai', data.response);
                    } else {
                        this.addMessage('error', `错误: ${data.error}`);
                    }
                } catch (error) {
                    this.addMessage('error', `请求失败: ${error.message}`);
                } finally {
                    this.setLoading(false);
                }
            }
            
            async clearHistory() {
                try {
                    const response = await fetch('/api/clear', { method: 'POST' });
                    const data = await response.json();
                    
                    if (data.success) {
                        this.clearChatContainer();
                        this.addMessage('ai', '✨ 聊天历史已清空');
                    }
                } catch (error) {
                    console.error('清空历史失败:', error);
                }
            }
            
            addMessage(type, content) {
                const messageDiv = document.createElement('div');
                messageDiv.className = `message ${type}`;
                
                const contentDiv = document.createElement('div');
                contentDiv.className = 'message-content';
                contentDiv.textContent = content;
                
                messageDiv.appendChild(contentDiv);
                this.chatContainer.appendChild(messageDiv);
                
                this.chatContainer.scrollTop = this.chatContainer.scrollHeight;
            }
            
            clearChatContainer() {
                this.chatContainer.innerHTML = '';
            }
            
            setLoading(loading) {
                if (loading) {
                    document.body.classList.add('loading');
                } else {
                    document.body.classList.remove('loading');
                }
            }
            
            updateStatus(message) {
                this.status.textContent = message;
            }
        }
        
        // 启动应用
        document.addEventListener('DOMContentLoaded', () => {
            new WebChatClient();
        });
    </script>
</body>
</html>
"""


# API路由
@app.route('/')
def index():
    """主页"""
    return render_template_string(HTML_TEMPLATE)


@app.route('/api/init', methods=['POST'])
def api_init():
    """初始化API"""
    global ai_client
    
    try:
        ai_client = FlaskAIClient()
        result = ai_client.initialize()
        return jsonify(result)
    except Exception as e:
        logger.error(f"初始化API失败: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/config', methods=['POST'])
def api_set_config():
    """设置配置API"""
    global ai_client
    
    if not ai_client:
        return jsonify({
            'success': False,
            'error': '客户端未初始化'
        }), 400
    
    data = request.get_json()
    config_name = data.get('config_name')
    
    if not config_name:
        return jsonify({
            'success': False,
            'error': '配置名称不能为空'
        }), 400
    
    result = ai_client.set_config(config_name)
    return jsonify(result)


@app.route('/api/chat', methods=['POST'])
def api_chat():
    """聊天API"""
    global ai_client
    
    if not ai_client:
        return jsonify({
            'success': False,
            'error': '客户端未初始化'
        }), 400
    
    data = request.get_json()
    message = data.get('message')
    
    if not message:
        return jsonify({
            'success': False,
            'error': '消息不能为空'
        }), 400
    
    result = ai_client.chat(message)
    return jsonify(result)


@app.route('/api/history', methods=['GET'])
def api_get_history():
    """获取聊天历史API"""
    global ai_client
    
    if not ai_client:
        return jsonify({
            'success': False,
            'error': '客户端未初始化'
        }), 400
    
    history = ai_client.get_chat_history()
    return jsonify({
        'success': True,
        'history': history
    })


@app.route('/api/clear', methods=['POST'])
def api_clear_history():
    """清空聊天历史API"""
    global ai_client
    
    if not ai_client:
        return jsonify({
            'success': False,
            'error': '客户端未初始化'
        }), 400
    
    result = ai_client.clear_history()
    return jsonify(result)


@app.route('/api/status', methods=['GET'])
def api_get_status():
    """获取状态API"""
    global ai_client
    
    if not ai_client:
        return jsonify({
            'success': False,
            'error': '客户端未初始化'
        }), 400
    
    status = ai_client.get_status()
    return jsonify({
        'success': True,
        'status': status
    })


@app.route('/api/configs', methods=['GET'])
def api_get_configs():
    """获取配置列表API"""
    global ai_client
    
    if not ai_client:
        return jsonify({
            'success': False,
            'error': '客户端未初始化'
        }), 400
    
    configs = ai_client.get_config_list()
    return jsonify({
        'success': True,
        'configs': configs
    })


if __name__ == '__main__':
    print("🚀 启动ARC_Spec_Python Flask Web应用")
    print("📋 功能特性:")
    print("  - Web聊天界面")
    print("  - 多配置切换")
    print("  - 聊天历史管理")
    print("  - RESTful API接口")
    print("\n🌐 访问地址: http://localhost:5000")
    print("\n💡 API端点:")
    print("  POST /api/init - 初始化")
    print("  POST /api/config - 设置配置")
    print("  POST /api/chat - 发送消息")
    print("  GET  /api/history - 获取历史")
    print("  POST /api/clear - 清空历史")
    print("  GET  /api/status - 获取状态")
    print("  GET  /api/configs - 获取配置列表")
    print("\n按 Ctrl+C 停止服务器")
    
    try:
        app.run(debug=True, host='0.0.0.0', port=5000)
    except KeyboardInterrupt:
        print("\n👋 服务器已停止")