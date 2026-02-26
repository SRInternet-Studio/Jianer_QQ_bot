import pymysql
import json
import os
from decimal import Decimal # 导入 Decimal 类型

def load_mysql_config(config_path=os.path.join(os.path.dirname(__file__), "mysql.json")):
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        return config
    except FileNotFoundError:
        print(f"配置文件未找到: {config_path}")
        return None
    except json.JSONDecodeError:
        print(f"配置文件JSON解析错误: {config_path}")
        return None

def create_connection(config):
    try:
        conn = pymysql.connect(
            host=config.get('host', '127.0.0.1'),
            user=config.get('user', ''),
            password=config.get('password', ''),
            database=config.get('database', ''),
            port=config.get('port', 3306),
            charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor
        )
        return conn
    except pymysql.Error as e:
        print(f"数据库连接失败: {e}")
        return None

def get_database_size(conn, database_name):
    try:
        with conn.cursor() as cursor:
            sql = """
            SELECT 
                ROUND(SUM(data_length + index_length) / 1024 / 1024, 2) AS total_size_mb
            FROM information_schema.tables 
            WHERE table_schema = %s
            """
            cursor.execute(sql, (database_name,))
            result = cursor.fetchone()
            # pymysql的ROUND()结果通常是Decimal，这里也确保返回Decimal
            return result.get('total_size_mb', Decimal('0.0')) if result else Decimal('0.0')
    except pymysql.Error as e:
        print(f"获取数据库 '{database_name}' 大小时发生错误: {e}")
        return Decimal('0.0')

def get_all_tables_info(conn, database_name):
    try:
        with conn.cursor() as cursor:
            sql = """
            SELECT
                table_name,
                table_rows,
                ROUND(data_length / 1024 / 1024, 2) AS data_size_mb,
                ROUND(index_length / 1024 / 1024, 2) AS index_size_mb,
                ROUND((data_length + index_length) / 1024 / 1024, 2) AS total_size_mb,
                engine,
                table_comment
            FROM information_schema.tables
            WHERE table_schema = %s
            ORDER BY data_length + index_length DESC
            """
            cursor.execute(sql, (database_name,))
            fetched_tables = cursor.fetchall()
            print(f"DEBUG: get_all_tables_info for '{database_name}' returned: {fetched_tables}")
            return fetched_tables
    except pymysql.Error as e:
        print(f"获取数据库 '{database_name}' 所有表信息时发生错误: {e}")
        return []

def get_exact_table_count(conn, table_name):
    try:
        with conn.cursor() as cursor:
            sql = f"SELECT COUNT(*) as exact_count FROM `{table_name}`"
            cursor.execute(sql)
            result = cursor.fetchone()
            return result.get('exact_count') if result else None
    except pymysql.Error as e:
        print(f"获取表 '{table_name}' 精确记录数时发生错误: {e}")
        return None

def get_database_charset_and_engine(conn, database_name):
    try:
        with conn.cursor() as cursor:
            sql = """
            SELECT 
                default_character_set_name,
                default_collation_name
            FROM information_schema.schemata 
            WHERE schema_name = %s
            """
            cursor.execute(sql, (database_name,))
            fetched_db_info = cursor.fetchone()
            print(f"DEBUG: get_database_charset_and_engine for '{database_name}' returned: {fetched_db_info}")
            return fetched_db_info
    except pymysql.Error as e:
        print(f"获取数据库 '{database_name}' 字符集和排序规则时发生错误: {e}")
        return None

def get_database_stats():
    config = load_mysql_config()
    if not config:
        return "错误: 无法加载数据库配置文件"
    conn = create_connection(config)
    if not conn:
        return "错误: 数据库连接失败"
    
    database_name = config.get('database', 'jianer_chat_db')
    result_lines = []
    
    try:
        result_lines.append(f"数据库信息统计 - {database_name}")
        result_lines.append("=" * 20)
        db_info = get_database_charset_and_engine(conn, database_name)
        if db_info:
            result_lines.append(f"字符集: {db_info.get('DEFAULT_CHARACTER_SET_NAME', '未知')}")
            result_lines.append(f"排序规则: {db_info.get('DEFAULT_COLLATION_NAME', '未知')}")
        else:
            result_lines.append("无法获取数据库字符集和排序规则信息。")
            
        total_size = get_database_size(conn, database_name)
        result_lines.append(f"数据库总大小: {total_size:.2f} MB") # 格式化输出Decimal
        result_lines.append("-" * 20)
        tables_info = get_all_tables_info(conn, database_name)
        
        if not tables_info:
            result_lines.append("未找到任何表或无法获取表信息")
            return "\n".join(result_lines)
        
        result_lines.append(f"\n共找到 {len(tables_info)} 个表:")
        result_lines.append("-" * 20)
        result_lines.append(f"{'表名':<30} {'记录数(估计)':<15} {'精确记录数':<15} {'数据大小(MB)':<12} {'索引大小(MB)':<12} {'存储引擎':<10}")
        result_lines.append("-" * 20)
        
        total_rows_estimated = 0
        total_rows_exact = 0
        # 将累加变量初始化为 Decimal 类型
        total_data_size = Decimal('0.0')
        total_index_size = Decimal('0.0')
        
        for table in tables_info:
            table_name = table.get('TABLE_NAME')
            if not table_name:
                print(f"DEBUG: 发现一个缺少 'TABLE_NAME' 键的表信息条目，已跳过。原始数据: {table}")
                result_lines.append(f"警告: 发现一个缺少表名的条目，已跳过。")
                continue

            estimated_rows = table.get('TABLE_ROWS', 0) or 0
            
            # 确保从字典获取的值是 Decimal，如果缺失则使用 Decimal('0.0')
            data_size = table.get('data_size_mb', Decimal('0.0'))
            index_size = table.get('index_size_mb', Decimal('0.0'))
            
            # 如果数据库返回的是None，也将其转换为Decimal('0.0')
            if data_size is None:
                data_size = Decimal('0.0')
            if index_size is None:
                index_size = Decimal('0.0')

            engine = table.get('ENGINE', '未知') 
            
            exact_count = get_exact_table_count(conn, table_name)

            row_line = f"{table_name:<30} {estimated_rows:<15} {exact_count if exact_count is not None else 'N/A':<15} {data_size:<12.2f} {index_size:<12.2f} {engine:<10}"
            result_lines.append(row_line)
            
            total_rows_estimated += estimated_rows
            if exact_count is not None:
                total_rows_exact += exact_count
            
            # 累加时，Decimal + Decimal 是允许的
            total_data_size += data_size
            total_index_size += index_size
        
        result_lines.append("-" * 20)
        total_line = f"{'总计':<30} {total_rows_estimated:<15} {total_rows_exact if total_rows_exact is not None else 'N/A':<15} {total_data_size:<12.2f} {total_index_size:<12.2f}"
        result_lines.append(total_line)
        result_lines.append("=" * 20)
        result_lines.append("\n空间使用分析:")
        
        # 确保total_size也是Decimal以便进行除法运算
        if total_size > Decimal('0.0'):
            # Decimal之间运算结果仍是Decimal
            data_percentage = (total_data_size / total_size * Decimal('100.0'))
            index_percentage = (total_index_size / total_size * Decimal('100.0'))
            result_lines.append(f"数据空间: {total_data_size:.2f} MB ({data_percentage:.1f}%)")
            result_lines.append(f"索引空间: {total_index_size:.2f} MB ({index_percentage:.1f}%)")
        else:
            result_lines.append(f"数据空间: {total_data_size:.2f} MB (0.0%)")
            result_lines.append(f"索引空间: {total_index_size:.2f} MB (0.0%)")

        if tables_info:
            valid_tables = [t for t in tables_info if t.get('TABLE_NAME')]
            if valid_tables:
                largest_table = valid_tables[0]
                largest_table_name = largest_table.get('TABLE_NAME', '未知表')
                # total_size_mb 也是 Decimal
                largest_table_size = largest_table.get('total_size_mb', Decimal('0.0'))
                result_lines.append(f"最大的表: {largest_table_name} ({largest_table_size:.2f} MB)")
            else:
                result_lines.append(f"最大的表: 未知 (无法识别有效表)")
        
        return "\n".join(result_lines)
        
    except Exception as e:
        print(f"在get_database_stats中发生意外错误: {e}")
        return f"错误: 在获取数据库统计信息时发生意外错误: {e}"
    finally:
        if conn:
            conn.close()

