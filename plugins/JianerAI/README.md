# JianerAI

`jianerbot-plugin-jianer-ai` 是简儿的 JianerCore 目录插件，统一承载 AI
对话、模型与角色切换、短期上下文、简儿记忆、回复后缀、TTS、图片与引用
解析。入口固定为 `setup.py`，依赖内置
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

模型配置继续从 `aiconfig/*.ai.json` 读取；角色数据继续使用
`prerequisites/current.json` 与相邻模板；后缀继续使用
`suffix_config.json`。不要提交包含密钥的本地配置。

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
