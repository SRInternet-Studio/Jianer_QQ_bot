# 更新日志

## JianerNext4 Dev-20260314a

### 新增
- 新增飞书平台适配器（`hyperot/LecAdapters/Feishu.py`），支持接收飞书消息并进入现有插件分发链路。
- 新增飞书事件接收双模式：
  - `long_connection`（基于 `lark-oapi` SDK 长连接）
  - `webhook`（保留原回调模式作为兼容回退）
- 新增按适配器拆分的连接配置结构 `connections`，支持 `OneBot/Milky/Kritor/Feishu` 独立连接参数。
- 新增平台级管理员配置 `Others.platform_admins`，支持分别配置 `qq/feishu` 的 `root/super/manage`。

### 变更
- 将飞书配置补充为可运行集合：`event_mode/listener_host/listener_port/event_path/default_receive_id_type`。
- 将事件上下文标准化，新增 `platform`、`session_id` 字段，便于插件按平台差异处理。
- 优化命令解析逻辑：支持 `@机器人 #指令` 场景，不再要求消息必须以提醒符开头。
- 飞书帮助消息发送改为文本分块发送，并对长消息自动切块，降低消息体过长导致发送失败的概率。
- 新增飞书合并转发实现：先发送子消息并收集 `message_id`，再调用 `merge_forward` 接口；失败自动回退普通文本发送。

### 修复
- 修复飞书环境下 `#帮助` 无回复问题（原合并转发链路在飞书侧不稳定）。
- 修复飞书发送失败时的静默异常问题，补充明确的 `code/msg` 日志输出。
- 修复飞书不支持消息段的处理：语音与引用段在飞书模式下跳过，不再输出占位文本。
- 修复飞书管理员权限识别问题，支持使用飞书 `open_id` 进行权限判定，恢复 `#重启` 等管理指令可用性。
- 修复 `config` 读取逻辑，兼容新 `connections` 与旧 `Connection` 并存场景。

## JianerNext4 Dev-20260228a

- 优化了一些问题.

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
