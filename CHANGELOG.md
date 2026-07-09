# 更新日志

## 未发布

### 变更
- Bot 框架从项目内置旧框架切换为 PyPI 包 `jianer-bot`（导入包名 `jianer`），主程序、AI 核心、工具模块和前置逻辑统一改用 JianerCore API。
- 移除项目自托管插件加载器/派发器，插件加载与派发改由 JianerCore `PluginManager` 负责。
- 当前仓库插件全部改写为 `PluginMetadata + dispatch(event, actions)` 新式插件，并按 `jianerbot-plugin-{name}` 规则声明插件 ID。
- `requirements.txt` 新增 `jianer-bot>=0.91.0`。

### 行为变更（插件开发者请关注）
- 旧式关键词函数插件契约不再作为项目插件接口；新增插件必须声明 `__plugin_meta__ = PluginMetadata(...)` 并暴露 `async def dispatch(event, actions)`。
- 插件如需读取项目运行时上下文，改为使用 `bot.plugin_state`，例如 `current_stage()`、`current_order()`、`get_runtime()`。
- `插件视角` 现在展示 JianerCore 插件 ID、禁用项、加载失败和加载警告。

### 修复
- Pixiv 生图插件的 `generating` 状态改为由 `plugin_state` 维护，避免派发参数副本导致并发状态丢失。
- `broadcast.send_msg_all_groups` 在 async 上下文里改用 `await asyncio.sleep`，不再阻塞事件循环。
- `broadcast.timing_message_loop` 修复在外层含 `⊕` 但首行不含的分支下 `time_part` 未定义可能引发的 NameError。
- 持久化层（`auth_store / feishu_bindings / help_mode`）写入失败改为 `logger.exception` 而非静默 `pass`。

## JianerNext4 Dev-20260116a

### 新增
- 新增“简儿记忆”长期记忆系统：采集群/私聊消息并按群/用户分表存入 SQLite（WAL）。
- 新增定时记忆生成：使用独立记忆AI（`config.json -> Others.memory_mode`）提炼增量聊天为结构化记忆并存储。
- 新增记忆检索与注入：基于当前对话筛选相关记忆（含全局记忆）并注入回复AI提示词。
- 新增记忆管理指令：`#简儿记忆 帮助/状态/开启/关闭/间隔/立即生成`。
- 新增 `memory_selftest.py` 自测脚本（无需联网调用）。

### 变更
- `config.json` 新增 `memory_*` 配置项：数据库路径、生成间隔、TopK、清理与全局优化参数等。

### 修复
- 修复 Windows 环境下测试数据库文件被占用的问题（关闭服务时释放 SQLite 连接）。

## JianerNext4 Dev-20251228a

### 新增
- 新增全局/特定用户后缀能力，支持在逗号、句号、分号等标点前插入后缀，并应用到 AI 回复、欢迎语与系统通知等场景。
- 新增后缀管理指令：
  - `#设置全局后缀 <后缀>`（管理员）
  - `#删除全局后缀`（管理员）
  - `#设置特定后缀 <后缀>`（用户）
  - `#删除特定后缀`（用户）

### 变更
- 不再使用`GPUtil`避免 Python 3.12+ 的 `distutils` 依赖问题。
- 更新 `requirements.txt`，移除 `gputil` 并补充缺失依赖。
- 将新增指令补充到帮助菜单中，便于发现与使用。

### 修复
- 修复 `SuffixManager` 未导入导致的 `NameError: name 'SuffixManager' is not defined` 启动报错。
- 修复 `GeminiParser` 无法使用人设的问题。

## JianerNext4 Dev-20251227a

### 新增
- 集成 `ARC_Spec_Python` 架构，引入 `Tools/ARC_AI.py` 作为统一的 AI 桥接模块。
- 新增 `aiconfig/` 和 `parser/` 目录，用于存放 ARC 风格的 AI 配置文件和解析器脚本。
- 新增 AI 管理指令：
  - `#ai管理菜单`：列出当前可用的 AI 模型（显示友好名称和代码）。
  - `#切换AI <config_name>`：在运行时动态切换当前使用的 AI 模型。

### 变更
- 将 `main.py` 中旧版的 AI 切换逻辑替换为基于 ARC 的路由逻辑（通过 `Tools.ARC_AI.get_response_stream` 调用）。
- 更新了 `main.py` 中的消息流处理逻辑，现在支持异步生成器 (`async generator`)。
- 优化了 Gemini 解析器的行为：
  - 支持从配置文件正确读取 `BaseUrl` 或 `BaseURL`。
  - 修复了当 Base URL 已包含 `/v1beta` 时重复拼接路径的问题。
  - 强制使用 `?key=...` 方式进行鉴权，以兼容各类第三方反代接口。
  - 在构建请求时正确包含传入的 `history` 上下文，确保多轮对话记忆正常。

### 修复
- 修复了因引用不存在的 `arcspec_ai.parsers.openai` 模块导致的 ARC 导入崩溃问题。
- 修复了在运行中的事件循环里调用 `asyncio.run()` 导致的报错，现在通过 `Tools/ARC_AI.py` 正确处理异步调用。
- 修复了移除旧版 Gemini 上下文管理器后出现的 `NameError: cmc is not defined` 错误。

### 安全
- 更新了 `.gitignore`，防止提交本地配置文件和 AI 密钥（例如 `config.json`, `aiconfig/`, `*.ai.json`）。
