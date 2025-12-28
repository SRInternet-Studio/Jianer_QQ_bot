#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
异步聊天机器人示例

这个示例展示了如何使用ARC_Spec_Python创建一个高性能的异步聊天机器人，
支持多用户并发对话和智能会话管理。

特性:
- 异步处理多用户对话
- 会话状态管理
- 智能配置切换
- 对话历史记录
- 性能监控

运行方式:
    python async_chat_bot.py
"""

import sys
import asyncio
import logging
import json
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from collections import defaultdict, deque
import time

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


@dataclass
class ChatMessage:
    """聊天消息数据类"""
    id: str
    user_id: str
    content: str
    timestamp: datetime
    message_type: str  # 'user', 'ai', 'system'
    config_used: Optional[str] = None
    processing_time: Optional[float] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        data = asdict(self)
        data['timestamp'] = self.timestamp.isoformat()
        return data


@dataclass
class UserSession:
    """用户会话数据类"""
    user_id: str
    session_id: str
    created_at: datetime
    last_activity: datetime
    current_config: Optional[str] = None
    message_count: int = 0
    total_processing_time: float = 0.0
    
    def update_activity(self):
        """更新活动时间"""
        self.last_activity = datetime.now()
    
    def is_expired(self, timeout_minutes: int = 30) -> bool:
        """检查会话是否过期"""
        return datetime.now() - self.last_activity > timedelta(minutes=timeout_minutes)


class AsyncChatBot:
    """异步聊天机器人"""
    
    def __init__(self, project_root: str = None, max_history_per_user: int = 100):
        if project_root is None:
            project_root = Path(__file__).parent.parent
        else:
            project_root = Path(project_root)
            
        self.project_root = project_root
        self.config_dir = project_root / 'configs'
        self.parsers_dir = project_root / 'arcspec_ai' / 'parsers'
        
        # AI组件
        self.configs = {}
        self.parser_registry = None
        self.parsers_cache = {}  # 缓存解析器实例
        
        # 会话管理
        self.sessions: Dict[str, UserSession] = {}
        self.user_histories: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=max_history_per_user)
        )
        
        # 性能统计
        self.stats = {
            'total_messages': 0,
            'successful_messages': 0,
            'failed_messages': 0,
            'active_users': 0,
            'total_processing_time': 0.0,
            'start_time': datetime.now()
        }
        
        # 配置
        self.max_history_per_user = max_history_per_user
        self.session_timeout_minutes = 30
        self.cleanup_interval_seconds = 300  # 5分钟清理一次
        
        self.initialized = False
        self._cleanup_task = None
    
    async def initialize(self) -> Dict[str, Any]:
        """
        异步初始化聊天机器人
        
        Returns:
            Dict[str, Any]: 初始化结果
        """
        try:
            logger.info("开始初始化异步聊天机器人...")
            
            # 在线程池中执行同步操作
            loop = asyncio.get_event_loop()
            
            # 加载配置
            self.configs = await loop.run_in_executor(
                None, load_ai_configs, str(self.config_dir)
            )
            
            if not self.configs:
                raise Exception("未找到任何配置文件")
            
            # 加载解析器
            self.parser_registry = await loop.run_in_executor(
                None, load_parsers, str(self.parsers_dir)
            )
            
            # 预加载所有解析器
            await self._preload_parsers()
            
            # 启动清理任务
            self._cleanup_task = asyncio.create_task(self._cleanup_sessions())
            
            self.initialized = True
            
            logger.info(
                f"聊天机器人初始化成功，加载了 {len(self.configs)} 个配置，"
                f"预加载了 {len(self.parsers_cache)} 个解析器"
            )
            
            return {
                'success': True,
                'message': f'成功加载 {len(self.configs)} 个配置',
                'configs': list(self.configs.keys()),
                'parsers_loaded': len(self.parsers_cache)
            }
            
        except Exception as e:
            logger.error(f"初始化失败: {e}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def _preload_parsers(self):
        """预加载所有解析器"""
        logger.info("预加载解析器...")
        
        loop = asyncio.get_event_loop()
        
        for config_name, config in self.configs.items():
            try:
                parser = await loop.run_in_executor(
                    None,
                    self.parser_registry.create_parser,
                    config['ResponseType'],
                    config
                )
                
                if parser:
                    self.parsers_cache[config_name] = parser
                    logger.debug(f"预加载解析器: {config_name}")
                else:
                    logger.warning(f"无法预加载解析器: {config_name}")
                    
            except Exception as e:
                logger.error(f"预加载解析器 {config_name} 失败: {e}")
    
    async def _cleanup_sessions(self):
        """定期清理过期会话"""
        while True:
            try:
                await asyncio.sleep(self.cleanup_interval_seconds)
                
                current_time = datetime.now()
                expired_sessions = []
                
                for session_id, session in self.sessions.items():
                    if session.is_expired(self.session_timeout_minutes):
                        expired_sessions.append(session_id)
                
                # 清理过期会话
                for session_id in expired_sessions:
                    session = self.sessions.pop(session_id)
                    logger.info(f"清理过期会话: {session.user_id} ({session_id})")
                
                # 更新活跃用户统计
                self.stats['active_users'] = len(self.sessions)
                
                if expired_sessions:
                    logger.info(f"清理了 {len(expired_sessions)} 个过期会话")
                    
            except Exception as e:
                logger.error(f"会话清理任务出错: {e}")
    
    def _get_or_create_session(self, user_id: str) -> UserSession:
        """获取或创建用户会话"""
        # 查找现有会话
        for session in self.sessions.values():
            if session.user_id == user_id and not session.is_expired():
                session.update_activity()
                return session
        
        # 创建新会话
        session_id = str(uuid.uuid4())
        session = UserSession(
            user_id=user_id,
            session_id=session_id,
            created_at=datetime.now(),
            last_activity=datetime.now()
        )
        
        self.sessions[session_id] = session
        logger.info(f"创建新会话: {user_id} ({session_id})")
        
        return session
    
    def _get_best_config(self, user_id: str, message: str) -> str:
        """
        智能选择最佳配置
        
        Args:
            user_id: 用户ID
            message: 用户消息
            
        Returns:
            str: 配置名称
        """
        session = self._get_or_create_session(user_id)
        
        # 如果用户有当前配置，继续使用
        if session.current_config and session.current_config in self.configs:
            return session.current_config
        
        # 简单的智能选择逻辑（可以根据需要扩展）
        message_lower = message.lower()
        
        # 根据消息内容选择配置
        for config_name, config in self.configs.items():
            friendly_name = config.get('FriendlyName', '').lower()
            model = config.get('Model', '').lower()
            
            # 如果消息中包含配置相关关键词
            if any(keyword in message_lower for keyword in [friendly_name, model]):
                session.current_config = config_name
                return config_name
        
        # 默认使用第一个配置
        default_config = list(self.configs.keys())[0]
        session.current_config = default_config
        return default_config
    
    async def chat(self, user_id: str, message: str, config_name: Optional[str] = None) -> Dict[str, Any]:
        """
        异步聊天处理
        
        Args:
            user_id: 用户ID
            message: 用户消息
            config_name: 指定配置名称（可选）
            
        Returns:
            Dict[str, Any]: 聊天结果
        """
        if not self.initialized:
            return {
                'success': False,
                'error': '聊天机器人未初始化'
            }
        
        start_time = time.time()
        message_id = str(uuid.uuid4())
        
        # 更新统计
        self.stats['total_messages'] += 1
        
        try:
            # 获取或创建会话
            session = self._get_or_create_session(user_id)
            
            # 选择配置
            if not config_name:
                config_name = self._get_best_config(user_id, message)
            elif config_name not in self.configs:
                return {
                    'success': False,
                    'error': f'配置 {config_name} 不存在'
                }
            
            # 获取解析器
            parser = self.parsers_cache.get(config_name)
            if not parser:
                return {
                    'success': False,
                    'error': f'解析器 {config_name} 不可用'
                }
            
            # 记录用户消息
            user_message = ChatMessage(
                id=message_id,
                user_id=user_id,
                content=message,
                timestamp=datetime.now(),
                message_type='user'
            )
            self.user_histories[user_id].append(user_message)
            
            # 在线程池中执行AI处理
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, parser.parse, message
            )
            
            processing_time = time.time() - start_time
            
            # 记录AI回复
            ai_message = ChatMessage(
                id=str(uuid.uuid4()),
                user_id=user_id,
                content=response,
                timestamp=datetime.now(),
                message_type='ai',
                config_used=config_name,
                processing_time=processing_time
            )
            self.user_histories[user_id].append(ai_message)
            
            # 更新会话统计
            session.message_count += 1
            session.total_processing_time += processing_time
            session.current_config = config_name
            session.update_activity()
            
            # 更新全局统计
            self.stats['successful_messages'] += 1
            self.stats['total_processing_time'] += processing_time
            self.stats['active_users'] = len(self.sessions)
            
            logger.info(
                f"用户 {user_id} 对话成功，配置: {config_name}, "
                f"处理时间: {processing_time:.2f}s"
            )
            
            return {
                'success': True,
                'response': response,
                'config_used': config_name,
                'processing_time': processing_time,
                'session_id': session.session_id,
                'message_id': ai_message.id
            }
            
        except Exception as e:
            processing_time = time.time() - start_time
            
            # 记录错误消息
            error_message = ChatMessage(
                id=str(uuid.uuid4()),
                user_id=user_id,
                content=f'错误: {str(e)}',
                timestamp=datetime.now(),
                message_type='system',
                processing_time=processing_time
            )
            self.user_histories[user_id].append(error_message)
            
            # 更新统计
            self.stats['failed_messages'] += 1
            
            logger.error(f"用户 {user_id} 对话失败: {e}")
            
            return {
                'success': False,
                'error': str(e),
                'processing_time': processing_time
            }
    
    async def get_user_history(self, user_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """获取用户聊天历史"""
        history = list(self.user_histories[user_id])
        
        if limit:
            history = history[-limit:]
        
        return [msg.to_dict() for msg in history]
    
    async def set_user_config(self, user_id: str, config_name: str) -> Dict[str, Any]:
        """设置用户配置"""
        if config_name not in self.configs:
            return {
                'success': False,
                'error': f'配置 {config_name} 不存在'
            }
        
        session = self._get_or_create_session(user_id)
        session.current_config = config_name
        
        return {
            'success': True,
            'message': f'用户 {user_id} 配置已设置为 {config_name}',
            'config': {
                'name': config_name,
                'friendly_name': self.configs[config_name].get('FriendlyName', config_name)
            }
        }
    
    async def get_user_status(self, user_id: str) -> Dict[str, Any]:
        """获取用户状态"""
        session = None
        for s in self.sessions.values():
            if s.user_id == user_id and not s.is_expired():
                session = s
                break
        
        if not session:
            return {
                'user_id': user_id,
                'active': False,
                'message_count': len(self.user_histories[user_id])
            }
        
        config_info = None
        if session.current_config:
            config = self.configs[session.current_config]
            config_info = {
                'name': session.current_config,
                'friendly_name': config.get('FriendlyName', session.current_config),
                'model': config.get('Model', 'Unknown')
            }
        
        return {
            'user_id': user_id,
            'session_id': session.session_id,
            'active': True,
            'created_at': session.created_at.isoformat(),
            'last_activity': session.last_activity.isoformat(),
            'message_count': session.message_count,
            'total_processing_time': session.total_processing_time,
            'current_config': config_info,
            'history_length': len(self.user_histories[user_id])
        }
    
    async def get_stats(self) -> Dict[str, Any]:
        """获取机器人统计信息"""
        uptime = datetime.now() - self.stats['start_time']
        
        return {
            'initialized': self.initialized,
            'uptime': str(uptime).split('.')[0],
            'total_configs': len(self.configs),
            'active_sessions': len(self.sessions),
            'total_messages': self.stats['total_messages'],
            'successful_messages': self.stats['successful_messages'],
            'failed_messages': self.stats['failed_messages'],
            'success_rate': (
                self.stats['successful_messages'] / max(self.stats['total_messages'], 1) * 100
            ),
            'total_processing_time': self.stats['total_processing_time'],
            'average_processing_time': (
                self.stats['total_processing_time'] / max(self.stats['successful_messages'], 1)
            ),
            'active_users': len({s.user_id for s in self.sessions.values()})
        }
    
    async def shutdown(self):
        """关闭聊天机器人"""
        logger.info("正在关闭聊天机器人...")
        
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        
        # 清理资源
        self.sessions.clear()
        self.parsers_cache.clear()
        
        logger.info("聊天机器人已关闭")


async def demo_concurrent_chat():
    """并发聊天演示"""
    print("=== 异步聊天机器人并发演示 ===")
    
    # 创建聊天机器人
    bot = AsyncChatBot()
    
    # 初始化
    init_result = await bot.initialize()
    if not init_result['success']:
        print(f"❌ 初始化失败: {init_result['error']}")
        return
    
    print(f"✅ 聊天机器人初始化成功")
    print(f"📋 加载配置: {init_result['configs']}")
    
    # 模拟多用户并发对话
    users = ['Alice', 'Bob', 'Charlie', 'Diana']
    messages = [
        "你好！",
        "请介绍一下你自己",
        "今天天气怎么样？",
        "能帮我写个Python函数吗？",
        "谢谢你的帮助！"
    ]
    
    print(f"\n🚀 开始 {len(users)} 用户并发对话测试...")
    
    # 创建并发任务
    tasks = []
    for user in users:
        for i, message in enumerate(messages):
            task = asyncio.create_task(
                bot.chat(user, f"[{i+1}] {message}"),
                name=f"{user}-{i+1}"
            )
            tasks.append(task)
    
    # 等待所有任务完成
    start_time = time.time()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    total_time = time.time() - start_time
    
    # 统计结果
    successful = sum(1 for r in results if isinstance(r, dict) and r.get('success'))
    failed = len(results) - successful
    
    print(f"\n📊 并发测试结果:")
    print(f"  总任务数: {len(tasks)}")
    print(f"  成功: {successful}")
    print(f"  失败: {failed}")
    print(f"  总耗时: {total_time:.2f}秒")
    print(f"  平均每任务: {total_time/len(tasks):.3f}秒")
    
    # 显示用户状态
    print(f"\n👥 用户状态:")
    for user in users:
        status = await bot.get_user_status(user)
        if status['active']:
            print(f"  {user}: 活跃会话，{status['message_count']} 条消息")
        else:
            print(f"  {user}: 无活跃会话")
    
    # 显示统计信息
    stats = await bot.get_stats()
    print(f"\n📈 机器人统计:")
    print(f"  运行时间: {stats['uptime']}")
    print(f"  活跃会话: {stats['active_sessions']}")
    print(f"  总消息数: {stats['total_messages']}")
    print(f"  成功率: {stats['success_rate']:.1f}%")
    print(f"  平均处理时间: {stats['average_processing_time']:.3f}秒")
    
    # 关闭机器人
    await bot.shutdown()


async def demo_interactive_chat():
    """交互式聊天演示"""
    print("\n=== 交互式聊天演示 ===")
    print("输入 'quit' 退出，'status' 查看状态，'history' 查看历史")
    
    bot = AsyncChatBot()
    
    # 初始化
    init_result = await bot.initialize()
    if not init_result['success']:
        print(f"❌ 初始化失败: {init_result['error']}")
        return
    
    print(f"✅ 聊天机器人已启动")
    
    user_id = "interactive_user"
    
    try:
        while True:
            try:
                # 获取用户输入（在实际应用中可能需要异步输入）
                message = input("\n你: ").strip()
                
                if not message:
                    continue
                
                if message.lower() == 'quit':
                    break
                elif message.lower() == 'status':
                    status = await bot.get_user_status(user_id)
                    print(f"\n📊 状态: {json.dumps(status, indent=2, ensure_ascii=False)}")
                    continue
                elif message.lower() == 'history':
                    history = await bot.get_user_history(user_id, limit=5)
                    print(f"\n📜 最近5条历史:")
                    for msg in history:
                        print(f"  [{msg['message_type']}] {msg['content'][:50]}...")
                    continue
                
                # 发送消息
                result = await bot.chat(user_id, message)
                
                if result['success']:
                    print(f"\n🤖 AI ({result['config_used']}): {result['response']}")
                    print(f"   (处理时间: {result['processing_time']:.2f}秒)")
                else:
                    print(f"\n❌ 错误: {result['error']}")
                    
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"\n❌ 发生错误: {e}")
    
    finally:
        await bot.shutdown()
        print("\n👋 聊天结束")


async def main():
    """主函数"""
    print("🤖 ARC_Spec_Python 异步聊天机器人演示")
    print("\n选择演示模式:")
    print("1. 并发聊天测试")
    print("2. 交互式聊天")
    print("3. 两个都运行")
    
    try:
        choice = input("\n请选择 (1/2/3): ").strip()
        
        if choice == '1':
            await demo_concurrent_chat()
        elif choice == '2':
            await demo_interactive_chat()
        elif choice == '3':
            await demo_concurrent_chat()
            await demo_interactive_chat()
        else:
            print("无效选择，运行并发测试")
            await demo_concurrent_chat()
            
    except KeyboardInterrupt:
        print("\n\n👋 用户中断，程序退出")
    except Exception as e:
        logger.error(f"演示过程中发生错误: {e}")
        print(f"\n❌ 演示失败: {e}")


if __name__ == "__main__":
    # 运行演示
    asyncio.run(main())