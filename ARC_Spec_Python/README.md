# ARC_Spec_Python

一个模块化的AI配置管理和解析器框架，用于动态加载和管理不同类型的AI模型配置。

## ✨ 特性

- 🔧 **模块化架构**: 配置管理、解析器管理和CLI界面完全分离
- 🚀 **动态加载**: 自动发现和注册解析器，支持热插拔
- 📝 **配置验证**: 完整的配置文件格式验证和错误提示
- 🎯 **易于扩展**: 简单的接口设计，方便创建自定义解析器

- 📚 **完整API**: 提供完整的编程接口，方便集成到其他项目

## 🏗️ 架构概览

```
ARC_Spec_Python/

├── arcspec_ai/                   # 核心包
│   ├── configurator/             # 配置管理模块
│   │   ├── aiconfig/             # AI配置管理
│   │   │   ├── __init__.py
│   │   │   └── loader.py
│   │   ├── parser/               # 解析器管理
│   │   │   ├── __init__.py
│   │   │   └── manager.py
│   │   └── __init__.py
│   ├── parsers/                  # 解析器实现
│   │   ├── base.py               # 基础解析器类
│   │   ├── openai.py             # OpenAI解析器
│   │   ├── mycostom.py           # 自定义解析器示例
│   │   └── registry.py           # 解析器注册表
│   ├── ui/                       # 用户界面
│   └── utils/                    # 工具函数
└── configs/                      # 配置文件目录
    └── *.ai.json                 # AI配置文件
```

## 📦 安装

### 环境要求

- Python 3.8+
- 支持的操作系统：Windows、macOS、Linux

### 安装步骤

1. **克隆项目**
   ```bash
   git clone <repository-url>
   cd ARC_Spec_Python
   ```

2. **安装依赖**
   ```bash
   pip install -r requirements.txt
   ```
   
   或使用uv（推荐）：
   ```bash
   uv sync
   ```

3. **验证安装**
   运行最小示例脚本验证库用法：
   ```python
   from arcspec_ai.configurator import load_ai_configs, load_parsers
   configs = load_ai_configs('configs')
   registry = load_parsers('arcspec_ai/parsers')
   name = next(iter(configs))
   parser = registry.create_parser(configs[name]['ResponseType'], configs[name])
   print(parser.chat("你好")) if parser else print("解析器创建失败")
   ```

## 🚀 快速开始


### 编程接口使用

```python
from arcspec_ai.configurator import load_ai_configs, load_parsers

# 加载AI配置
configs = load_ai_configs('configs')
print(f"加载了 {len(configs)} 个配置文件")

# 加载解析器
parser_registry = load_parsers('arcspec_ai/parsers')
print(f"发现了 {len(parser_registry.list_parsers())} 个解析器")

# 创建解析器实例
config = configs['mycostom']  # 使用mycostom配置
parser = parser_registry.create_parser(config['ResponseType'], config)

# 使用解析器
if parser:
    response = parser.parse("Hello, AI!")
    print(response)
```

## 📝 配置文件格式

配置文件使用JSON格式，扩展名为`.ai.json`：

```json
{
  "FriendlyName": "我的自定义AI",
  "Model": "custom-model-v1",
  "Introduction": "这是一个自定义AI配置",
  "ResponseType": "mycostom",
  "Temperature": 0.7,
  "MaxTokens": 1000,
  "Personality": "友好且专业的AI助手",
  "max_history_tokens": 2000,
  "max_history_messages": 15,
  "it_multimodal_model": "false"
}
```

### 必需字段

| 字段 | 类型 | 描述 |
|------|------|------|
| `FriendlyName` | string | 配置显示名称 |
| `Model` | string | 模型标识符 |
| `ResponseType` | string | 解析器类型 |
| `Temperature` | number | 温度参数 (0.0-2.0) |
| `MaxTokens` | integer | 最大令牌数 |

### 可选字段

| 字段 | 类型 | 描述 |
|------|------|------|
| `Introduction` | string | 配置描述 |
| `Personality` | string | AI人格设定 |
| `max_history_tokens` | integer | 历史记录最大令牌数 |
| `max_history_messages` | integer | 历史记录最大消息数 |
| `it_multimodal_model` | string | 是否为多模态模型 |

## 🔧 创建自定义解析器

1. **继承BaseParser类**
   ```python
   from arcspec_ai.parsers.base import BaseParser
   
   class MyCustomParser(BaseParser):
       PARSER_NAME = "mycustom"
       PARSER_DESCRIPTION = "我的自定义解析器"
       PARSER_ALIASES = ["custom", "my"]
       
       def parse(self, user_input: str) -> str:
           # 实现解析逻辑
           return f"处理结果: {user_input}"
       
       def get_model_info(self) -> dict:
           return {
               "name": self.config.get("Model", "Unknown"),
               "type": "Custom Parser"
           }
   ```

2. **保存到parsers目录**
   - 将文件保存为`arcspec_ai/parsers/my_custom.py`
   - 系统会自动发现并注册解析器

3. **创建对应配置文件**
   ```json
   {
     "FriendlyName": "我的自定义解析器",
     "Model": "my-custom-v1",
     "ResponseType": "mycustom",
     "Temperature": 0.7,
     "MaxTokens": 1000
   }
   ```

## 🔍 调试和日志

### 启用调试模式

```python
import logging
logging.basicConfig(level=logging.DEBUG)
# 在你的脚本中启用更详细日志
```

### 查看日志文件

日志文件位于项目根目录的`arcspec_ai.log`，包含详细的运行信息和错误记录。

## 🤝 贡献指南

1. Fork 项目
2. 创建特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 打开 Pull Request

### 开发环境设置

```bash
# 安装开发依赖
pip install -e .

# 运行测试
python -m pytest

# 代码格式化
black .
flake8 .
```

## 📄 许可证

本项目采用 MIT 许可证 - 查看 [LICENSE](LICENSE) 文件了解详情。

## 🆘 获取帮助

- 📖 查看 [API使用教程](API_USAGE.md) 了解详细的编程接口
- 🐛 [提交Issue](../../issues) 报告问题或建议
- 💬 [讨论区](../../discussions) 参与社区讨论

## 🔗 相关链接

- [技术架构文档](.trae/documents/ARC_Spec_Python_技术架构文档.md)
- [重构需求文档](.trae/documents/ARC_Spec_Python_重构需求文档.md)
- [API使用教程](API_USAGE.md)

---

**ARC_Spec_Python** - 让AI配置管理变得简单而强大！ 🚀



