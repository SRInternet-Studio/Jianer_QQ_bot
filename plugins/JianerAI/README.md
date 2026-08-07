# JianerAI

`jianerbot-plugin-jianer-ai` 是简儿的 JianerCore 目录插件，统一承载 AI
对话、模型与角色切换、短期上下文、简儿记忆、回复后缀、TTS、图片与引用
解析，以及受限的 Agent 工具调用。入口固定为 `setup.py`，依赖内置
`jianerbot-plugin-alconna`。

## 触发规则

- 群聊：必须明确 At 机器人；`At + 文本` 和裸 At 都进入 AI 对话。
- 普通 `reminder` 前缀（通常为 `~`）只用于命令，不再触发群聊 AI。
- 私聊：普通文本直接进入 AI；带前缀的插件命令仍由 Alconna 处理。
- 飞书：支持私聊和群内 mention；不声明或模拟原生合并转发能力。
- `config.black_list` 只限制 JianerAI，不影响其它宿主或业务插件。

模型、角色和 TTS 按完整会话键
`(protocol, self_id, conversation_kind, conversation_id, preset)` 保存。群会话由
群成员共享；角色 preset 同时隔离短期上下文和长期记忆。每个人设使用独立的物理
SQLite 分表，分别保存它对每个 canonical 用户、每个群的长期记忆，以及该人设在每个
会话中“用户说了什么、自己如何回答”的对话片段。个人记忆可随同一 canonical 用户跨
私聊和群聊使用；群记忆与对话片段不会跨群或跨人设读取。

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
- `content_moderation_enabled`：是否启用独立模型内容审核，默认开启
- `content_moderation_model`：审核使用的 `aiconfig` 模型代码，默认 `deepseek`；当前项目
  固定由 DeepSeek 独立审核，不跟随用户切换的主对话模型
- `content_moderation_timeout_seconds`：单次审核超时，范围 1–120 秒，默认 30 秒；
  超时、提供商失败或非法审核结果都会按 fail closed 拒绝本轮，不会绕过审核
- `memory_mode`：记忆提炼模型
- `memory_enabled_default`
- `memory_interval_seconds_default`
- `memory_scheduler_tick_seconds`
- `memory_min_new_rows_to_generate`
- `memory_topk`
- `memory_cleanup_keep_days`：原始聊天保留天数（最少 1 天，默认 90 天）；长期记忆、
  证据摘要、episode 和删除墓碑不受此项清理
- `memory_review_external_context_enabled`：是否允许把当前会话最近最多 50 条、合计
  最多 8000 字的聊天原文发送给 `memory_mode` 用作回复后记忆审查；默认开启，关闭后
  仍保存客观聊天和 episode，但不创建待审查任务。主模型使用近期上下文属于部署时单独
  确认的数据发送边界
- `max_message_length`：单次回复最多分段数
- `ai_reply_chunk_chars`：单段目标字符数
- `TTS`：音色、语速、音量与音高
- `agent_enabled_default`：没有会话覆盖时是否启用 Agent，默认开启
- `agent_max_parallel_calls`：只读工具并发上限，默认 4
- `agent_total_timeout_seconds`：完整 Agent 轮次总超时，默认 180 秒
- `agent_allowed_tools`：允许暴露给模型的工具名称数组或逗号分隔字符串；未配置时
  允许全部内置工具，显式空数组则不暴露工具；配置白名单时，记忆写入还需显式加入
  `create_my_memory` 和/或 `update_my_memory`
- `agent_browser_enabled`：启用 `web_browser`，默认 `true`
- `agent_browser_headless`：使用无界面 Chromium，默认 `true`
- `agent_browser_profile_dir`：共享持久 Profile，默认 `data/jianer_browser/profile`
- `agent_browser_audit_path`：写请求脱敏审计，默认 `data/jianer_browser/audit.jsonl`
- `agent_browser_max_pages`：会话页面上限，默认且最大为 16
- `agent_browser_idle_seconds`：空闲页面回收时间，默认 900 秒

每次 AI 对话会先安全解析引用和附件，再把当前请求、最近最多 8 条且合计最多
6000 字的短期上下文、从当前人设本地提取的最小风格标签（角色 ID/名称、自称、语气、
思考倾向与语气词）以及解析后的图片、语音或视频字节交给 `content_moderation_model`；完整
人设模板不会发送给审核模型。附件只有在审核模型所用协议支持对应格式时才能送审；不支持、
解析失败或无法送达时会按 fail closed 拒绝本轮，不会跳过审核。审核发生在主模型和任何
Agent 工具之前。审核器只接受严格 JSON，
会区分医学教育、新闻法律讨论、风险预防等正当语境与露骨色情、未成年人性内容、性剥削、
自残伤人、武器、违法犯罪、恶意网络行为、仇恨骚扰、隐私侵害和极端主义支持等请求。

命中违规时，审核模型必须按当前人设生成一段简短、自然、不复述违规细节的拒绝，并在合适时
提出安全替代方向。该请求不会进入主模型、工具调用、短期历史或回复后记忆审查；若消息已由
观察器写入客观聊天记录，其正文会替换为 `[内容已由安全审核隐藏]`。审核日志只记录模型、
分类代码、耗时和字符/附件数量，不记录审核理由或原始违规内容。拒绝文本不会再套用用户可配置
的 AI 回复后缀，避免审核后的安全文本被二次改写。若关闭
`content_moderation_enabled`，以上前置保障不会生效。

当前项目的默认主回答模型为 `grok`。为避免把完整角色模板中的敏感身体设定重复发送给
Grok 并触发兼容端点空响应，Grok 主回答使用同一份本地最小风格投影，而不是完整模板正文；
角色 ID/名称、自称、语气、思考倾向、语气词、机器人名称和本轮可用工具名仍会保留。其他
模型仍沿用完整人设模板。DeepSeek 审核配置建议使用 `OpenAI Chat Completions`、
`Temperature: 0` 和 `ToolsEnabled: false`，当前本地 `aiconfig/deepseek.ai.json` 已按此设置。
模型配置还可使用 `EmptyResponseRetries: 0|1|2`；只有一次提供商响应同时没有文本和工具调用时
才会重试同一请求，不会重新执行 Agent 工具。当前本地 Grok 配置设为 1，用于吸收兼容端点
偶发的空响应。

网页搜索使用 `ddgs` 文本搜索接口。宿主可通过 `DDGS_BACKEND` 环境变量选择
`auto`、`bing`、`brave`、`duckduckgo`、`google`、`grokipedia`、`mojeek`、
`wikipedia`、`yahoo` 或 `yandex`，默认 `yandex`；如需多引擎回退可显式设置为
`auto`。区域固定为 `cn-zh`，安全搜索
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

`ResponseType` 使用以下规范名称；旧值 `openai`、`openai-compatible`、`gemini`、
`google`、`claude` 等仍可读取：

| ResponseType | 请求协议 | 附件能力 |
| --- | --- | --- |
| `OpenAI Chat Completions` | OpenAI `chat.completions` | 图片；模型支持时可读 MP3/WAV 语音，其他音频先转 WAV；不支持视频 |
| `OpenAI Responses` | OpenAI `responses` | 原生图片；语音先走转写；视频抽取最多 8 帧并转写音轨 |
| `Google GenerateContent` | Google GenAI SDK `generate_content` | 原生图片、语音和视频 |
| `Anthropic Messages` | Anthropic SDK `messages` | 图片；当前 Anthropic 协议不接收语音或视频 |

所有协议都支持 `BaseUrl` 自定义端点。`Google GenerateContent` 通过官方
`google-genai` SDK 的 `HttpOptions(base_url=...)` 发送异步请求；若配置地址以
`/v1`、`/v1alpha` 或 `/v1beta` 结尾，JianerAI 会把末段识别为 SDK 的 API 版本，避免
重复拼接。Gemini 3 的 Agent 工具回合在未配置时默认使用 `ThinkingLevel: "medium"`，避免
默认高强度思考耗尽较小的 `MaxTokens` 后只返回空 candidate；普通无工具对话仍沿用模型
默认值，也可以把 `ThinkingLevel` 显式设为 `minimal`、`low`、`medium` 或 `high`。
`OpenAI Responses` 的语音和视频音轨使用同一 `BaseUrl` 下的 OpenAI
transcription 接口，默认模型为
`gpt-4o-mini-transcribe`，可通过 `TranscriptionModel` 修改。所有协议的对话上下文都只
使用 JianerAI 自己维护的本地短期历史。`OpenAI Responses` 每轮固定发送
`store: false`，不会发送或保存 `previous_response_id`，并在 `input` 中携带当前会话的
完整短期历史、当前消息和本轮结构化工具调用结果；配置文件中的 `other` 或 `Extra_Body`
也不能覆盖 `input`、`store` 或注入 `previous_response_id`。因此不会发起协议能力探测，
也不依赖官方服务或第三方中转站保存 Response。短期历史按完整 JianerCore 会话键
（协议、机器人账号、群聊/私聊、会话 ID、角色预设）隔离；切换模型、执行“注销”或
删除对应角色时仍由 JianerAI 清空相关本地上下文。

音视频预处理要求系统存在 `ffmpeg` 和 `ffprobe`；Docker 镜像会自动安装。单个视频
上限为 20 MiB、5 分钟，每次 OpenAI Responses 请求最多一个视频。JianerAI 只处理
适配器通过 `RESOLVE_MEDIA` 安全解析后返回的固定字节，不会把聊天中的原始 URL 或本地
路径直接交给 FFmpeg 或模型提供商。

示例：

```json
{
  "FriendlyName": "Claude",
  "Model": "claude-sonnet-4-5",
  "ResponseType": "Anthropic Messages",
  "ApiKey": "your-api-key",
  "BaseUrl": "https://api.anthropic.com",
  "Temperature": 0.5,
  "MaxTokens": 2000,
  "ToolsEnabled": "auto"
}
```

角色人设模板除现有的 `{self.bot_name}`、`{self.bot_name_en}`、
`{self.event_user}`、`{self.event_user_id}` 外，还可使用：

- `{agent_tools}`：当前会话实际可调用的工具名称，以逗号分隔；没有时为“无”
- `{agent_tools_info}`：当前会话实际可调用工具的描述、调用形式和参数说明；没有时为“无”

两项 Agent 变量会按 Agent 状态、当前模型的 tools 能力、当前协议/适配器能力以及
`agent_allowed_tools` 白名单动态过滤，不会列出本轮无法调用的工具。

## Agent 工具

内置工具包括当前时间、安全算术、当前发言人资料、当前会话资料、当前 canonical
用户 + preset 的长期记忆读取、创建与修改、只返回标题/URL/摘要的 `web_search`、只读的
`github_repository`、通用制图 `render_information_card`，以及可查看和操作网页的高权限
`web_browser`。主对话 Agent 只在用户明确要求“记住”或明确纠正既有记忆时直接使用写
记忆工具；普通对话在回复成功发送后，由独立 `memory_mode` 异步审查一次，不增加用户等待
时间。`list_my_memories`、`create_my_memory` 和 `update_my_memory` 都接受
`scope=person|group`：person 只能操作当前 canonical 用户，group 只能在群聊中操作当前群；
两者始终锁定当前 preset。新式写入参数将中性的 `canonical_fact` 与符合当前人设第一人称
语气、价值观和思考方式的 `memory_text` 分开；修改时还必须使用相同 scope 下
`list_my_memories` 返回的 ID。`read_recent_chat`（默认 20，最大 100）和
`search_current_chat` 只能读取当前会话 90 天内的客观聊天，不能传入 QQ 号、群号或表名。
两个写工具属于
`ToolRisk.MUTATING`；默认只放行这两个内置写工具，其他插件
注册的写工具必须通过 `agent_allowed_tools` 显式点名。插件仍不向模型开放 shell、本地文件
系统、原始 WebSocket、文件上传下载、消息管理或记忆删除能力。外部工具结果作为不可信数据处理；工具
中间结果不会写入短期历史、TTS 或长期记忆，只有最终 AI 文本会进入原有回复链。
调用搜索、GitHub 或网页工具本身不代表用户要求查看来源；默认回答不展示来源或 URL，只有用户在
当前请求中明确要求来源、出处、引用、链接或参考资料时，才附上实际使用的完整 URL。
所有场景的最终 AI 文字都会按纯文本输出，不使用 Markdown 或 HTML；制图工具内部使用的
HTML 只用于生成图片，不会作为聊天正文发送。

### 通用制图

`render_information_card` 只在适配器声明 `SEND_IMAGE` 能力时提供，包含两个 operation：

- `render_html`：模型自行生成 HTML/CSS/内联 SVG，适合通用信息卡、图表、表格、流程和
  时间线。渲染环境禁用 JavaScript、网络请求、本地文件、事件属性、下载和外部资源。
- `render_weather`：使用固定天气卡片模板，输入标题、重点指标、分区内容和上游来源；模板
  自动显示“天气服务由和风天气驱动 · www.qweather.com”。

工具会在当前群聊或私聊直接发送 PNG，随后模型只用纯文本概括。它属于
`ToolRisk.PRESENTATION`，只允许向当前会话发送刚生成的临时图片，临时文件在发送后删除；
HTML 正文不会写入工具日志。若配置了 `agent_allowed_tools`，需要显式加入
`render_information_card`。Docker 镜像安装 Noto CJK 字体；Windows 使用系统中文字体。

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
`agent_allowed_tools`，需要显式加入要开放的上述 Tool 名称以及
`render_information_card`，否则天气数据只能退回纯文本展示。

实现范围以当前[官方 API 目录](https://dev.qweather.com/docs/api/)为准：Weather 使用坐标型
v1 接口，不包含已标记弃用的三个城市天气 v7 接口，也不包含控制台 API；分钟预报、指数、
时光机、热带气旋、潮汐和天文等尚无替代的现行 v7 接口继续提供。鉴权遵循
[JWT 规范](https://dev.qweather.com/docs/configuration/authentication/)。凡使用这些 Tool 的结果，
模型会优先调用固定天气卡片，把和风天气标识、官网以及天气预警/空气质量响应中的上游归因
放入图片，聊天正文只保留纯文本天气概括。制图工具不可用或渲染失败时，归因才退回纯文本。

## 记忆与身份

- OneBot/Milky QQ 身份自动归一到 `qq:<id>`。
- 飞书绑定以宿主 JSON 为权威源，通过持久 outbox 幂等调用插件
  `authorize()` / `merge_identity()`；AI 合并失败不回滚非 AI 绑定。
- schema v5 的全局表只保存注册、身份、设置、任务和审计信息，按 `sys_`、`cfg_`、
  `job_`、`audit_` 前缀分组，不保存集中式记忆正文或聊天正文。
- `sys_persona_partitions` 为每个人设登记五张稳定、可读的物理表。例如 XingYu 的首个人设
  使用 `mem_p0001_xingyu_people`、`mem_p0001_xingyu_groups`、
  `mem_p0001_xingyu_evidence`、`mem_p0001_xingyu_episodes` 和
  `mem_p0001_xingyu_deleted`。修改人设显示名不会重命名这些表。
- people 表按 canonical 人员统一保存该人设对一个人的长期记忆，不绑定单一群；groups 表只
  保存该人设对当前群整体的长期记忆；`memory_text` 是人设化主观回忆，
  `canonical_fact` 只用于去重、冲突和更新判断。evidence 表保留来源摘要，deleted 表保留
  墓碑并防止后台重新生成已删除内容。
- 每个群、每个私聊各有一张独立客观聊天表，例如 `chat_g000042_2822554898` 或
  `chat_u000017_qq2822554898`。同一个群原文只存一份，所有可见入站消息在 fallback/命令
  匹配前记录；JianerAI 成功发出的消息也记录。不同人设读取同一份客观原文，再分别形成
  自己风格的长期记忆。
- 原始聊天默认滚动保存 90 天，每张表每轮最多清理 1000 行。回复前和审查时只读取当前
  会话最近 50 条、最多 8000 字；任何工具或模型上下文都不能跨会话或跨人设泄漏。
- 每次回复成功发送后先写出站聊天和 episode，再创建持久 `job_memory_reviews` 任务。
  独立审查器只允许 `create`、`update` 或 `no-op`，每轮最多三项；任务以 exchange key
  幂等，失败按指数退避，进程重启后继续。发送失败不会创建 episode 或审查任务。
- v4 数据库不会在普通启动时静默升级；启动会抛出明确的迁移要求。只有显式调用
  `JianerMemoryStore(..., initialize=False).migrate_to_v5()` 才会先用 SQLite Backup API
  完整备份、校验并迁移，全部验证通过后删除 `memory_facts`、`memory_evidence`、
  `memory_suppressions`、`raw_transcript_messages` 及旧 `persona_<64位哈希>_*` 表；不创建
  同名兼容视图，也不再双写旧表。

## 生命周期与媒体

插件后台任务、TTS 临时目录、数据库连接和命令 matcher 都归当前
`PluginManager` generation 管理。重载先完整加载候选 generation，原子交换后
再关闭旧 generation；加载失败时继续保留旧实例。

引用解析与媒体解析是两个独立适配器能力。媒体只接受 Core
`resolve_media()` 返回的已解析字节，并受 scheme、重定向、大小、总超时、MIME
嗅探和本地路径白名单限制。
