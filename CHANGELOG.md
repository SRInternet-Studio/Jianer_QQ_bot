# 更新日志

## 未发布

### 新增
- JianerMate 新增 `~模型` / `~模型 <代码>` / `~模型 重置`：列出可用模型并把本会话回复模型切换成任意原生或桥接模型，覆盖值存在 `mate.db` 的 `state` 表里，只影响当前群/私聊且重启后仍然生效。
- 新增目录插件 `plugins/JianerMate/`（`jianerbot-plugin-jianer-mate`）：单人设群聊 AI，支持群内 @/私聊被动对话、按群开启的主动发言、短句多段回复与拟人化后处理（错别字自纠、颜文字/口癖）、全局表情包库（自动收集、视觉挑选、`~偷表情`/`~表情包` 管理）、MaiBot 风格人格（备用说话风格、群聊/私聊提示词、别名点名）、旧预设用 LLM 转写为人格（`~人格 转换/导入/重置`）；模型复用 `aiconfig`，非 OpenAI 兼容条目（Gemini/Anthropic/Responses）通过 JianerAI Provider 层桥接；长期记忆默认走 memorix（缺 embedding 配置自动回退旧存储）并在回复后自动审查写入，工具侧接入内置工具、只读平台 API、Agent Skills、MCP、第三方插件工具、`bash`（人工审批）与 `spawn_subagents`；支持 TTS 语音回复与回复后缀；拥有独立插件配置文件 `data/jianer_mate/config.json`（优先于宿主 `config.others` 的同名键）。
- `web_search` 支持模型用 `backend` 指定搜索后端；当前模型为 DeepSeek 官方 API 时自动改用 DeepSeek 网页搜索；DDGS 超时或网络错误后按智谱、博查、Bing 中国顺序自动回退，新增 `WEB_SEARCH_FALLBACK_BACKENDS`、`ZHIPUAI_API_KEY`、`BOCHA_API_KEY` 配置。
- 新增目录插件 `plugins/Wordle/`（参考 `nonebot-plugin-wordle`，MIT）：`~wordle [-l 长度] [-d 词典]` 开局、`~wordle 猜 <单词>` 竞猜、`~wordle 提示/状态/结束`；群内共享一局、任何成员都可猜，开局后也可直接发送单词、提示或结束；用 PIL 渲染棋盘，内置多套词典与释义，长度 3~8、最多“长度+1”次机会，10 分钟超时自动结束。
- 移植 `nonebot-plugin-maimaidx` v3.1.4 为 JianerCore 目录插件 `plugins/MaimaiDX/`，覆盖成绩、曲目、别名、猜歌、表格、数据源和主题等命令，并保留上游 MIT 许可与绘图署名。
- 新增水鱼设备 OAuth、落雪 OAuth、SSE 别名推送与 Yuri-YuzuChaN 数据源适配；令牌使用 Windows DPAPI 和受限 ACL 保护，运行时凭据统一从 `.env` 读取。
- 首次启动缺少静态资源时在后台自动下载基础包与 1.56 增量包，校验后安全解压，并按配置自动安装 Playwright Chromium；文字功能不等待资源下载完成。
- 为 JianerAI 注册 MaimaiDX B50、曲目搜索/详情、单曲成绩与 Rating 排行榜工具；图片工具按当前适配器能力过滤，并限制他人查询只能使用公开数据。
- 新增脱敏的 `config.example.json`，提供 OneBot、Milky、Kritor、飞书和 AI 基础配置结构。

### 变更
- JianerMate 模型来源改为可多目录：新增 `jianer_mate_ai_config_dirs`（插件配置里的 `ai_config_dirs`），
  除默认 `aiconfig` 外还能并列加载其它 `*.ai.json` 目录，同名模型以先加载的为准并给出重复提示；
  `planner/replyer` 留空时按宿主 `default_mode` 自动选择，启动日志分别列出原生模型、桥接模型与已加载但不支持的条目。
- 舞萌DX 的 AI 工具宿主改为依次查找 JianerAI 与 JianerMate，切换到 JianerMate 后 `b50`、查歌、猜歌等 AI 工具依旧可用。
- 舞萌DX 不再依赖 JianerAI 插件：移除插件依赖声明，未启用或尚未就绪时跳过 AI 工具注册并在监听启动后重试，命令功能与 `@机器人` 消息让位行为保持不变。
- 舞萌DX 插件不再响应群聊中 `@机器人` 的消息，这类消息继续交给 JianerAI 处理；`b50 @成员` 等指定其他群成员的查询不受影响。
- 网页搜索提供方改由宿主配置 `WEB_SEARCH_PROVIDER` 决定（`ddgs` 默认，或 `deepseek` 官方网页搜索 API `POST /v1/tools/search`），不再按当前对话模型的 BaseUrl 自动切换；`deepseek` 也可作为 DDGS 回退链的一环，密钥读取 `DEEPSEEK_API_KEY`。
- `web_search` 的 DDGS 默认后端由 `yandex` 改为 `bing`。
- `web_browser` 移除全部访问约束：不再校验 URL，可访问 `localhost`、内网地址和 `file://`，并放开媒体、字体、WebSocket、Service Worker 与下载拦截。
- Bot 框架从项目内置旧框架切换为 PyPI 包 `jianer-bot`（导入包名 `jianer`），主程序、AI 核心、工具模块和前置逻辑统一改用 JianerCore API。
- 移除项目自托管插件加载器/派发器，插件加载与派发改由 JianerCore `PluginManager` 负责。
- AI 对话、模型/角色切换、短期上下文、TTS、回复后缀和简儿记忆迁入目录插件 `plugins/JianerAI/`。
- 旧 `AI_bot/`、ARC AI 桥接、旧 parser、旧记忆服务与宿主 AI fallback 已删除。
- 旧 `jianer_memory.db` 已通过可演练迁移器导入规范化 `jianer_ai.db`；支持 dry-run、stage、校验、切换和回滚。
- 当前仓库命令插件全部改写为 `PluginMetadata + Alconna Command` 新式插件，并按 `jianerbot-plugin-{name}` 规则声明插件 ID。
- 插件运行时重载采用候选加载、原子交换、旧 generation 关闭；正常停止会等待插件任务与资源释放。
- 插件订阅事件纳入宿主统一派发，热重载后会重放 listener-start 生命周期，并回收旧代 Alconna 命令注册。
- JianerAI Tool 上下文增加当前会话历史和会话发送者查询，供工具在不猜测身份的前提下解析群名片或昵称。
- `config.json`、`.codex/`、`.agent/`、`.agents/` 和 `AGENTS.md` 改为仅本地使用，不再纳入版本控制。
- `requirements.txt` 将 JianerCore 下限锁定为 `jianer-bot>=0.92.2`，并补充 MaimaiDX 所需的异步 IO、数据模型、绘图与 `py7zr` 依赖。

### 行为变更（插件开发者请关注）
- 旧式关键词函数插件契约不再作为项目插件接口；命令插件必须声明 `__plugin_meta__ = PluginMetadata(...)` 并使用 `@Command(...).handle()`。
- 非命令事件插件可提供 `on_message_observe`、`on_message` 与 `on_message_fallback`；宿主固定按 observe → normal → fallback 派发。
- 插件如需读取项目运行时上下文，使用 `bot.plugin_state.get_runtime()`，不再依赖 stage/order 兼容状态。
- `插件视角` 现在展示 JianerCore 插件 ID、禁用项、加载失败和加载警告。
- QQ 群聊只接受前缀触发 AI，`@机器人 + 文本` 不触发；后缀只应用于 AI 回复。
- 飞书绑定以 JSON 为权威源，通过持久 outbox 幂等合并 AI 身份；回调失败不回滚非 AI 绑定。

### 修复
- JianerMate 的后台任务（表情包收集、主动发言、记忆审查、第三方工具加载）现在会记录异常，不再是静默消失的 `Task exception was never retrieved`。
- 修复 JianerMate 被动回复整体不可用：`on_message_fallback` 调用了不存在的 `_has_media`，群里只有 `@` 或只发图片时直接抛 `AttributeError` 并中断派发；现在补齐该方法，纯 `@` 走「只是 @ 了你」提示，纯媒体按图片/语音/视频判定。
- `~人格 转换` 遇到模型返回非 JSON 时不再只回一句「模型返回的 JSON 无法解析」：先以更严格的“只输出 JSON”提示重试一次，仍失败时提示所用模型并建议换模型或缩短设定；JSON 解析同时容忍对象/数组结尾的多余逗号。
- 修复 Gemini 400 `any_of` schema 报错：构造 functionDeclarations 时把 `anyOf`/`oneOf` 的同级字段（`type`、`properties`、`required`、`description` 等）合并进各分支，OneBot、Milky 与 MCP 工具声明不再与 Gemini 限制冲突。
- 修复 embedding 接口 512 token 上限导致的 HTTP 400：独立 embedding 端点返回 input too long 时，超长文本自动按自然边界切分、长度加权合并并重新归一化向量，仍超限时逐级截断兜底。
- GitHub 额度用尽提示补充 `GITHUB_TOKEN` 指引；episode 分段失败日志附带底层错误原因。
- `web_search` 不再硬编码 DDGS 引擎列表，改为读取当前安装的 `ddgs` 实际启用的 text 引擎；`ddgs` 9.16 起 text 引擎无 `bing`，此时 `bing` 自动改用内置直连 Bing，未启用的引擎直接报错而不是静默回落到 `auto`。
- MaimaiDX 自动资源安装改用持久缓存和断点续传，修复大文件下载中断后重头开始；兼容资源包多一层目录及 1.56 增量素材覆盖到 `mai/pic`。
- JianerAI 在 OpenAI 协议（Chat Completions / Responses）发送图片前统一归一化：动图只取第一帧，最长边压到 2048，编码为静态 PNG 或 JPEG，单张上限 2 MiB。此前 GIF 会被中转站以上游 400 拒收、大体积 base64 请求会超时，导致带图的一轮直接回「暂时无法连接AI服务」。
- Pixiv 生图插件的 `generating` 状态改为由 `plugin_state` 维护，避免派发参数副本导致并发状态丢失。
- `broadcast.send_msg_all_groups` 在 async 上下文里改用 `await asyncio.sleep`，不再阻塞事件循环。
- `broadcast.timing_message_loop` 修复在外层含 `⊕` 但首行不含的分支下 `time_part` 未定义可能引发的 NameError。
- 持久化层（`auth_store / feishu_bindings / help_mode`）写入失败改为 `logger.exception` 而非静默 `pass`。
- 删除记忆写入 suppression tombstone，防止后台提炼或并发生成把已删除事实重新写回。
- QQ 插件命令文本只拼接文本段，保留原始 `@` 段供 MaimaiDX 等插件精确识别目标成员。
- 热重载完成后重新对齐 Arclet Alconna 命令注册，避免旧 generation 释放后计数泄漏并最终触发 `ExceedMaxCount`。
- 名片点赞仅在 OneBot 协议执行，并改为一次请求提交每日点赞数量，避免在不支持的协议上重复调用。

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
