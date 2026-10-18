# JianerMate

`jianerbot-plugin-jianer-mate` 是 JianerCore 目录插件，用单一人设复刻
MaiBot 风格的群聊 AI：群里被 @ 或私聊时正常回话，群内可以按需开启主动发言，
回复拆成短句多段，并带一个全局共享的表情包库（自动收集 + 视觉挑选发送）。

## 与 JianerAI 的关系

本插件是 `jianerbot-plugin-jianer-ai` 的替代品，两者**互斥**：

- 插件启动时检测到 JianerAI 已加载，会打印明确错误并且不注册任何消息处理；
- 想切换时先用 `~禁用插件 JianerAI`、再用 `~启用插件 JianerMate`，然后重载插件；
- 依赖 JianerAI 的功能（Agent 工具、长期记忆、TTS、后缀、MCP、飞书身份合并、
  MaimaiDX AI 工具）在本插件接管期间不可用。

## 触发规则

- 群聊：必须显式 At 机器人；裸 At 也会回应；`~` 前缀命令仍由 Alconna 处理。
- 私聊：普通文本直接进入 AI；带前缀并按插件命令解析。
- 主动发言：只对「已开启」的群生效，默认全部关闭，由群主/管理员开启。

## 常用命令

群主/管理员（机器人管理员可在任意群执行）：

- `~主动发言` / `~主动发言 状态`
- `~主动发言 开启` / `~主动发言 关闭`
- `~主动发言 频率 0.5`（0 到 1，越大越爱说话）
- `~主动发言 免打扰 23:00-08:00` / `~主动发言 免打扰 关闭`
- `~主动发言 决策 [条数]`：查看最近的主动发言决策与理由
- `~设置提示 <文本>` / `~清空提示`（`~设置群提示` / `~清空群提示` 同样可用）
- `~偷表情`（回复一张图片后发送）
- `~表情包` / `~表情包 列表 1` / `~表情包 查看 <编号>`（回复图片便于改标签）

仅机器人管理员：

- `~人格` / `~人格 状态`：查看当前人格与来源
- `~人格 转换 <原设定文本>`：用模型把旧设定转写成新人格（也可以回复一条含设定的消息后发送 `~人格 转换`）
- `~人格 导入 [旧预设名]`：读取 `prerequisites/current.json` 里的旧角色预设并转写（不带名字时使用 `Normal`）
- `~人格 重置`：恢复成 `config.json` 里的人格
- `~人格 概率 0.2` / `~人格 风格` / `~人格 风格添加 <文本>` /
  `~人格 风格删除 <编号>` / `~人格 风格清空`：管理备用说话风格
- `~主动发言 调试`（把“本会怎么发言”的预览发到群里，不发正式消息）
- `~主动发言 重载`（重读 `config.others` 与 `aiconfig`）
- `~表情包 删除 <编号>` / `~表情包 标签 <编号> <标签>` / `~表情包 清空 确认`

## 人格（MaiBot 风格）

人格字段对齐 MaiBot 的 `[personality]` / `[chat.reply_style]`：

- `personality`：她是谁、什么性格（1-2 句）
- `behavior_style`：行动准则，什么时候插话、什么时候安静
- `reply_style`：说话习惯，长短、语气、口头禅
- `multiple_reply_styles`：备用说话风格列表，触发后只影响当次回复
- `multiple_probability`：每次回复改用备用风格的概率，`0` 表示从不切换
- `group_chat_prompt` / `private_chat_prompt`：群聊与私聊各自的补充要求
- `aliases`（也可写 `alias_names`）：别人用来称呼她的名字；开启
  `jianer_mate_mentioned_reply` 后，群里出现这些名字会跳过随机概率直接进入决策

人格优先级：`~人格 转换` / `~人格 导入` 的结果保存在
`data/jianer_mate/mate.db` 的 `state` 表里，优先于 `config.others.jianer_mate_persona`；
`~人格 重置` 会删除这份覆盖，回到 `config.json`。

`~人格 导入` 只以只读方式读取 `prerequisites/current.json` 与其中的模板文件，
不依赖 JianerAI 插件本身；导入结果由模型转写，转写前会先发一条“正在转写”的提示。

## 插件配置文件

JianerMate 有自己的配置文件，默认 `data/jianer_mate/config.json`：

- 首次启用（或 `~主动发言 重载` 时文件不存在）会按当前生效值自动生成一份，包含全部可调项；
- **文件里的值优先于宿主 `config.json → others` 里的 `jianer_mate_*` 键**，宿主键只作为兜底与兼容；
- 位置由宿主键 `jianer_mate_config_path` 决定（只有宿主配置能改它，避免自指）；
- 改完发 `~主动发言 重载` 生效；`~主动发言 状态` 会显示路径、已加载项数以及解析错误；
- 文件损坏不会让插件崩溃：会打印错误并退回 `config.others`，状态里给出具体原因；
- 除了嵌套结构，也接受直接写 `jianer_mate_*` 扁平键（方便从宿主配置粘贴）。

完整字段见 `plugins/JianerMate/config.example.json`，结构为
`enabled / data_dir / ai_config_dir / models / moderation / persona / style /
proactive / context / memes / limits / tools / group_blacklist`。

## 工具调用

Replyer 会走标准 OpenAI `tools` 协议：需要实时信息时先调用工具，再输出最终的短句
JSON。工具实现直接复用仓库里 JianerAI 的 `plugins/JianerAI/tools`（只导入模块，
**不需要启用 JianerAI 插件**）；导入失败时自动降级为无工具模式，聊天不受影响。

| 工具 | 说明 |
| --- | --- |
| `get_current_time` / `calculate_expression` | 当前时间、只含数字与基础运算符的安全算术 |
| `get_current_user_profile` / `get_current_chat_info` | 当前发言人、当前会话的基础资料 |
| `read_recent_chat` / `search_current_chat` | 当前会话最近聊天与文字搜索（读本插件自己的聊天表） |
| `web_search` / `github_repository` | 联网搜索摘要与 GitHub 仓库只读查询 |
| `render_information_card` | 生成 PNG 信息卡并直接发到当前会话（需要适配器支持发图） |
| `list_agent_skills` / `load_agent_skill` / `read_agent_skill_resource` | 读取 `.agents/skills/**/SKILL.md` 技能说明（目录存在时才注册） |
| `qweather_*` | 配好 `.env` 的和风天气工具；缺配置时不会注册 |
| 只读平台 API（`milky_get_*` / `onebot_get_*` / Lark GET） | 需要群主/管理员或机器人管理员身份，随权限自动可见 |
| `web_browser`、平台写操作、长期记忆工具 | 默认不放开：`web_browser` 需 `include_web_browser`，写操作需 `allow_mutating` |

`tools` 段配置：

- `tools.enabled`：总开关，默认 `true`；
- `tools.max_rounds`：单次回复最多几轮工具调用，默认 `3`；
- `tools.allowlist`：允许暴露给模型的工具名列表，默认是上表里的 9 个只读工具；
  写 `[]` 表示彻底关闭工具，写 `null` 表示不做白名单限制；
- `tools.skills_dirs` / `tools.skills_max_file_bytes`：Agent Skills 目录（默认
  `.agents/skills`）与单个技能文件上限；
- `tools.include_web_browser`：注册 `web_browser`，默认 `false`（会启动浏览器）；
- `tools.allow_mutating`：允许注册写操作与平台 API 工具，默认 `false`；
- `tools.max_output_chars`：单次工具结果注入模型的长度上限，默认 `4000`。

权限沿用 JianerAI 的工具契约：声明需要权限的工具只对群主/管理员或机器人管理员
可见；长期记忆类工具在本插件里会明确回复“没有启用长期记忆”，不会静默失败。

## 配置

优先读取插件配置文件 `data/jianer_mate/config.json`，宿主 `config.others` 作为兜底；
所有键都有默认值，模型定义复用 `aiconfig/*.ai.json`：

- `jianer_mate_config_path`：插件配置文件位置（只在这里配置，默认 `data/jianer_mate/config.json`）。
- `jianer_mate_enabled`：插件总开关，默认 `true`。
- `jianer_mate_planner_model`：Planner 使用的 aiconfig 模型代码（文本模型即可）。
- `jianer_mate_replyer_model`：Replyer 使用的模型代码，必须支持图片输入（视觉挑选表情包）。
- `jianer_mate_moderation_enabled` / `jianer_mate_moderation_model`：可选审核，默认关闭。
- `jianer_mate_persona`：单一人设，
  `{name, personality, behavior_style, reply_style, group_chat_prompt, private_chat_prompt, multiple_reply_styles, multiple_probability}`。
- `jianer_mate_reply_max_parts` / `jianer_mate_reply_max_chars`：单次最多几条、每条目标字数。
- `jianer_mate_reply_delay_ms`：条间延迟，`[600, 1800]` 或 `{"min":600,"max":1800}`。
- `jianer_mate_typo_probability` / `jianer_mate_typo_correction`：错别字概率与引用纠正。
- `jianer_mate_kaomoji_probability` / `jianer_mate_kaomoji` / `jianer_mate_catchphrases`：颜文字与口癖。
- `jianer_mate_context_messages`：注入的最近消息条数，默认 40。
- `jianer_mate_history_keep_messages` / `jianer_mate_history_retention_days`：本地聊天保留策略。
- `jianer_mate_talk_value_default`：主动发言概率，默认 0.5。
- `jianer_mate_min_interval_seconds`：两次主动发言最小间隔，默认 30，`0` 关闭。
- `jianer_mate_debounce_seconds` / `jianer_mate_max_debounce_seconds`：消息防抖聚合窗口。
- `jianer_mate_quiet_hours_default`：默认免打扰时段，默认 `23:00-08:00`。
- `jianer_mate_group_blacklist`：永不主动发言的群号列表。
- `jianer_mate_mentioned_reply`：消息里出现名字/别名时是否跳过 talk_value 概率，默认 `true`。
- `jianer_mate_reply_at`：群内被动回复时是否在首条 @ 发言人，默认 `false`（引用不受影响）。
- `jianer_mate_backoff_base_seconds` / `jianer_mate_backoff_cap_seconds` /
  `jianer_mate_backoff_start_count`：连续 N 次决定沉默后逐步放慢主动检查
  （默认 2 次后 15 秒起、最高 300 秒），说话成功后立即重置。
- `jianer_mate_max_concurrent_ai`：Planner/Replyer 并发上限，默认 2。
- `jianer_mate_meme_enabled` / `jianer_mate_steal_emoji`：表情包开关与自动收集。
- `jianer_mate_meme_max_entries` / `jianer_mate_meme_max_size_mb`：库容量与单张大小上限。
- `jianer_mate_meme_candidates` / `jianer_mate_meme_thumbnail_px`：候选数量与缩略图边长。
- `jianer_mate_meme_reuse_cooldown_seconds`：同一张表情的最短复用间隔。

模型协议限制：只支持 `ResponseType` 为 OpenAI 兼容（`OpenAI` / `OpenAI-Compatible` /
`Chat Completions`）的条目；Gemini、Anthropic、OpenAI Responses 条目会在
`~主动发言 状态` 里标记为「不支持」。

## 数据

- `data/jianer_mate/mate.db`：会话开关、最近消息、决策记录、表情包索引。
- `data/jianer_mate/memes/`：表情包文件；发送时只允许读取该目录内的文件。
- 聊天只保留最近 `history_keep_messages` 条且不超过 `history_retention_days` 天，
  没有长期记忆与摘要。

## 已知限制

- 单人设，不支持多角色切换；没有 Agent 工具、MCP、TTS、后缀与长期记忆。
- 飞书为尽力支持（文本可用，表情包不保证）；目标平台是 QQ OneBot/Milky。
- Replyer 模型若不支持图片，会自动退化为纯文字回复，不再发送表情包。
