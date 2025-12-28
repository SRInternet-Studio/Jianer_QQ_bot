# 更新日志

## JianerNext4 Dev-20251228a

### 新增
- 新增全局/特定用户后缀能力，支持在逗号、句号、分号等标点前插入后缀，并应用到 AI 回复、欢迎语与系统通知等场景。
- 新增后缀管理指令：
  - `#设置全局后缀 <后缀>`（管理员）
  - `#删除全局后缀`（管理员）
  - `#设置特定后缀 <后缀>`（用户）
  - `#删除特定后缀`（用户）

### 变更
- GPU 监控从 `GPUtil` 切换为调用系统命令 `nvidia-smi` 获取信息，避免 Python 3.12+ 的 `distutils` 依赖问题。
- 更新 `requirements.txt`，移除 `gputil` 并补充缺失依赖。
- 将新增指令补充到帮助菜单中，便于发现与使用。

### 修复
- 修复 `SuffixManager` 未导入导致的 `NameError: name 'SuffixManager' is not defined` 启动报错。

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
