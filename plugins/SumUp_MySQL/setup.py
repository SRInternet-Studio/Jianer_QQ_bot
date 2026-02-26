import asyncio
import re, os, shutil
from Tools.GoogleAI import Context, Roles, Parts
from Tools.user_info import get_nickname_by_userid as get_user_nickname
from collections import defaultdict, deque, Counter
import pickle, gc
from Hyper import Configurator
Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())
from Hyper import Events
from Tools.websocket_message import ws_custom_api
from plugins.SumUp_MySQL.chat_database_info import get_database_stats
# 导入 PyMySQL 库和 contextlib 用于连接管理
import pymysql
from contextlib import contextmanager
import json

TRIGGHT_KEYWORD = "Any"
HELP_MESSAGE = f'''{Configurator.cm.get_cfg().others["reminder"]}总结以上N条消息 —> 总结当前群聊的指定数量的消息 (0<N<=1000)
{Configurator.cm.get_cfg().others["reminder"]}聊天数据看板 —> 展示当前(或全部)群聊的聊天数据看板
{Configurator.cm.get_cfg().others["reminder"]}查看数据库状态 —> 展示当前聊天数据库的状态'''

client = Context(
    api_key=Configurator.cm.get_cfg().others["gemini_key"],
    model=Configurator.cm.get_cfg().others.get("gemini_model", "gemini-2.0-flash-exp"),
    base_url=Configurator.cm.get_cfg().others.get("gemini_base_url", "https://generativelanguage.googleapis.com")
)

script_dir = os.path.dirname(__file__)
mysql_config_path = os.path.join(script_dir, "mysql.json")

# 默认 MySQL 配置,防止tm没有mysql.json
MYSQL_HOST = "localhost"
MYSQL_USER = "root"
MYSQL_PASSWORD = "Jianer_chat_db"
MYSQL_DATABASE = "jianer_chat_db"
MYSQL_PORT = 3306

#try 从 mysql.json 加载配置
try:
    with open(mysql_config_path, 'r', encoding='utf-8') as f:
        mysql_json_data = json.load(f)
        MYSQL_HOST = mysql_json_data.get("host", MYSQL_HOST)
        MYSQL_USER = mysql_json_data.get("user", MYSQL_USER)
        MYSQL_PASSWORD = mysql_json_data.get("password", MYSQL_PASSWORD)
        MYSQL_DATABASE = mysql_json_data.get("database", MYSQL_DATABASE)
        MYSQL_PORT = mysql_json_data.get("port", MYSQL_PORT)
    print(f"SumUp: 成功从 '{mysql_config_path}' 加载 MySQL 配置。")
except FileNotFoundError:
    print(f"SumUp: 未找到 '{mysql_config_path}' 文件，将使用默认 MySQL 配置。请创建此文件以自定义数据库连接。")
except json.JSONDecodeError:
    print(f"SumUp: '{mysql_config_path}' 文件格式错误，无法解析 JSON。请检查文件内容，将使用默认 MySQL 配置。")
except Exception as e:
    print(f"SumUp: 加载 '{mysql_config_path}' 时发生未知错误: {e}，将使用默认 MySQL 配置。")


class MySQLManager:
    def __init__(self, host, user, password, database, port):
        self.host = host
        self.user = user
        self.password = password
        self.database = database
        self.port = port
        self._create_database_and_tables()

    @contextmanager
    def _get_connection(self):
        conn = None
        try:
            conn = pymysql.connect(
                host=self.host,
                user=self.user,
                password=self.password,
                database=self.database,
                port=self.port,
                charset='utf8mb4',
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=False
            )
            yield conn
        except pymysql.Error as e:
            print(f"SumUp: MySQL 连接或操作错误: {e}")
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()

    def _create_database_and_tables(self):
        temp_conn = None
        try:
            temp_conn = pymysql.connect(
                host=self.host,
                user=self.user,
                password=self.password,
                port=self.port,
                charset='utf8mb4'
            )
            with temp_conn.cursor() as cursor:
                # 检查并创建数据库
                cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{self.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
            temp_conn.commit()
            print(f"SumUp: 数据库 '{self.database}' 检查/创建成功。")
        except pymysql.Error as e:
            print(f"SumUp: 创建数据库 '{self.database}' 失败: {e}")
            raise # 无法创建数据库是严重错误
        finally:
            if temp_conn:
                temp_conn.close()

        with self._get_connection() as conn:
            with conn.cursor() as cursor:
                # 创建 groups 表 (存储群ID)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS `groups` (
                        `group_id` VARCHAR(255) PRIMARY KEY
                    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
                """)
                # 创建 messages 表 (存储聊天消息)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS `messages` (
                        `id` INT AUTO_INCREMENT PRIMARY KEY,
                        `group_id` VARCHAR(255) NOT NULL,
                        `user_id` VARCHAR(255) NOT NULL,
                        `nickname` VARCHAR(255) NOT NULL,
                        `content` TEXT NOT NULL,
                        `timestamp` DATETIME DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (`group_id`) REFERENCES `groups`(`group_id`) ON DELETE CASCADE
                    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
                """)
            conn.commit()
        print("SumUp: MySQL 表 'groups' 和 'messages' 检查/创建成功。")

    def add_message(self, group_id: str, user_id: str, nickname: str, content: str):
        """添加消息到数据库"""
        with self._get_connection() as conn:
            with conn.cursor() as cursor:
                # 确保群组存在，如果不存在则插入 (IGNORE 避免重复插入错误)
                cursor.execute("INSERT IGNORE INTO `groups` (`group_id`) VALUES (%s)", (group_id,))
                # 插入消息
                cursor.execute(
                    "INSERT INTO `messages` (`group_id`, `user_id`, `nickname`, `content`) VALUES (%s, %s, %s, %s)",
                    (group_id, user_id, nickname, content)
                )
            conn.commit()
        # print(f"SumUp: 消息已写入数据库: 群 {group_id}, 用户 {nickname}")

    def get_messages(self, group_id: str, limit: int = 1000) -> list:
        """从数据库获取指定群聊的最新 N 条消息"""
        with self._get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT nickname, content FROM `messages` WHERE `group_id` = %s ORDER BY `timestamp` DESC LIMIT %s",
                    (group_id, limit)
                )
                return cursor.fetchall()[::-1]

    def get_all_group_ids(self) -> list:
        """获取所有已记录的群聊ID"""
        with self._get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT `group_id` FROM `groups`")
                return [row['group_id'] for row in cursor.fetchall()]

    def get_group_stats(self, group_id: str):
        """获取指定群聊的消息总数和发言人数"""
        with self._get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) as message_count, COUNT(DISTINCT `user_id`) as speaker_count FROM `messages` WHERE `group_id` = %s",
                    (group_id,)
                )
                return cursor.fetchone()

    def get_all_messages_content_for_hot_words(self, group_id: str) -> list:
        """获取指定群聊的所有消息内容，用于热词计算"""
        with self._get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT content FROM `messages` WHERE `group_id` = %s",
                    (group_id,)
                )
                return cursor.fetchall()

try:
    db_manager = MySQLManager(MYSQL_HOST, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE, MYSQL_PORT)
    print("SumUp: MySQL Manager 初始化成功。")
except Exception as e:
    print(f"SumUp: MySQL Manager 初始化失败，请检查配置和数据库连接: {e}")
    db_manager = None

# --- 辅助函数 (估算Token) ---
def estimate_tokens(text: str) -> int:
    """估算Token（中文≈1字/Tok，英文≈1词/4字母）"""
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    non_chinese = len(text) - chinese_chars
    return chinese_chars + (non_chinese // 4) + 1

# --- 消息总结相关函数 ---
def max_summarizable_msgs(group_id: str, max_tokens=800000) -> int:
    """计算当前群聊最多可总结的消息条数，从数据库获取"""
    if not db_manager:
        return 0

    try:
        # 从数据库获取最多1000条最新消息
        history_from_db = db_manager.get_messages(group_id, limit=1000)
        total_tokens = 0
        count = 0
        # 从最新消息向前回溯，直到Token超限
        for msg in reversed(history_from_db): # history_from_db 已经是按时间正序
            msg_tokens = estimate_tokens(f"{msg['nickname']}: {msg['content']}")
            if total_tokens + msg_tokens > max_tokens:
                break
            total_tokens += msg_tokens
            count += 1
        return count
    except Exception as e:
        print(f"SumUp: 计算群 {group_id} 可总结消息数失败: {e}")
        return 0

async def handle_summary_request(group_id: str, match):
    """处理用户总结请求（如：'总结以上100条消息'），从数据库获取消息"""
    if not db_manager:
        return "❌ 数据库未连接，无法总结消息！"

    try:
        n = int(match.group(1))
        if n <= 0 or n > 1000:
            return "❌ 命令格式错误！请用：'总结以上N条消息' (0<N<=1000)"

        messages_from_db = db_manager.get_messages(group_id, limit=n)

        if len(messages_from_db) < 5:
            return "⚠️ 消息过少（少于 5 条消息），无法有效总结。"

        total_tokens = sum(estimate_tokens(f"{msg['nickname']}: {msg['content']}")
                           for msg in messages_from_db)
        max_tokens = 800000  # Gemini 模型的 token 限制，预留缓冲
        if total_tokens > max_tokens:
            max_n = max_summarizable_msgs(group_id, max_tokens)
            return f"⚠️ 消息过长（{total_tokens} Tokens > 上限{max_tokens}）\n最多可总结{max_n}条消息"

        messages_text = "\n".join(f"{msg['nickname']}: {msg['content']}" for msg in messages_from_db)
        prompt = f'''请根据以下群聊记录生成摘要：

### 聊天记录：
{messages_text}

### 总结要求：
1. 用紧凑的格式呈现，详细但少于{max_tokens // 10}个汉字
2. 关键点或关键决策点需加粗
3. 标注提出重要意见的成员
4. 如果有，请列出未解决的问题
5. 总结后给出建议或方案
6. 尽量不要使用 Markdown 格式'''

        user_content = Roles.User(Parts.Text(prompt))
        response_generator = client.gen_content(user_content, model_override="gemini-2.0-flash", stream=False)

        full_response = ""
        for response_chunk, _ in response_generator:
            full_response += response_chunk

        return full_response
    except Exception as e:
        return f"❌ 总结时发生异常：\n{e}"

async def summarize_arbitrary_messages(messages_list: list, match):
    """总结从转发消息中提取的任意消息列表"""
    try:
        n = int(match.group(1))
        if n <= 0 or n > 1000:
            return "❌ 命令格式错误！请用：'总结以上N条消息' (0<N<=1000)"

        # 取消息列表的最后 N 条
        messages_to_summarize = messages_list[-n:]

        if len(messages_to_summarize) < 5:
            return "⚠️ 消息过少（少于 5 条消息），无法有效总结。"

        total_tokens = sum(estimate_tokens(f"{msg['user']}: {msg['content']}")
                           for msg in messages_to_summarize)
        max_tokens = 800000  # Gemini 模型的 token 限制，预留缓冲
        if total_tokens > max_tokens:
            return f"⚠️ 消息过长（{total_tokens} Tokens > 上限{max_tokens}）\n请减少总结的消息数量。"

        messages_text = "\n".join(f"{msg['user']}: {msg['content']}" for msg in messages_to_summarize)
        prompt = f'''请根据以下群聊记录生成摘要：

### 聊天记录：
{messages_text}

### 总结要求：
1. 用紧凑的格式呈现，详细但少于{max_tokens // 10}个汉字
2. 关键点或关键决策点需加粗
3. 标注提出重要意见的成员
4. 如果有，请列出未解决的问题
5. 总结后给出建议或方案
6. 尽量不要使用 Markdown 格式'''

        user_content = Roles.User(Parts.Text(prompt))
        response_generator = client.gen_content(user_content, model_override="gemini-2.0-flash", stream=False)

        full_response = ""
        for response_chunk, _ in response_generator:
            full_response += response_chunk

        return full_response
    except Exception as e:
        return f"❌ 总结转发消息时发生异常：\n{e}"

async def handle_node_messages(data: dict) -> list:
    """
    处理转发消息节点，提取其中的文本消息。
    此函数返回一个消息列表，这些消息不会被存储到主数据库中，仅用于临时总结。
    """
    processed_messages = []

    print(f"SumUp: 开始处理转发消息数据")
    framework_response = await ws_custom_api("get_version_info", {})
    app_name = ""
    if isinstance(framework_response, dict):
        data_part = framework_response.get('data', {})
        if isinstance(data_part, dict):
            app_name = data_part.get('app_name', '')
        else:
            app_name = str(framework_response.get('data', ''))

    print(f"SumUp: 检测到框架: {app_name}")

    if "NapCat.Onebot" in app_name:
        print("SumUp: 使用 NapCat 格式解析")
        if 'messages' in data:
            for message_item in data['messages']:
                sender = message_item.get('sender', {})
                nickname = sender.get('nickname', str(message_item.get('user_id', '')))
                message_list = message_item.get('message', [])

                text_parts = []
                for message_content in message_list:
                    if message_content.get('type') == 'text':
                        text_data = message_content.get('data', {})
                        text = text_data.get('text', '')
                        if text:
                            text_parts.append(text)

                full_text = ''.join(text_parts)
                if full_text:
                    processed_messages.append({"user": nickname, "content": full_text})
        elif 'data' in data and 'messages' in data['data']:
            for message_item in data['data']['messages']:
                sender = message_item.get('sender', {})
                nickname = sender.get('nickname', str(message_item.get('user_id', '')))
                message_list = message_item.get('message', [])

                text_parts = []
                for message_content in message_list:
                    if message_content.get('type') == 'text':
                        text_data = message_content.get('data', {})
                        text = text_data.get('text', '')
                        if text:
                            text_parts.append(text)

                full_text = ''.join(text_parts)
                if full_text:
                    processed_messages.append({"user": nickname, "content": full_text})
    else:
        print("SumUp: 使用Lagrange.OneBot格式解析")
        for message_node in data['message']:
            if message_node.get('type') == 'node':
                node_data = message_node.get('data', {})
                nickname = node_data.get('nickname', node_data.get('user_id', ''))
                content_list = node_data.get('content', [])

                text_parts = []
                for content_item in content_list:
                    if content_item.get('type') == 'text':
                        text_data = content_item.get('data', {})
                        text = text_data.get('text', '')
                        if text:
                            text_parts.append(text)

                full_text = ''.join(text_parts)
                if full_text:
                    processed_messages.append({"user": nickname, "content": full_text})

    print(f"SumUp: 转发消息处理完成，共收集 {len(processed_messages)} 条消息")
    return processed_messages

# --- 热词计算和数据看板函数 ---
def _calculate_hot_words_internal(messages_list_for_processing: list, min_count=1, max_words=5, recursion_depth=0):
    """内部函数：计算热词排行，接收一个消息列表"""
    if recursion_depth > 20: # 防止无限递归
        return []

    all_words = []
    # 常见停用词列表
    stop_words = {
        '的', '了', '是', '在', '我', '有', '和', '就', '不', '人', '都', '一', '一个',
        '上', '也', '很', '到', '说', '要', '去', '你', '会', '着', '没有', '看', '好',
        '自己', '这', '但', '而', '于', '以', '可', '为', '之', '与', '则', '其', '或',
        '即', '因', '及', '由', '时', '等', '所', '并', '且', '着', '呢', '吗', '啊',
        '吧', '呀', '哦', '恩', '嗯', '哈', '嘿', '嘻', '呗', '哒', '啦', '哟', '呼'
    }

    for msg in messages_list_for_processing:
        # 只提取中文字符，且长度在2-4个字符之间，避免提取单个字或过长的词
        # 使用lookbehind和lookahead来确保我们提取的是完整的中文词语
        words = re.findall(r'(?<![\u4e00-\u9fff])([\u4e00-\u9fff]{2,4})(?![\u4e00-\u9fff])', msg['content'])
        # 过滤掉停用词
        filtered_words = [word for word in words if word not in stop_words]
        all_words.extend(filtered_words)

    # 统计词频
    word_count = Counter(all_words)

    # 如果热词不足3个，降低标准
    current_min_count = min_count
    hot_words = [word for word, _ in word_count.most_common(max_words) if _ >= current_min_count]

    while len(hot_words) < 3 and current_min_count > 1:
        current_min_count -= 1
        hot_words = [word for word, _ in word_count.most_common(max_words) if _ >= current_min_count]

    return hot_words

def calculate_hot_words(group_id: str, min_count=1, max_words=5) -> list:
    """计算热词排行，从数据库获取消息"""
    if not db_manager:
        return []

    try:

        db_messages_raw = db_manager.get_all_messages_content_for_hot_words(group_id)
        messages_for_processing = [{"content": msg['content']} for msg in db_messages_raw]
        return _calculate_hot_words_internal(messages_for_processing, min_count, max_words)
    except Exception as e:
        print(f"SumUp: 计算群 {group_id} 热词失败: {e}")
        return []

def generate_chat_summary(group_id: str):
    """生成单个群的聊天数据看板，从数据库获取数据"""
    if not db_manager:
        return f"群：{group_id}\n消息总数：0\n发言人数：0\n热词排行：暂无数据 (数据库未连接)"

    try:
        group_stats = db_manager.get_group_stats(group_id)
        if not group_stats or group_stats['message_count'] == 0:
            return f"群：{group_id}\n消息总数：0\n发言人数：0\n热词排行：暂无数据"

        message_count = group_stats['message_count']
        speaker_count = group_stats['speaker_count']

        hot_words = calculate_hot_words(group_id, 1, 5)

        hot_words_str = '；'.join(hot_words) if hot_words else "暂无足够热词"

        hot_words_str = hot_words_str.replace('图片', '[图片]')
        summary = f"群：{group_id}\n消息总数：{message_count}\n发言人数：{speaker_count}\n热词排行：{hot_words_str}"
        return summary
    except Exception as e:
        print(f"SumUp: 生成群 {group_id} 聊天数据看板失败: {e}")
        return f"群：{group_id}\n生成数据看板失败: {e}"

async def on_message(event, actions, Manager, Events, Segments, bot_name, gen_message, ADMINS, CONFUSED_WORD):
    if not isinstance(event, Events.GroupMessageEvent):
        return None

    user_message = str(event.message).strip()
    reminder_prefix = Configurator.cm.get_cfg().others['reminder']
    if user_message == f'{reminder_prefix}查看数据库信息' or user_message == f'{reminder_prefix}查看数据库状态':
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(get_database_stats())))
        return True
    if user_message.startswith(reminder_prefix) and '聊天数据看板' in user_message:
        if '@all' in user_message or '@全体' in user_message:
            if not str(event.user_id) in ADMINS:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
                return True

            chat_summary = """===== 全群聊天数据看板 ====="""
            if db_manager:
                all_group_ids = db_manager.get_all_group_ids()
                for group_id in all_group_ids:
                    group_summary = generate_chat_summary(group_id)
                    chat_summary += f"\n{group_summary}\n{'-'*30}"
            else:
                chat_summary += "\n数据库未连接，无法获取所有群聊数据。"

            await actions.send_group_forward_msg(
                group_id=event.group_id,
                message=Manager.Message(Segments.CustomNode(
                    str(event.self_id),
                    bot_name,
                    Manager.Message(Segments.Text(chat_summary))
            )))
        else:
            chat_summary = generate_chat_summary(event.group_id)
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(chat_summary)))
        return True
    
    match = re.search(r"总结(?:以上|最近)?(\d+)(?:条|个)?消息", user_message)
    if match and user_message.startswith(Configurator.cm.get_cfg().others["reminder"]):
        selfID = await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"请等待，{bot_name} 正在总结消息......φ(゜▽゜*)♪")))
        message_to_send = ""
        if isinstance(event.message[0], Segments.Reply):
            content = await actions.get_msg(event.message[0].id)
            msg = gen_message({"message": content.data["message"]})
            extracted_messages_for_summary = []
            for i in msg:
                if isinstance(i, Segments.Forward):
                    data = Manager.Ret.fetch(await actions.custom.get_forward_msg(id=i.id)).data.raw
                    extracted_messages_for_summary = await handle_node_messages(data)
                    break

            if not extracted_messages_for_summary:
                message_to_send = "❌ 未找到转发的消息！\n请确保引用消息的是一条聊天记录，并确保聊天记录中包含需要总结的消息"
            else:
                message_to_send = await summarize_arbitrary_messages(extracted_messages_for_summary, match)
        else:
            message_to_send = await handle_summary_request(event.group_id, match)

        if len(message_to_send) < 400:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(message_to_send)))
        else:
            await actions.send_group_forward_msg(
                group_id=event.group_id,
                message=Manager.Message(Segments.CustomNode(
                                        str(event.self_id),
                                        bot_name,
                                        Manager.Message(Segments.Text(message_to_send))
                    )
                )
            )
        await actions.del_message(selfID.data.message_id)
        return True
    else:
        if db_manager:
            nike = await get_user_nickname(event.user_id, Manager, actions)
            db_manager.add_message(str(event.group_id), str(event.user_id), nike, user_message)
            print(f"SumUp: 添加ID为 {event.user_id} 的用户 {nike} 的信息到数据库，群组 {event.group_id}")
        else:
            print(f"SumUp: 数据库未连接，未保存群组 {event.group_id} 的消息。")

        return None
