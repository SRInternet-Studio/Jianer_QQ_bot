# MaimaiDX for JianerCore

这是 `nonebot-plugin-maimaidx` 的 JianerCore 全量移植，基线为
`v3.1.4` / `7867b359d90f0ada0ddb4410d714c17bd7205387`。上游 MIT
许可证保存在 `LICENSE.upstream`，绘图中的原作者署名不得移除。

## 运行要求

- Python 3.11/3.12 与项目 `requirements.txt` 中的依赖。
- OneBot 11 或 Milky 协议，支持当前项目使用的群聊和私聊消息。
- Playwright Chromium。首次启动会按配置在后台自动安装；Linux 若缺少系统库，
  仍需管理员执行 `python -m playwright install --with-deps chromium`。

## 静态资源

默认自动下载并合并 `Resource CN1.55.7z` 和
`Resource CN1.56 UPDATE.7z`。安装位置由 `MAIMAIDX_PATH` 指定，默认为
`data/maimaidx/static`。下载期间文字查询可用，图片功能会提示资源准备中。

也可用 `MAIMAIDX_RESOURCE_ARCHIVE` 指向本地 7z 离线包，或用
`MAIMAIDX_RESOURCE_URL` / `MAIMAIDX_RESOURCE_UPDATE_URL` 覆盖下载地址。

## 配置

配置从仓库根目录 `.env` 读取：

```dotenv
MAIMAIDX_PATH=data/maimaidx/static
MAIMAIDX_STATE_PATH=data/maimaidx/private
MAIMAIDX_AUTO_DOWNLOAD=true
MAIMAIDX_AUTO_INSTALL_CHROMIUM=true
MAIMAIDX_ALIAS_PUSH=true
MAIMAIDX_ALIAS_PROXY=false
SAVE_IN_MEMORY=true
ASSETS_ONLINE=true

DIVINGFISH_CLIENT_ID=
DIVINGFISH_CLIENT_SECRET=
DIVINGFISH_SCOPE=prober.profile.read
DIVINGFISH_AUTH_URL=https://auth.diving-fish.com
DIVINGFISH_PROBER_PROXY=false

LXNS_DEV_TOKEN=
LX_CLIENT_ID=
LX_CLIENT_SECRET=
REDIRECT_URI=urn:ietf:wg:oauth:2.0:oob
LXNS_BIND_PRIVATE_ONLY=true
```

水鱼绑定使用设备授权和确认码流程，BOT 不保存用户令牌。落雪绑定支持 OAuth
授权码或完整回调链接；令牌保存在 `MAIMAIDX_STATE_PATH`，Windows 下使用
DPAPI，Linux 下收紧文件权限。

## 功能入口

- 成绩：`b50`、`ap50`、`info` / `minfo`、`ginfo`、`分数线`、排名。
- 授权：`lxbind`、`dfbind`、`数据源`、`主题`。
- 曲目：查歌、`id`、今日舞萌、随机曲目和推分建议。
- 别名：查询、添加、申请、投票和 SSE 推送开关。
- 猜歌：猜歌、猜曲绘、重置及群开关。
- 表格：定数表、完成表、进度表、等级进度和分数列表。
- 管理：更新曲目数据、定数表和完成表。

## JianerAI Tools

插件依赖 `jianerbot-plugin-jianer-ai`，加载与热重载时注册以下工具：

- `maimaidx_b50`
- `maimaidx_song_search`
- `maimaidx_song_info`
- `maimaidx_player_song_score`
- `maimaidx_rating_ranking`

图片工具只在适配器声明 `SEND_IMAGE` 能力且协议为 OneBot/Milky 时提供。他人
B50 查询只使用水鱼公开视图，不会读取或返回目标用户的落雪 OAuth 凭据。

## 验证

```bash
PYTHONPATH="$PWD" python -m pytest tests/test_maimaidx_*.py -q
PYTHONPATH="$PWD" python -m pytest tests/ -q
```

自动化替身只验证命令、状态机、适配层和安全契约；真实查分器、OAuth 与平台
消息仍需部署环境验收。
