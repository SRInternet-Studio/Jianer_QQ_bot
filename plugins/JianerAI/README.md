# JianerAI

`jianerbot-plugin-jianer-ai` 是简儿的 JianerCore 目录插件，统一承载 HypeR
风格的 Agent 事件管线、持久 Main/System Context、Collector、工具发送、长期
记忆、回复后缀、TTS、图片与引用解析。入口固定为 `setup.py`，依赖内置
`jianerbot-plugin-alconna`。

## 触发规则

- 群聊自动处理严格按白名单工作；没有任何白名单的群不会缓存普通消息。
- 白名单成员发言会进入 Collector；被 @ 时跳过等待窗口，立即处理“缓存 + 当前消息”。
- 非白名单成员的消息只作为不可执行背景进入已有白名单群的缓存，不会单独触发 Agent。
- 私聊自动处理；Bot 主人私聊立即处理，其他用户进入 Collector。
- 群通知默认进入缓存，戳 Bot 或 Bot 消息被撤回时立即处理。
- 普通 `reminder` 前缀（通常为 `~`）只用于命令，不进入 Agent 对话。
- 飞书：支持私聊和群内 mention；不声明或模拟原生合并转发能力。
- `config.black_list` 只限制 JianerAI，不影响其它宿主或业务插件。

每个 `(protocol, self_id, conversation_kind, conversation_id)` 拥有独立持久 Main
Context，完整保存模型 tool call、tool result、reasoning 和 provider 返回内容；空闲
上下文会从内存回收并按需从磁盘恢复。System Context 负责总结、压缩和全局人设切换。
全局人设来自 `data/jianer_ai/profiles.json`，原始人设模板直接作为 prompt，不再生成
结构化人格。长期记忆继续由 memorix 提供，并保留 person/group 作用域、画像、Episode、
证据和回复后审查。

## 常用命令

- `~模型菜单`、`~切换模型 [代码]`；兼容旧名 `~ai管理菜单`、`~切换AI`
- `~人设`、`~切换人设 [名称]`：管理全局 profiles；兼容旧名 `~角色扮演`、`~切换角色`
- `~添加人设 [名称] [简介] : [原文]`、`~删除人设 [名称]`
- `~重载人设 [名称]`：从原始模板重新加载；`~人设 迁移` 导入原始模板
- `~简儿记忆 [帮助|状态|开启|关闭|间隔|立即生成|列表|删除|清空|恢复]`
- `~设置全局后缀`、`~删除全局后缀`
- `~设置特定后缀`、`~删除特定后缀`
- `~TTS [开启|关闭|状态]`
- `~Agent [开启|关闭|状态|工具|子代理]`：管理当前群白名单、工具和 Sub-Agent 状态
- `~清空上下文`：清空当前 Main Context；兼容旧名 `~注销`、`~重置上下文`
- `~agent.on/off/status/stop/profile/context/func/model`：HypeR 风格别名

后缀只在 `SuffixStore.apply_ai_reply()` 中应用；ping、群发、休眠等宿主消息不会
经过后缀处理。私聊 TTS 默认关闭，群聊默认开启。

## 配置与数据

插件读取宿主 `config.others`：

- `jianer_ai_db_path`：规范化 SQLite 数据库，默认 `jianer_ai.db`
- `default_mode` / `ai_default_model`：默认对话模型
- `content_moderation_enabled`：是否启用独立模型内容审核；未配置时默认关闭
- `content_moderation_ai_reply_enabled`：是否在主模型生成回复后再次审核即将发送的
  AI 回复；默认关闭。开启后复用 `content_moderation_model`，拒绝或审核故障都会阻止
  原回复发送、写入短期历史和对话记忆，并由审核模型生成的拒绝文本代替回复。兼容旧的
  配置别名 `content_moderation_reply_enabled`
- `content_moderation_model`：审核使用的 `aiconfig` 模型代码；审核开启时必须显式配置，
  可选择任意已经加载的模型代码，且不跟随用户切换的主对话模型
- `content_moderation_timeout_seconds`：单次审核超时，范围 1–120 秒，默认 30 秒；
  超时、提供商失败或非法审核结果都会按 fail closed 拒绝本轮，不会绕过审核
- `memory_mode`：记忆提炼模型
- `memory_episode_segmentation_model`：长期记忆 episode 分段使用的模型，默认
  `auto` 会在 `aiconfig/*.ai.json` 中挑选可用模型；某个模型不可用时可显式填写
  可用的模型配置名（如 `grok`），避免分段一直回退到规则聚类
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
- `agent_profile`：当前全局人设名，来自 `data/jianer_ai/profiles.json`
- `agent_api_profile`：当前全局 Agent 模型代码；留空时使用 `default_mode`
- `agent_white`：群白名单，键为 `protocol:self_id:group_id`，值为用户 ID 列表
- `agent_heartbeat`：启用 System Context 周期维护，默认关闭
- `agent_max_concurrency`：全局 Agent 模型调用并发上限，`0` 表示不限
- `agent_collector_max_buffer` / `agent_collector_keep_after_compress` /
  `agent_collector_max_summary_chars`：Collector 默认 80 / 60 / 8000
- `agent_core_idle_hours`：Main Context 空闲回收时间，默认 12 小时
- `agent_max_history_chars`：自动总结阈值，默认 60000
- `agent_stop_perm` / `agent_stop_open_after`：Stop 权限和向所有人开放的超时
- `agent_func_disabled`：工具运行期禁用状态，由 Agent 命令持久化
- `agent_allowed_tools`：允许暴露给模型的工具名称数组或逗号分隔字符串；未配置时
  允许全部内置工具，显式空数组则不暴露工具；配置白名单时，记忆写入还需显式加入
  `create_my_memory` 和/或 `update_my_memory`
- `agent_platform_api_enabled`：向模型注册平台 API 工具，默认 `true`
- `agent_platform_api_cross_conversation_admin_only`：仅机器人管理员可向其他会话
  发消息，默认 `true`
- `agent_subagent_enabled`：注册持久 Sub-Agent 工具，默认 `true`
- `~Agent 子代理`：查看持久 Sub-Agent 列表
- `agent_skills_enabled`：加载 Agent Skills，默认 `true`
- `agent_skills_dirs`：`SKILL.md` 技能目录列表，相对路径以项目根目录为基准，默认 `.agents/skills`
- `agent_skills_max_file_bytes`：单个 `SKILL.md` 最大字节数，默认 65536
- `agent_mcp_enabled`：启用配置的 MCP 服务，默认 `false`
- `agent_mcp_servers`：管理员配置的 MCP server 映射；支持 stdio 和 Streamable HTTP
- `agent_bash_enabled`：注册人工审核的 `bash` 工具，默认 `true`；只对机器人管理员开放
- `agent_bash_approval_timeout_seconds`：等待发言人审核的秒数，默认 120，范围 5–600
- `agent_bash_command_timeout_seconds`：命令最长执行秒数，默认 60，范围 1–300
- `memory_auto_inject`：每个 Agent 轮次是否自动注入相关个人记忆、群记忆、近期聊天和对话片段，默认开启
- `memory_auto_inject_max_chars`：自动记忆资料字符预算，默认 16000
- `memory_auto_inject_recent_chat` / `memory_auto_inject_episodes` / `memory_auto_inject_group`：分别控制近期聊天、
  相关对话片段和群记忆注入，默认开启
- `memory_auto_inject_subagents`：是否把当前轮次的只读记忆快照传给 Sub-Agent，默认开启
- `agent_browser_enabled`：启用 `web_browser`，默认 `true`
- `agent_browser_headless`：使用无界面 Chromium，默认 `true`
- `agent_browser_profile_dir`：共享持久 Profile，默认 `data/jianer_browser/profile`
- `agent_browser_audit_path`：写请求脱敏审计，默认 `data/jianer_browser/audit.jsonl`
- `agent_browser_max_pages`：会话页面上限，默认且最大为 16
- `agent_browser_idle_seconds`：空闲页面回收时间，默认 900 秒

当请求审核开启时，每次 AI 对话会先安全解析引用和附件，再把当前请求、最近最多 8 条且合计最多
6000 字的 Main Context 历史、当前完整人设模板以及解析后的图片、语音或视频字节
交给 `content_moderation_model`。完整人设只供审核模型在 `refuse` 时模仿身份、自称、称呼方式、
情感立场、句式节奏和口癖，不能参与安全分类，也不能覆盖审核规则。附件只有在审核模型所用
协议支持对应格式时才能送审；不支持、解析失败或无法送达时会按 fail closed 拒绝本轮，
不会跳过审核。审核发生在主模型和任何 Agent 工具之前。审核器只接受严格 JSON，
会区分医学教育、新闻法律讨论、风险预防等正当语境与露骨色情、未成年人性内容、性剥削、
自残伤人、武器、违法犯罪、恶意网络行为、仇恨骚扰、隐私侵害和极端主义支持等请求。

命中违规时，审核模型必须按当前人设生成一段简短、自然、不复述违规细节的拒绝，并在合适时
提出安全替代方向。该请求不会进入主模型、工具调用、短期历史或回复后记忆审查；若消息已由
观察器写入客观聊天记录，其正文会替换为 `[内容已由安全审核隐藏]`。审核日志只记录模型、
分类代码、耗时和字符/附件数量，不记录审核理由或原始违规内容。拒绝文本不会再套用用户可配置
的 AI 回复后缀，避免审核后的安全文本被二次改写。若关闭
`content_moderation_enabled`，以上前置保障不会生效。若开启
`content_moderation_ai_reply_enabled`，主模型回复（包括配置的回复后缀）会在发送前用同一
审核模型检查；审核模型判断不能输出时，会像拒绝用户请求一样直接使用它生成的拒绝文本，
不会把原回复保存到 Main Context、对话片段或回复后记忆审查。关闭
`content_moderation_enabled` 只会关闭用户请求的前置审核；回复审核是否生效由
`content_moderation_ai_reply_enabled` 单独决定。

当前项目的默认主回答模型为 `grok`。示例配置选择 DeepSeek 作为旁路门禁；也可以把
`content_moderation_model` 改成其他已加载模型，或把 `content_moderation_enabled` 设为
`false` 完全关闭审核。审核关闭时不会构造或验证审核模型。审核返回 `allow` 后，
发送给 Grok、Gemini 或其他主模型的消息、历史、完整人设、系统提示、附件、工具声明和提供商
协议均保持接入审核前的原样，审核结果及任何额外安全提示都不会进入主模型请求。只有
`refuse` 或审核故障时才会在主模型之前终止本轮。如果选择 DeepSeek 审核，配置建议使用
`OpenAI Chat Completions`、`Temperature: 0` 和 `ToolsEnabled: false`，当前本地
`aiconfig/deepseek.ai.json` 已按此设置。

网页搜索走哪个后端由宿主配置 `WEB_SEARCH_PROVIDER` 决定，不按当前模型推断：

- `ddgs`（默认）：使用 `ddgs` 聚合搜索，默认引擎由 `DDGS_BACKEND` 决定，当前默认
  `bing`；可用的 DDGS 引擎来自当前安装版本实际启用的 text 引擎（如 `auto`、
  `brave`、`duckduckgo`、`google`、`grokipedia`、`mojeek`、`startpage`、
  `wikipedia`、`yahoo`），另外可直接选择国内后端 `zhipu`、`bocha`、`bing_cn`。
  `ddgs` 9.16 起 text 引擎不再包含 `bing`，此时 `bing` 会自动改用内置直连 Bing
  客户端（先请求 `cn.bing.com`，无结果再试 `www.bing.com/search`）；未启用的
  DDGS 引擎会直接报错，不再静默回落到 `auto`。
- `deepseek`：使用 DeepSeek 官方网页搜索 API `POST /v1/tools/search`，读取
  `DEEPSEEK_API_KEY`，与当前对话模型无关，任何模型下都可启用。该模式下忽略
  `backend` 参数，DeepSeek 搜索失败不会回退到 DDGS。
- `anysearch`：调用 AnySearch 统一搜索 API
  `POST https://api.anysearch.com/v1/search`。`ANYSEARCH_API_KEY` 可选，留空时使用
  匿名访问（按 IP 限制的每日免费额度）；`ANYSEARCH_ZONE` 可选 `cn`/`intl`（默认
  `cn`），`ANYSEARCH_LANGUAGE` 默认 `zh-CN`，`ANYSEARCH_TAG` 可指定垂直领域标签
  （如 `code.doc`），标签要求额外参数时用 `ANYSEARCH_PARAMS` 传 JSON 对象（如
  `{"library":"react"}`）。AnySearch 也可以写进 `WEB_SEARCH_FALLBACK_BACKENDS`，
  作为 DDGS 超时或网络错误后的回退后端。

DDGS 超时、限流或网络错误时，按 `WEB_SEARCH_FALLBACK_BACKENDS`（默认
`zhipu,bocha,bing_cn`）依次回退：智谱联网搜索读取 `ZHIPUAI_API_KEY`，博查
Web Search 读取 `BOCHA_API_KEY`，DeepSeek 网页搜索读取 `DEEPSEEK_API_KEY`；
AnySearch 读取可选的 `ANYSEARCH_API_KEY`；未配置 key 的付费后端自动跳过。Bing 中国
（`cn.bing.com`）和 AnySearch 匿名模式无需 key，可作为兜底。
搜索后端变量与密钥可由进程环境或仓库根 `.env` 提供；`WEB_SEARCH_PROVIDER` 或
`WEB_SEARCH_FALLBACK_BACKENDS` 填了不支持的值会直接报错。DDGS 区域固定为
`cn-zh`，安全搜索固定为 `moderate`；模型不能指定代理、请求头或待抓取 URL。若
配置了 `agent_allowed_tools` 白名单，需要显式加入 `web_search`。

以图搜文由 `reverse_image_search` 提供：读取当前消息中的第一张图片，或使用模型
传入的公开 `image_url`，依次尝试 Bing Visual Search、Yandex、Google Lens、
SauceNAO。Bing 需要 `BING_VISUAL_SEARCH_KEY`（可选自定义
`BING_VISUAL_SEARCH_ENDPOINT`），SauceNAO 可选 `SAUCENAO_API_KEY`，其余两个
后端无需 Key；未配置 Key 的 Bing 会自动跳过。若配置了 `agent_allowed_tools`
白名单，需要显式加入 `reverse_image_search`。

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
进行二次确认。浏览器不执行 URL 安全策略，可访问 `localhost`、内网地址、链路本地
地址和 `file://`；也不拦截重定向、子请求、媒体、字体、WebSocket 或 Service
Worker，并允许下载。页面只以正文和最新快照元素编号提供给模型，不开放 CSS、
XPath、任意 JavaScript、坐标或截图。

模型配置继续从 `aiconfig/*.ai.json` 读取；全局人设使用
`data/jianer_ai/profiles.json`，原始模板全文直接写入 `prompt`；后缀继续使用
`suffix_config.json`。模型配置可用
`ToolsEnabled: true/false/"auto"` 控制
Function Calling；兼容端点明确拒绝 tools 参数时，本 generation 会自动退回普通
对话。部分 DeepSeek 兼容网关把调用返回为 DSML 文本；插件会把完整合法的 DSML
转换为结构化工具调用，并拒绝把损坏的调用标记发送给用户。不要提交包含密钥的本地配置。

`ResponseType` 支持以下协议；Anthropic 已不再支持：

| ResponseType | 请求协议 | 附件能力 |
| --- | --- | --- |
| `OpenAI Chat Completions` | OpenAI `chat.completions` | 图片，发送前统一为静态 PNG/JPEG；模型支持时可读 MP3/WAV 语音，其他音频先转 WAV；不支持视频 |
| `OpenAI Responses` | OpenAI `responses` | 图片，发送前统一为静态 PNG/JPEG；语音先走转写；视频抽取最多 8 帧并转写音轨 |
| `Google GenerateContent` | Google GenAI SDK `generate_content` | 原生图片、语音和视频 |

`OpenAI Chat Completions` 支持 `SystemPromptMode`：

- `auto`（默认）：官方 OpenAI/DeepSeek 地址使用标准 `system` role；自定义
  `BaseUrl` 中转站使用 user 前缀，避免中转站静默丢弃 `system`。
- `system`：强制发送标准 `system` role。
- `user`：强制把人格与系统规则作为当前请求前的 user 指令发送。

如果中转站仍然忽略人格，可在对应的 `*.ai.json` 中显式设置
`"SystemPromptMode": "user"`。

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
也不依赖官方服务或第三方中转站保存 Response。持久历史按
`(protocol, self_id, conversation_kind, conversation_id)` 隔离；执行“清空上下文”会清空
当前 Main Context。

音视频预处理要求系统存在 `ffmpeg` 和 `ffprobe`；Docker 镜像会自动安装。单个视频
上限为 20 MiB、5 分钟，每次 OpenAI Responses 请求最多一个视频。JianerAI 只处理
适配器通过 `RESOLVE_MEDIA` 安全解析后返回的固定字节，不会把聊天中的原始 URL 或本地
路径直接交给 FFmpeg 或模型提供商。

OpenAI 协议的图片由 Pillow 归一化后再发送：动图（GIF、动态 PNG/WebP）只取第一帧，
最长边压缩到 2048 像素，编码为静态 PNG 或 JPEG，单张编码上限 2 MiB；已经是合规静态
图片时保持原字节不变。部分 OpenAI 兼容中转站会以上游 400 拒收 GIF，或在多兆字节
base64 请求上超时，归一化后这类图片不会再让整轮对话失败。其他协议按各自能力原样内联。

示例：

```json
{
  "FriendlyName": "Gemini",
  "Model": "gemini-3.8-flash",
  "ResponseType": "Google GenerateContent",
  "ApiKey": "your-api-key",
  "BaseUrl": "https://generativelanguage.googleapis.com",
  "Temperature": 0.5,
  "MaxTokens": 2000,
  "ToolsEnabled": "auto"
}
```

角色人设模板除现有的 `{self.bot_name}`、`{self.bot_name_en}`、
`{self.event_user}`、`{self.event_user_id}` 外，还可使用：

- `{agent_tools}`：当前会话实际可调用的工具名称，以逗号分隔；没有时为“无”
- `{agent_tools_info}`：当前会话实际可调用工具的紧凑调用目录；完整描述和参数
  schema 仍由本轮原生 `tools` 声明传递，避免在人设里重复一遍；没有工具时为“无”

两项 Agent 变量会按 Agent 状态、当前模型的 tools 能力、当前协议/适配器能力以及
`agent_allowed_tools` 白名单动态过滤，不会列出本轮无法调用的工具。

## 全局人设

HypeR 风格人设保存在 `data/jianer_ai/profiles.json`：

```json
{
  "XingYu": {
    "prompt": "原始人设模板全文",
    "inject_master": true
  }
}
```

当前人设由 `config.others.agent_profile` 指定。切换人设时 System Context 先总结
所有已加载 Main Context，再更新 system prompt；不再读取或生成结构化人格 JSON。

模型配置可用 `max_history_messages` 和 `max_history_chars` 同时限制短期历史；
字符预算从最新消息向前保留，默认 24000 字符，设为 `0` 表示不限制。当前请求和
完整人设不参与该裁剪，避免长历史挤占人设与工具声明的空间。

## Context 与 Collector

- 每个群和私聊拥有独立持久 Main Context，原始 tool call、tool result、reasoning 和
  provider 内容都会写入 `data/jianer_ai/agent/contexts/`。
- Collector 默认等待 5–16 秒并与上一条消息节奏相关，缓冲达到阈值时把旧事件压缩为摘要。
- 处理期间的新事件保留到下一批，不再取消当前 provider 请求。
- 达到历史预算后由 System Context 总结并替换历史前缀。
- `~agent.context.summary` 手动总结当前 Context；`~agent.context.clear` 清空。
- `~agent.stop` 中止当前处理；超过配置阈值后普通成员也可中止。

## 平台 API 工具

当 `agent_platform_api_enabled` 为真时，模型获得当前协议对应的静态 API 工具：

- `send_group_msg` / `send_private_msg`：HypeR 主回复工具。
- `collected_send`：长文本或合并转发发送。
- `send_message`：兼容工具，支持正文、At 列表、引用消息与图片链接。
- OneBot V11：按 [OneBot V11 公开 API](https://11.onebot.dev/api/public.html) 注册 38 个
  `onebot_*` 工具，包含消息收发、消息查询、群成员管理、好友请求和状态查询。隐藏 API
  与未声明 action 不会暴露。
- Milky 1.3：按官方 OpenAPI 注册 65 个 `milky_*` 工具，包含消息收发、群精华、群成员、
  通知、文件和账号 API。每个工具固定 endpoint、HTTP 方法及参数 JSON Schema。
- Lark/飞书：注册 9 个 `lark_*` 消息工具，覆盖发送、回复、查询、撤回、已读成员查询和表情回应。
  请求路径、HTTP 方法、查询字段和消息体由代码固定；工具接收文本，不要求模型拼接 OAPI JSON 字符串。
- `platform_command`：用 `jianer-plugin-alconna` 解析少量固定的 OneBot 常用命令，没有任意
  `action {JSON}` 回退。

不向模型暴露任意 action、Milky endpoint 或 Lark path/method/body 透传工具。OneBot、Milky
和 Lark 静态 API 工具使用精确参数类型并拒绝未声明字段；读取和变更 API 均需要群管理员
（`sender.role` 为 owner/admin）或机器人管理员（runtime 的 `root_users`/`super_users`/
`manage_users`/`admins`）权限。角色缺失时会回退查询群成员信息并缓存 60 秒。Sub-Agent
默认不继承这些平台 API 工具。

## Sub-Agent

Sub-Agent 是最多 3 个的持久 Agent 实例。创建后拥有独立 prompt、history、tasks 和
受限工具集；`sub_feed` 投喂任务，`sub_report` 把结果写回主 Context 的下一轮通知。
子 Agent 不继承消息发送、平台写操作、memorix 写入和递归派生能力。元数据和历史保存在
`data/jianer_ai/agent/subagents/`，插件重载后可重新发现。

## Tool 插件

所有内置工具、平台 API、Context 工具和 Sub-Agent 都通过 `ToolRegistry` 注册。第三方
插件可以实现 `plugin_id` 与 `provide_tools(context)`，再通过 `setup.py` 导出的
`register_tool_plugin(plugin)` 动态加载；返回的 `ToolPluginRegistration` 可传给
`unregister_tool_plugin(...)` 卸载。异步 provider 使用 service 的异步注册接口。每个工具仍经过
风险级别、协议能力、权限、JSON schema、超时、输出长度和审计校验，插件卸载时会调用 provider
的 `shutdown()`。

## Agent Skills 与 MCP

Agent Skills 按 `<技能目录>/SKILL.md` 加载，文件需包含 YAML frontmatter 的 `name` 与
`description`，且 `name` 必须与目录名相同。默认扫描 `.agents/skills/`，也可用
`agent_skills_dirs` 配置多个目录。模型只会先看到技能名称与描述；需要时调用
`load_agent_skill` 读取正文，再用 `read_agent_skill_resource` 读取该技能目录下的 UTF-8
文本资源。读取路径会限制在对应技能目录内，技能文本不会执行；frontmatter 中的
`allowed-tools` 等字段不能授予运行权限，所有工具仍按 `ToolRegistry` 的权限和风险规则过滤。
示例：

```text
.agents/skills/
  code-review/
    SKILL.md
    references/checklist.md
```

MCP server 由管理员在 `config.others.agent_mcp_servers` 配置，模型不能添加或修改 server。
将 `agent_mcp_enabled` 设为 `true` 后启用，支持 stdio 与 Streamable HTTP transport。配置键
就是 server 名称，例如：

```json
"agent_mcp_enabled": true,
"agent_mcp_servers": {
  "docs": {
    "transport": "stdio",
    "command": "uvx",
    "args": ["--from", "example-mcp-server", "example-mcp-server"]
  },
  "internal_api": {
    "transport": "streamable_http",
    "url": "https://mcp.example.invalid/mcp",
    "headers": {"Authorization": "Bearer ..."}
  }
}
```

MCP 工具名会加 server 命名空间，例如 `mcp_docs_search`，并注册进同一
`ToolRegistry`；工具参数仍经 JSON Schema 校验，工具超时、风险级别、`agent_allowed_tools`
白名单和 shutdown 清理仍由 JianerAI 管理。MCP 工具默认只读；有副作用的工具必须列入该
server 的 `mutating_tools`，并在 `agent_allowed_tools` 中加入其完整工具名。stdio 命令会在
本机启动子进程；HTTP server 会收到工具参数，只应连接可信服务。

`bash` 是机器人管理员专用工具。每次调用都会把完整命令和工作目录直接发到当前会话；
命令保持待执行状态，直到同一会话的原发言人使用当前 `reminder` 前缀发送
`批准Shell <请求ID>` 或 `拒绝Shell <请求ID>`。普通“同意”消息不构成授权；拒绝、过期、插话或插件关闭都会取消
待执行命令。每次批准只对应一条命令，命令输出有长度上限，Shell 进程超时或取消时会被终止。
若设置了 `agent_allowed_tools` 白名单，需把 `bash` 加入白名单才能交给模型调用。

其他已加载的 JianerCore 插件可显式导出 `create_agent_tool_provider(context)` 或
`provide_agent_tools(context)`，返回 JianerAI 的 `ToolSpec`（或实现 `provide_tools(context)`
的 provider）。JianerAI 只识别这两个约定，不反射调用任意插件函数。导出的工具会加上
`plugin_<插件ID>_` 前缀；每次调用持有当前插件代的运行租约，并继续经过 JSON Schema、风险、
协议、能力、权限和超时检查。示例：

```python
from plugins.JianerAI.tools import ToolRisk, ToolSpec


def provide_agent_tools(context):
    async def handle(tool_context, arguments):
        return await plugin_service.lookup(arguments["query"])

    return (
        ToolSpec(
            name="lookup",
            description="查询此插件提供的数据。",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=handle,
            risk=ToolRisk.READ_ONLY,
        ),
    )
```

## Agent 工具

内置工具包括当前时间、安全算术、当前发言人资料、当前会话资料、当前 canonical
用户 + preset 的长期记忆读取、创建与修改、只返回标题/URL/摘要的 `web_search`、只读的
`reverse_image_search`、只读的 `github_repository`、通用制图
`render_information_card`，以及可查看和操作网页的高权限 `web_browser`。主对话
Agent 只在用户明确要求“记住”或明确纠正既有记忆时直接使用写
记忆工具；普通对话在回复成功发送后，由独立 `memory_mode` 异步审查一次，不增加用户等待
时间。`list_my_memories`、`create_my_memory` 和 `update_my_memory` 都接受
`scope=person|group`：person 只能操作当前 canonical 用户，group 只能在群聊中操作当前群；
两者始终锁定当前 preset。新式写入参数将中性的 `canonical_fact` 与符合当前人设第一人称
语气、价值观和思考方式的 `memory_text` 分开；修改时还必须使用相同 scope 下
`list_my_memories` 返回的 ID。`read_recent_chat`（默认 20，最大 100）和
`search_current_chat` 只能读取当前会话 90 天内的客观聊天，不能传入 QQ 号、群号或表名。
后台记忆巩固和回复后记忆审查同样使用当前 preset 渲染出的完整人设模板，不生成或使用
其他版本的人设。
两个写工具属于
`ToolRisk.MUTATING`；默认只放行这两个内置写工具，其他插件
注册的写工具必须通过 `agent_allowed_tools` 显式点名。插件仍不向模型开放 shell、本地文件
系统、文件上传下载或记忆删除能力。外部工具结果作为不可信数据处理；工具
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
  独立审查器会自动整理长期事实，并从用户明确表达的关系中生成关系三元组；关系写入
  与 paragraph、图投影和可选 relation embedding 使用同一条 ingest 链路，不要求 Agent
  先调用记忆工具。审查器每轮最多三项记忆 action 和六项关系，任务以 exchange key
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
# Jianer Memory 长期记忆

JianerAI 的默认 `memory_backend` 是 `long_memory`。长期记忆由
`plugins/JianerAI/memorix/` 内核维护：SQLite 保存元数据、证据、Episode 和
生命周期状态，FAISS 保存 paragraph / relation 向量，SQLite FTS5/BM25、双路
融合、图关系和 PageRank 负责召回。旧 `JianerMemoryStore` 继续保存聊天原文、
身份绑定、生成屏障和审查任务；长期记忆读写默认不会切换到旧存储。

Embedding 使用独立的 `aiconfig/embedding.json`，该文件不与聊天模型配置共享。
可从 `aiconfig/embedding.json.example` 复制后填写 OpenAI 兼容接口或 Gemini
接口。向量维度、批量、并发、重试和缓存均由该文件控制。默认集成要求 Embedding
模型可用；连接失败会报告运行时错误，不会静默改用旧记忆或关键词检索。

记忆控制台是可选的局域网服务。配置 `others.jianer_ai_memory_console_enabled`
为 `true` 后，服务每次启动都会生成新的临时 Token，并只输出到启动控制台；Token
不会写入配置、数据库或浏览器持久存储。重启后旧 Token 失效，需要从本次启动输出中
重新输入。服务默认监听 `0.0.0.0:8787`，所有 API 请求必须带 `X-Memory-Token`。
前端源码位于 `plugins/JianerAI/memorix_console/frontend/`，使用 Vue 3 构建；机器人运行时
从 `memorix_console/static/` 提供页面。开发预览可在 `frontend/` 下运行 `npm ci && npm run dev`，
更新运行时页面时运行 `npm ci && npm run build`。控制台展示内核状态、向量池、语义检索、
段落、对话片段、图谱、画像、事实、来源、回收站和维护任务。

迁移旧动态表数据库：

```bash
python -m plugins.JianerAI.memorix.scripts.migrate_jianer_memory \
  jianer_memory.db --target-db jianer_ai.db \
  --data-dir data/jianer_ai_memorix
```

先加 `--dry-run` 查看表和计数。迁移脚本会记录源文件 SHA-256，并在覆盖已有
目标数据库前创建备份。

本项目的长期记忆功能基于 MaiBot 的开源记忆实现编写，并结合 JianerAI 的运行时接口完成集成；来源与许可说明见 `plugins/JianerAI/memorix/NOTICE.md`。
