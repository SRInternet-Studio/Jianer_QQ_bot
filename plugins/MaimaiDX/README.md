# MaimaiDX for JianerCore

这是 `nonebot-plugin-maimaidx` 的 JianerCore 目录插件移植，固定上游版本为
`v3.0.13` / `83a1bee46fad81ad4436b6fba5863ac4d2abb976`。上游 MIT
许可证保存在 `LICENSE.upstream`，绘图中的原作者署名不得移除。

## 运行要求

- Python 3.11+（当前项目使用 JianerCore 0.92.1）。
- `requirements.txt` 中的 aiofiles、aiosqlite、Pydantic、Pyecharts、
  Playwright、httpx-ws、NumPy、SQLModel 和 Pillow。
- Playwright Chromium：`python -m playwright install chromium`。
- 上游静态资源解压到 `data/maimaidx/static`，或通过
  `MAIMAIDX_PATH` 指向其他绝对/相对目录。

资源目录必须至少包含字体、`mai/pic`、`mai/cover`、`mai/shougou`、
`mai/plate_version` 和 `mai/cover/0.png`。默认 `ASSETS_ONLINE=true` 时名牌
资源可在线读取；设为 `false` 时还必须提供 `mai/plate`。

## 本地配置

凭据只写入仓库已忽略的 `.env`，不要写入 `config.json`、源码或测试：

```dotenv
MAIMAIDX_PATH=data/maimaidx/static
MAIMAIDX_STATE_PATH=data/maimaidx/private
MAIMAIDX_ALIAS_PUSH=true
MAIMAIDX_ALIAS_PROXY=false
SAVE_IN_MEMORY=true
ASSETS_ONLINE=true

DIVINGFISH_TOKEN=
DIVINGFISH_PROBER_PROXY=false

LXNS_DEV_TOKEN=
LX_CLIENT_ID=
LX_CLIENT_SECRET=
REDIRECT_URI=urn:ietf:wg:oauth:2.0:oob
LXNS_BIND_PRIVATE_ONLY=true
```

图片中的中文机器人名读取 `config.json` 的 `bot_name`；英文署名优先读取
`bot_name_en`，未配置时自动回退到 `bot_name`。

创建落雪 OAuth 应用时勾选“无回调地址”，回调地址使用固定值
`urn:ietf:wg:oauth:2.0:oob`，权限只开启“读取玩家数据”。插件也以此作为
`REDIRECT_URI` 未配置时的默认值。

无回调绑定只接受私聊。先发送 `lxbind`，打开本次生成的链接并授权，再把页面
显示的授权码直接发回机器人（也可发送 `授权码：...`）。授权码必须对应当前 QQ
在当前 Bot 上十分钟内创建的绑定会话，提交期间会被原子锁定，成功后立即销毁；
群聊提交、过期会话和重复提交都会被拒绝。若显式配置普通 HTTP(S) 回调地址，
则仍必须提交同时包含 `code` 与 `state` 的完整回调链接。

令牌交换和刷新遵循落雪 OAuth 2.0 接入指南：优先从响应顶层读取
`access_token`、`refresh_token`、`token_type`、`expires_in` 与 `scope`；
旧版 `data` 包装只作为暂时兼容。错误响应按顶层 `error` 和
`error_description` 解析，不依赖旧版 `success` / `code` / `data` 格式。

绑定、数据源与主题始终修改消息发送者本人的记录。`@` 他人查询只会使用水鱼
查分器的公开 QQ 查询，不会读取被提及用户保存的落雪查分器 OAuth 凭据或私有成绩。

OAuth 数据库与静态素材目录分离，默认存放在
`data/maimaidx/private/user.db`。Windows 启动时会将 OAuth Token 使用当前
服务账户的 DPAPI 加密，并把该目录及 `.env` 的 ACL 收紧到当前账户、SYSTEM
和管理员；旧版 `MAIMAIDX_PATH/data/user.db` 会在首次启动时以主库最后切换的
可重试流程迁移。
`DIVINGFISH_PROBER_PROXY=true` 只允许无凭据请求使用第三方代理；只要配置了
Developer Token，插件就会拒绝代理并直连水鱼查分器官方 API。

## 功能入口

- 成绩：`b50`、`b50 @成员`、`b50 QQ号`、`ap50`、`info` / `minfo`、
  `ginfo`、`分数线`。
- 账号与设置：`lxbind`、`数据源`、`主题`。
- 曲目：`查歌`、`定数查歌`、`bpm查歌`、`曲师查歌`、`谱师查歌`、
  `id`、`今日舞萌`、随机曲目和推分语句。
- 别名：查询、本地别名、申请、投票、投票状态、群/全局推送开关。
- 猜歌：`猜歌`、`猜曲绘`、`重置猜歌`、开启/关闭群猜歌。
- 表格：定数表、完成表、进度表、等级进度、分数列表和牌子条件。
- 管理：更新曲库、别名库、定数表和完成表。

## JianerAI Tools

插件依赖 `jianerbot-plugin-jianer-ai`，加载与热重载时会自动注册、注销以下
Agent Tool：

- `maimaidx_b50`：查询当前发言者、当前消息中被 `@` 的成员、显式 QQ、
  当前群上下文/持久会话记忆中可唯一解析的群名片或昵称，或公开水鱼用户名，
  并发送 B50 图片。
- `maimaidx_song_search`：按曲名、Yuri-YuzuChaN 别名、ID、定数、BPM、
  曲师或谱师返回结构化曲目结果。
- `maimaidx_song_info`：发送指定曲目的谱面信息图片。
- `maimaidx_player_song_score`：发送当前发言者指定单曲的水鱼成绩图片。
- `maimaidx_rating_ranking`：发送水鱼查分器公开 Rating 排行榜图片。

发送图片的 Tool 只在当前 OneBot/Milky 会话声明 `SEND_IMAGE` 能力时提供。
他人 B50 查询强制使用水鱼查分器公开视图，不会读取、返回或修改目标用户的落雪
OAuth Token。姓名只做精确匹配；没有记录或出现重名时，Tool 会要求改用 `@` 或
QQ 号，不会猜测目标。
别名申请、投票、数据更新和管理操作不会暴露给 AI。若 JianerAI 配置了
`agent_allowed_tools` 白名单，需要把希望开放的上述 Tool 名称显式加入白名单。
发送 `~Agent 开启` 后，可用 `~Agent 工具` 查看当前协议、模型和权限下实际可用的
Tool。

OneBot 11 与 Milky 使用各自的响应队列；群列表、图片、合并转发和回复
均通过 Jianer 适配器完成。插件由 `bot/plugin_state.py` 中唯一的
`PluginManager` 加载、派发和热重载。

## 验证

```powershell
$env:PYTHONPATH = (Get-Location).Path
.venv\Scripts\python.exe -c "from jianer.plugins import PluginManager; r=PluginManager().load_plugins('plugins'); assert not r.failed"
.venv\Scripts\python.exe -m pytest tests\ -q --basetemp=.pytest-tmp-maimaidx
.venv\Scripts\python.exe -m pip check
```

曲目别名默认合并 `Yuri-YuzuChaN别名数据源`、落雪查分器别名数据和本地
别名。`Yuri-YuzuChaN别名数据源` 只负责歌曲别名，不是第三个查分器。

自动化测试中的本地替身只验证命令和异常契约，不代表真实查分器、OAuth、
QQ 适配器或端到端结果。真实验收必须另外记录实际服务响应、实际资源渲染和
实际 OneBot/Milky 消息结果。
