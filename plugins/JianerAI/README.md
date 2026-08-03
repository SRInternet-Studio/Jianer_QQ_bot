# JianerAI

`jianerbot-plugin-jianer-ai` 是简儿的 JianerCore 目录插件，统一承载 AI
对话、模型与角色切换、短期上下文、简儿记忆、回复后缀、TTS、图片与引用
解析，以及受限的 Agent 工具调用。入口固定为 `setup.py`，依赖内置
`jianerbot-plugin-alconna`。

## 触发规则

- 群聊：消息必须以宿主 `reminder`（通常为 `~`）开头。
- QQ（OneBot/Milky）：`@机器人 + 文本` 不触发 AI。
- 私聊：普通文本直接进入 AI；带前缀的插件命令仍由 Alconna 处理。
- 飞书：支持私聊和群内 mention；不声明或模拟原生合并转发能力。
- `config.black_list` 只限制 JianerAI，不影响其它宿主或业务插件。

模型、角色和 TTS 按完整会话键
`(protocol, self_id, conversation_kind, conversation_id, preset)` 保存。群会话由
群成员共享；角色 preset 同时隔离短期上下文和长期记忆。长期记忆按 canonical
用户与 preset 跨私聊、群聊共享。

## 常用命令

- `~ai管理菜单`、`~切换AI [代码]`
- `~角色扮演`、`~切换角色 [名称]`
- `~添加预设 [名称] [简介] : [内容]`、`~删除预设 [名称]`
- `~简儿记忆 [帮助|状态|开启|关闭|间隔|立即生成|列表|删除|清空|恢复]`
- `~设置全局后缀`、`~删除全局后缀`
- `~设置特定后缀`、`~删除特定后缀`
- `~TTS [开启|关闭|状态]`
- `~Agent [开启|关闭|自动|状态|工具]`
- `~注销`（只清空当前会话的短期上下文）

后缀只在 `SuffixStore.apply_ai_reply()` 中应用；ping、群发、休眠等宿主消息不会
经过后缀处理。私聊 TTS 默认关闭，群聊默认开启。

## 配置与数据

插件读取宿主 `config.others`：

- `jianer_ai_db_path`：规范化 SQLite 数据库，默认 `jianer_ai.db`
- `default_mode` / `ai_default_model`：默认对话模型
- `memory_mode`：记忆提炼模型
- `memory_enabled_default`
- `memory_interval_seconds_default`
- `memory_scheduler_tick_seconds`
- `memory_min_new_rows_to_generate`
- `memory_topk`
- `memory_cleanup_keep_days`（最少 1 天，生产默认 30 天）
- `max_message_length`：单次回复最多分段数
- `ai_reply_chunk_chars`：单段目标字符数
- `TTS`：音色、语速、音量与音高
- `agent_enabled_default`：没有会话覆盖时是否启用 Agent，默认开启
- `agent_max_parallel_calls`：只读工具并发上限，默认 4
- `agent_total_timeout_seconds`：完整 Agent 轮次总超时，默认 180 秒
- `agent_allowed_tools`：允许暴露给模型的工具名称数组或逗号分隔字符串；未配置时
  允许全部内置工具，显式空数组则不暴露工具
- `agent_browser_enabled`：启用 `web_browser`，默认 `true`
- `agent_browser_headless`：使用无界面 Chromium，默认 `true`
- `agent_browser_profile_dir`：共享持久 Profile，默认 `data/jianer_browser/profile`
- `agent_browser_audit_path`：写请求脱敏审计，默认 `data/jianer_browser/audit.jsonl`
- `agent_browser_max_pages`：会话页面上限，默认且最大为 16
- `agent_browser_idle_seconds`：空闲页面回收时间，默认 900 秒

网页搜索使用 `ddgs` 文本搜索接口。宿主可通过 `DDGS_BACKEND` 环境变量选择
`auto`、`bing`、`brave`、`duckduckgo`、`google`、`grokipedia`、`mojeek`、
`wikipedia`、`yahoo` 或 `yandex`，默认 `auto`。区域固定为 `cn-zh`，安全搜索
固定为 `moderate`；模型不能指定代理、请求头、任意后端或待抓取 URL。若配置了
`agent_allowed_tools` 白名单，需要显式加入 `web_search`。

GitHub 仓库读取由 `github_repository` 提供，只连接固定的 GitHub.com REST API，
支持仓库概况、目录和 UTF-8 文本代码、提交、Pull Request 与 Issue 的只读查看。
公开仓库默认可匿名读取；宿主可通过 `GITHUB_TOKEN` 提高请求额度并访问该令牌获权
的私有仓库，仓库内代码搜索必须配置此变量。令牌不会暴露给模型或写入 AI 配置。
若配置了 `agent_allowed_tools` 白名单，需要显式加入 `github_repository`。

状态化网页操作由 `web_browser` 提供，支持打开、快照、点击、填写、选择、按键、
滚动、前进后退、刷新、等待和关闭。它使用所有 Agent 用户共享的持久 Chromium
Profile，每个会话拥有独立 Page；Cookie、localStorage 和外部登录账号会跨用户、
跨重启共享。若配置了 `agent_allowed_tools` 白名单，需要显式加入 `web_browser`。
Windows 首次运行前安装浏览器：

```powershell
python -m playwright install chromium
```

`web_browser` 属于 `ToolRisk.PRIVILEGED`，默认启用且不会为提交表单等外部副作用
进行二次确认。它拒绝本地、私有、回环、链路本地和保留地址，拦截重定向与子请求，
并禁用 Service Worker、WebSocket、文件上传、下载、媒体和字体。页面只以正文和
最新快照元素编号提供给模型，不开放 CSS、XPath、任意 JavaScript、坐标或截图。

模型配置继续从 `aiconfig/*.ai.json` 读取；角色数据继续使用
`prerequisites/current.json` 与相邻模板；后缀继续使用
`suffix_config.json`。模型配置可用 `ToolsEnabled: true/false/"auto"` 控制
Function Calling；兼容端点明确拒绝 tools 参数时，本 generation 会自动退回普通
对话。部分 DeepSeek 兼容网关把调用返回为 DSML 文本；插件会把完整合法的 DSML
转换为结构化工具调用，并拒绝把损坏的调用标记发送给用户。不要提交包含密钥的本地配置。

角色人设模板除现有的 `{self.bot_name}`、`{self.bot_name_en}`、
`{self.event_user}`、`{self.event_user_id}` 外，还可使用：

- `{agent_tools}`：当前会话实际可调用的工具名称，以逗号分隔；没有时为“无”
- `{agent_tools_info}`：当前会话实际可调用工具的描述、调用形式和参数说明；没有时为“无”

两项 Agent 变量会按 Agent 状态、当前模型的 tools 能力、当前协议/适配器能力以及
`agent_allowed_tools` 白名单动态过滤，不会列出本轮无法调用的工具。

## Agent 工具

内置工具包括当前时间、安全算术、当前发言人资料、当前会话资料、当前 canonical
用户 + preset 的长期记忆、只返回标题/URL/摘要的 `web_search`，以及只读的
`github_repository`，以及可查看和操作网页的高权限 `web_browser`。首版不向模型
开放 shell、本地文件系统、原始 WebSocket、文件上传下载、消息管理或记忆删除能力。外部工具结果作为不可信数据处理；工具
中间结果不会写入短期历史、TTS 或长期记忆，只有最终 AI 文本会进入原有回复链。
调用搜索、GitHub 或网页工具本身不代表用户要求查看来源；默认回答不展示来源或 URL，只有用户在
当前请求中明确要求来源、出处、引用、链接或参考资料时，才附上实际使用的完整 URL。

网页写请求的审计不会记录 Cookie、请求头、请求体、输入值或查询参数。填写密码框或
显式传入 `sensitive=true` 时，值会从工具结果、短期历史、SQLite 原始转录和后续
记忆输入中替换为 `[REDACTED]`。但模型提供商在执行当前轮第一次请求时仍会看到用户
原始消息；不要在群聊中发送密码。共享 Profile 也意味着所有 Agent 用户可能看到同一
外部账号内的数据，并代表该账号直接执行操作。

依赖本插件的 JianerCore 插件可以声明
`requires={"jianerbot-plugin-jianer-ai"}`，再通过 Manager 的
`get_plugin_module()` 获取本入口并调用 `register_tool(ToolSpec(...))`。注册返回的
token 应在依赖插件 shutdown 时传给 `unregister_tool()`；当前策略只会向模型暴露
允许风险等级且通过 `agent_allowed_tools` 白名单的工具。

## 和风天气 Tools

JianerAI 可按需注册 11 个只读和风天气 Tool，覆盖天气与气候相关的 23 个操作：

| Tool | operation |
| --- | --- |
| `qweather_geo` | `city_lookup`、`top_city`、`poi_lookup`、`poi_range` |
| `qweather_weather` | `current`、`daily`、`hourly` |
| `qweather_minutely` | `precipitation` |
| `qweather_warning` | `current` |
| `qweather_indices` | `forecast` |
| `qweather_air_quality` | `current`、`hourly`、`daily`、`station` |
| `qweather_time_machine` | `weather` |
| `qweather_tropical_cyclone` | `list`、`track`、`forecast` |
| `qweather_ocean` | `tide` |
| `qweather_solar_radiation` | `forecast` |
| `qweather_astronomy` | `sun`、`moon`、`solar_elevation` |

将仓库根目录的 `.env.example` 复制为 `.env`，配置以下四项：

```dotenv
QWEATHER_API_HOST=your-api-host.qweatherapi.com
QWEATHER_PROJECT_ID=your-project-id
QWEATHER_CREDENTIAL_ID=your-credential-id
QWEATHER_PRIVATE_KEY_PATH=secrets/qweather-ed25519-private.pem
```

`QWEATHER_API_HOST` 必须是控制台分配的专属 `*.qweatherapi.com` 主机；私钥必须是
PKCS#8 PEM 格式的 Ed25519 私钥。相对私钥路径从仓库根目录解析。四项完全未配置时不注册
这些 Tool；只配置一部分或私钥无效时会安全跳过，不影响 JianerAI 的其余功能。系统环境变量
优先于 `.env`。不要提交 `.env`、PEM 或 key 文件。

参数统一使用 snake_case，所有调用必须传 `operation`。列表响应支持本地 `offset`（默认 0）
和 `limit`（默认及最大 50）分页，单次结果上限为 64 KiB。若设置了
`agent_allowed_tools`，需要显式加入要开放的上述 Tool 名称。

实现范围以当前[官方 API 目录](https://dev.qweather.com/docs/api/)为准：Weather 使用坐标型
v1 接口，不包含已标记弃用的三个城市天气 v7 接口，也不包含控制台 API；分钟预报、指数、
时光机、热带气旋、潮汐和天文等尚无替代的现行 v7 接口继续提供。鉴权遵循
[JWT 规范](https://dev.qweather.com/docs/configuration/authentication/)。凡使用这些 Tool 的结果，
回复必须显示“天气服务由和风天气驱动”并链接和风天气；天气预警和空气质量还必须显示响应中
的上游归因。

## 记忆与身份

- OneBot/Milky QQ 身份自动归一到 `qq:<id>`。
- 飞书绑定以宿主 JSON 为权威源，通过持久 outbox 幂等调用插件
  `authorize()` / `merge_identity()`；AI 合并失败不回滚非 AI 绑定。
- 群原始转录物理存储一次，默认保留 30 天。
- 删除记忆会写入 canonical 用户 + preset + 内容/证据指纹墓碑；后台提炼和并发
  写入都必须经过 suppression 与 generation barrier 检查。
- `migration.py` 提供可演练的 plan、dry-run、stage、verify、switch 与 rollback
  API，用于从旧 `jianer_memory.db` 切换。

## 生命周期与媒体

插件后台任务、TTS 临时目录、数据库连接和命令 matcher 都归当前
`PluginManager` generation 管理。重载先完整加载候选 generation，原子交换后
再关闭旧 generation；加载失败时继续保留旧实例。

引用解析与媒体解析是两个独立适配器能力。媒体只接受 Core
`resolve_media()` 返回的已解析字节，并受 scheme、重定向、大小、总超时、MIME
嗅探和本地路径白名单限制。
