# 更新日志

# JianerNext4 dev 20251227a

### 新增
- 接入 `ARC_Spec_Python`，并引入 `Tools/ARC_AI.py` 作为统一 AI 桥接层。
- 新增 `aiconfig/` + `parser/` 目录，用于 ARC 风格的 AI 配置与解析器扩展。
- 新增 AI 管理指令：
  - `#ai管理菜单`：按 `FriendlyName` + 配置代码列出可用 AI。
  - `#切换AI <config_name>`：运行时切换当前调用的 AI 配置。

### 变更
- 将 `main.py` 中旧的 AI 切换逻辑替换为通过 `Tools.ARC_AI.get_response_stream` 的 ARC 路由。
- 将 `main.py` 的消息流处理更新为支持异步生成器（`async for`）。
- 更新 Gemini 解析器行为：
  - 兼容读取配置里的 `BaseUrl`/`BaseURL`。
  - 当 `BaseUrl` 已包含 `/v1beta` 时，避免重复拼接导致 `/v1beta/v1beta`。
  - `generateContent` 统一使用 `?key=...` 的鉴权方式，以兼容常见反代端点。
  - 请求构建时使用传入的 `history`，保证上下文在切换/注销后表现一致。

### 修复
- 修复 ARC 初始化时因缺失 `arcspec_ai.parsers.openai` 导致的导入崩溃。
- 修复事件循环中调用 `asyncio.run()` 导致的报错，统一通过 `Tools/ARC_AI.py` 处理异步调用。
- 修复移除旧 Gemini 上下文管理后出现的 `NameError: cmc is not defined`。

