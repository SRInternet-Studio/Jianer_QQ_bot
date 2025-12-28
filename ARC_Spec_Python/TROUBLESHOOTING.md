# ARC_Spec_Python 故障排除指南

本指南帮助您解决在使用ARC_Spec_Python时可能遇到的常见问题。

## 📋 目录

- [安装问题](#安装问题)
- [配置问题](#配置问题)
- [解析器问题](#解析器问题)
- [运行时错误](#运行时错误)
- [性能问题](#性能问题)
- [集成问题](#集成问题)
- [调试技巧](#调试技巧)
- [常见错误代码](#常见错误代码)
- [获取帮助](#获取帮助)

## 🔧 安装问题

### 问题1: 导入模块失败

**错误信息:**
```
ImportError: No module named 'arcspec_ai'
ModuleNotFoundError: No module named 'arcspec_ai.configurator'
```

**可能原因:**
- 项目路径未添加到Python路径
- 项目结构不完整
- Python环境问题

**解决方案:**

1. **检查项目结构:**
   ```bash
   ls -la
   # 应该看到 arcspec_ai/ 目录
   ls -la arcspec_ai/
   # 应该看到 configurator/ 目录
   ```

2. **添加项目路径:**
   ```python
   import sys
   from pathlib import Path
   
   # 方法1: 相对路径
   project_root = Path(__file__).parent.parent
   sys.path.insert(0, str(project_root))
   
   # 方法2: 绝对路径
   sys.path.insert(0, '/path/to/ARC_Spec_Python')
   
   # 然后导入
   from arcspec_ai.configurator import load_ai_configs
   ```

3. **检查Python环境:**
   ```bash
   python --version
   which python
   pip list
   ```

### 问题2: 依赖库缺失

**错误信息:**
```
ModuleNotFoundError: No module named 'requests'
ModuleNotFoundError: No module named 'openai'
```

**解决方案:**

1. **安装基础依赖:**
   ```bash
   pip install requests openai anthropic google-generativeai
   ```

2. **使用requirements.txt:**
   ```bash
   pip install -r requirements.txt
   ```

3. **检查虚拟环境:**
   ```bash
   # 激活虚拟环境
   source venv/bin/activate  # Linux/Mac
   venv\Scripts\activate     # Windows
   
   # 然后安装依赖
   pip install -r requirements.txt
   ```

## ⚙️ 配置问题

### 问题1: 未找到配置文件

**错误信息:**
```
未找到任何配置文件
No configuration files found in directory
```

**解决方案:**

1. **检查配置目录:**
   ```bash
   ls -la configs/
   # 应该看到 .ai.json 文件
   ```

2. **检查文件扩展名:**
   ```bash
   # 正确的文件名格式
   mycustom.ai.json
   openai_gpt4.ai.json
   
   # 错误的格式
   mycustom.json
   config.ai.txt
   ```

3. **创建示例配置:**
   ```json
   {
     "FriendlyName": "我的自定义配置",
     "Model": "gpt-3.5-turbo",
     "ResponseType": "OpenAI",
     "Description": "基于OpenAI的聊天配置",
     "Parameters": {
       "api_key": "your-api-key-here",
       "base_url": "https://api.openai.com/v1",
       "max_tokens": 1000,
       "temperature": 0.7
     }
   }
   ```

### 问题2: 配置文件格式错误

**错误信息:**
```
JSON decode error
配置验证失败
Invalid configuration format
```

**解决方案:**

1. **验证JSON格式:**
   ```bash
   # 使用在线JSON验证器或命令行工具
   python -m json.tool configs/mycustom.ai.json
   ```

2. **检查必需字段:**
   ```json
   {
     "FriendlyName": "必需字段",
     "Model": "必需字段",
     "ResponseType": "必需字段",
     "Parameters": {
       "api_key": "必需字段"
     }
   }
   ```

3. **常见格式错误:**
   ```json
   // 错误: 尾随逗号
   {
     "Model": "gpt-3.5-turbo",
   }
   
   // 错误: 单引号
   {
     'Model': 'gpt-3.5-turbo'
   }
   
   // 正确格式
   {
     "Model": "gpt-3.5-turbo"
   }
   ```

### 问题3: API密钥问题

**错误信息:**
```
Authentication failed
Invalid API key
API key not found
```

**解决方案:**

1. **检查API密钥格式:**
   ```json
   {
     "Parameters": {
       "api_key": "sk-..."  // OpenAI格式
       // 或
       "api_key": "sk-ant-..."  // Anthropic格式
     }
   }
   ```

2. **使用环境变量:**
   ```bash
   # 设置环境变量
   export OPENAI_API_KEY="your-key-here"
   export ANTHROPIC_API_KEY="your-key-here"
   ```

3. **验证API密钥:**
   ```python
   import requests
   
   # 测试OpenAI API
   headers = {"Authorization": f"Bearer {api_key}"}
   response = requests.get(
       "https://api.openai.com/v1/models", 
       headers=headers
   )
   print(response.status_code)
   ```

## 🔍 解析器问题

### 问题1: 解析器未找到

**错误信息:**
```
解析器 'OpenAI' 不可用
Parser not found for ResponseType
```

**解决方案:**

1. **检查解析器目录:**
   ```bash
   ls -la arcspec_ai/parsers/
   # 应该看到对应的解析器文件
   ```

2. **检查解析器注册:**
   ```python
   from arcspec_ai.configurator import load_parsers
   
   parser_registry = load_parsers('./arcspec_ai/parsers')
   print("可用解析器:", parser_registry.list_parsers())
   ```

3. **检查ResponseType匹配:**
   ```json
   // 配置文件中的ResponseType必须与解析器类名匹配
   {
     "ResponseType": "OpenAI"  // 必须有对应的OpenAI解析器类
   }
   ```

### 问题2: 解析器初始化失败

**错误信息:**
```
解析器初始化失败
Parser initialization error
```

**解决方案:**

1. **检查解析器实现:**
   ```python
   # 确保解析器继承自BaseParser
   from arcspec_ai.parsers.base_parser import BaseParser
   
   class MyParser(BaseParser):
       def __init__(self, config):
           super().__init__(config)
           # 初始化逻辑
   ```

2. **检查配置参数:**
   ```python
   # 确保所需参数都在配置中
   required_params = ['api_key', 'model']
   for param in required_params:
       if param not in config.get('Parameters', {}):
           raise ValueError(f"缺少必需参数: {param}")
   ```

## 🚨 运行时错误

### 问题1: 网络连接错误

**错误信息:**
```
ConnectionError: Failed to establish connection
Timeout error
SSL certificate verify failed
```

**解决方案:**

1. **检查网络连接:**
   ```bash
   ping api.openai.com
   curl -I https://api.openai.com/v1/models
   ```

2. **配置代理:**
   ```python
   import os
   
   # 设置代理
   os.environ['HTTP_PROXY'] = 'http://proxy.company.com:8080'
   os.environ['HTTPS_PROXY'] = 'https://proxy.company.com:8080'
   ```

3. **SSL问题:**
   ```python
   import ssl
   import requests
   
   # 临时禁用SSL验证（不推荐用于生产环境）
   requests.packages.urllib3.disable_warnings()
   session = requests.Session()
   session.verify = False
   ```

### 问题2: 内存不足

**错误信息:**
```
MemoryError
Out of memory
```

**解决方案:**

1. **减少并发数:**
   ```python
   # 减少线程池大小
   with ThreadPoolExecutor(max_workers=2) as executor:
       # 处理任务
   ```

2. **批量处理:**
   ```python
   # 分批处理大量数据
   batch_size = 10
   for i in range(0, len(data), batch_size):
       batch = data[i:i+batch_size]
       process_batch(batch)
   ```

3. **清理资源:**
   ```python
   import gc
   
   # 手动垃圾回收
   gc.collect()
   ```

### 问题3: 编码问题

**错误信息:**
```
UnicodeDecodeError
UnicodeEncodeError
```

**解决方案:**

1. **指定编码:**
   ```python
   # 读取文件时指定编码
   with open('file.txt', 'r', encoding='utf-8') as f:
       content = f.read()
   
   # 写入文件时指定编码
   with open('output.txt', 'w', encoding='utf-8') as f:
       f.write(content)
   ```

2. **处理编码错误:**
   ```python
   # 忽略编码错误
   text = text.encode('utf-8', errors='ignore').decode('utf-8')
   
   # 替换编码错误
   text = text.encode('utf-8', errors='replace').decode('utf-8')
   ```

## ⚡ 性能问题

### 问题1: 响应速度慢

**可能原因:**
- 网络延迟
- API限制
- 配置不当

**解决方案:**

1. **优化配置:**
   ```json
   {
     "Parameters": {
       "max_tokens": 500,     // 减少token数量
       "temperature": 0.3,    // 降低随机性
       "timeout": 30          // 设置超时
     }
   }
   ```

2. **使用缓存:**
   ```python
   from functools import lru_cache
   
   @lru_cache(maxsize=100)
   def cached_parse(message):
       return parser.parse(message)
   ```

3. **并发处理:**
   ```python
   import asyncio
   
   async def process_concurrent(messages):
       tasks = [process_message(msg) for msg in messages]
       results = await asyncio.gather(*tasks)
       return results
   ```

### 问题2: 内存使用过高

**解决方案:**

1. **监控内存使用:**
   ```python
   import psutil
   import os
   
   process = psutil.Process(os.getpid())
   memory_usage = process.memory_info().rss / 1024 / 1024  # MB
   print(f"内存使用: {memory_usage:.2f} MB")
   ```

2. **优化数据结构:**
   ```python
   # 使用生成器而不是列表
   def process_large_file(filename):
       with open(filename, 'r') as f:
           for line in f:  # 逐行处理，不加载整个文件
               yield process_line(line)
   ```

## 🔗 集成问题

### 问题1: Flask集成问题

**错误信息:**
```
RuntimeError: Working outside of application context
```

**解决方案:**

```python
from flask import Flask, g

app = Flask(__name__)

# 在应用上下文中初始化
with app.app_context():
    g.ai_backend = initialize_ai_backend()

@app.route('/chat', methods=['POST'])
def chat():
    # 使用应用上下文中的后端
    backend = g.get('ai_backend')
    if not backend:
        backend = initialize_ai_backend()
        g.ai_backend = backend
    
    return backend.process(request.json['message'])
```

### 问题2: 异步集成问题

**错误信息:**
```
RuntimeError: This event loop is already running
```

**解决方案:**

```python
import asyncio
import nest_asyncio

# 允许嵌套事件循环
nest_asyncio.apply()

# 或者使用线程池
from concurrent.futures import ThreadPoolExecutor

async def async_parse(message, parser):
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, parser.parse, message
    )
    return result
```

## 🐛 调试技巧

### 1. 启用详细日志

```python
import logging

# 设置日志级别
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# 为特定模块设置日志级别
logging.getLogger('arcspec_ai').setLevel(logging.DEBUG)
logging.getLogger('requests').setLevel(logging.WARNING)
```

### 2. 使用调试模式

```python
# 在代码中添加调试信息
def debug_config_loading():
    print("=== 调试信息 ===")
    print(f"当前工作目录: {os.getcwd()}")
    print(f"Python路径: {sys.path}")
    print(f"配置目录: {config_dir}")
    print(f"配置文件: {list(Path(config_dir).glob('*.ai.json'))}")
    print("===============")

debug_config_loading()
```

### 3. 单步测试

```python
# 分步测试每个组件
def test_components():
    # 1. 测试配置加载
    try:
        configs = load_ai_configs('./configs')
        print(f"✓ 配置加载成功: {len(configs)} 个配置")
    except Exception as e:
        print(f"✗ 配置加载失败: {e}")
        return
    
    # 2. 测试解析器加载
    try:
        parser_registry = load_parsers('./arcspec_ai/parsers')
        print(f"✓ 解析器加载成功: {parser_registry.list_parsers()}")
    except Exception as e:
        print(f"✗ 解析器加载失败: {e}")
        return
    
    # 3. 测试解析器创建
    for config_name, config in configs.items():
        try:
            parser = parser_registry.create_parser(
                config['ResponseType'], config
            )
            print(f"✓ 解析器 {config_name} 创建成功")
        except Exception as e:
            print(f"✗ 解析器 {config_name} 创建失败: {e}")

test_components()
```

### 4. 性能分析

```python
import time
import cProfile

# 简单计时
start_time = time.time()
result = parser.parse(message)
end_time = time.time()
print(f"处理时间: {end_time - start_time:.2f}s")

# 详细性能分析
def profile_parsing():
    profiler = cProfile.Profile()
    profiler.enable()
    
    result = parser.parse(message)
    
    profiler.disable()
    profiler.print_stats(sort='cumulative')

profile_parsing()
```

## 📊 常见错误代码

| 错误代码 | 描述 | 解决方案 |
|---------|------|----------|
| CONFIG_001 | 配置文件未找到 | 检查配置目录和文件名 |
| CONFIG_002 | 配置格式错误 | 验证JSON格式 |
| CONFIG_003 | 必需字段缺失 | 添加必需的配置字段 |
| PARSER_001 | 解析器未找到 | 检查ResponseType和解析器实现 |
| PARSER_002 | 解析器初始化失败 | 检查配置参数和依赖 |
| API_001 | API密钥无效 | 验证API密钥格式和权限 |
| API_002 | 网络连接失败 | 检查网络和代理设置 |
| API_003 | 请求超时 | 增加超时时间或优化请求 |
| RUNTIME_001 | 内存不足 | 减少并发数或优化内存使用 |
| RUNTIME_002 | 编码错误 | 指定正确的文件编码 |

## 🆘 获取帮助

### 1. 检查日志

首先查看详细的错误日志：

```python
import logging

# 启用详细日志
logging.basicConfig(level=logging.DEBUG)

# 运行你的代码
# 查看输出的详细信息
```

### 2. 创建最小复现示例

创建一个最简单的示例来复现问题：

```python
#!/usr/bin/env python3
# minimal_example.py

import sys
from pathlib import Path

# 添加项目路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

try:
    from arcspec_ai.configurator import load_ai_configs, load_parsers
    
    # 加载配置
    configs = load_ai_configs('./configs')
    print(f"加载的配置: {list(configs.keys())}")
    
    # 加载解析器
    parser_registry = load_parsers('./arcspec_ai/parsers')
    print(f"可用解析器: {parser_registry.list_parsers()}")
    
    # 测试解析
    if configs:
        config_name = list(configs.keys())[0]
        config = configs[config_name]
        parser = parser_registry.create_parser(
            config['ResponseType'], config
        )
        result = parser.parse("Hello, world!")
        print(f"解析结果: {result}")
    
except Exception as e:
    import traceback
    print(f"错误: {e}")
    print("详细错误信息:")
    traceback.print_exc()
```

### 3. 收集系统信息

```python
#!/usr/bin/env python3
# system_info.py

import sys
import os
import platform
from pathlib import Path

print("=== 系统信息 ===")
print(f"操作系统: {platform.system()} {platform.release()}")
print(f"Python版本: {sys.version}")
print(f"当前工作目录: {os.getcwd()}")
print(f"Python路径: {sys.path[:3]}...")  # 只显示前3个路径

print("\n=== 项目结构 ===")
project_root = Path('.')
for item in project_root.iterdir():
    if item.is_dir():
        print(f"📁 {item.name}/")
    else:
        print(f"📄 {item.name}")

print("\n=== 配置文件 ===")
config_dir = project_root / 'configs'
if config_dir.exists():
    for config_file in config_dir.glob('*.ai.json'):
        print(f"📄 {config_file.name}")
else:
    print("❌ 配置目录不存在")

print("\n=== 解析器文件 ===")
parser_dir = project_root / 'arcspec_ai' / 'parsers'
if parser_dir.exists():
    for parser_file in parser_dir.glob('*.py'):
        if parser_file.name != '__init__.py':
            print(f"📄 {parser_file.name}")
else:
    print("❌ 解析器目录不存在")

print("\n=== 已安装的包 ===")
try:
    import pkg_resources
    installed_packages = [d.project_name for d in pkg_resources.working_set]
    relevant_packages = [p for p in installed_packages if any(
        keyword in p.lower() for keyword in ['openai', 'anthropic', 'google', 'requests']
    )]
    for package in relevant_packages:
        print(f"📦 {package}")
except:
    print("无法获取包信息")
```

### 4. 社区支持

- **GitHub Issues**: 在项目仓库中创建issue
- **文档**: 查看项目文档和API参考
- **示例代码**: 参考examples目录中的示例

### 5. 报告Bug时请包含

1. **错误描述**: 详细描述问题
2. **复现步骤**: 如何重现问题
3. **期望行为**: 期望的正确行为
4. **实际行为**: 实际发生的情况
5. **环境信息**: 系统信息脚本的输出
6. **最小示例**: 能复现问题的最小代码
7. **错误日志**: 完整的错误堆栈信息

---

**记住**: 大多数问题都可以通过仔细检查配置文件、确保依赖正确安装、以及启用详细日志来解决。如果问题仍然存在，不要犹豫寻求帮助！