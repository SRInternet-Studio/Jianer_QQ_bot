# RollPigPlus for JianerCore

`nonebot-plugin-rollpig-plus`（今天是什么小猪 Plus）的 JianerCore 移植，基线为
`v0.14.4` / `bd8681f62bc3d24153af52b0090bde3e35c5a791`。插件完全运行在
JianerCore 插件 API 上：Alconna `Command` 注册命令，`actions` 收发消息，
`PluginMetadata` 声明元数据，**不依赖 NoneBot**。上游 MIT 许可证保存在
`LICENSE.upstream`，第三方资源声明保存在 `THIRD_PARTY_NOTICES.upstream.md`。

## 目录结构

```
plugins/RollPigPlus/
├── setup.py                     # 插件入口：PluginMetadata、订阅、启停
├── host/                        # 插件与 JianerCore 的绑定层
│   ├── logger.py                # 转发到宿主 logger
│   ├── paths.py                 # ROLLPIG_PATH 数据/缓存目录
│   ├── settings.py              # ROLLPIG_* 环境变量与 JSON 配置
│   ├── messages.py              # Message / MessageSegment（JianerCore 消息段）
│   ├── bot.py                   # actions 包装：发送、群成员查询、call_api
│   ├── lifecycle.py             # on_startup / on_shutdown / on_bot_connect
│   └── scheduler.py             # asyncio 定时任务（cron / interval）
└── rollpig/                     # 业务层（来自上游，框架相关部分已改写）
    ├── handlers/                # 全部 Alconna 命令 + on_message 钩子
    ├── store/                   # local / cloud 存储后端
    ├── data_manager.py          # 抽猪、图鉴、烤猪、日报的领域逻辑
    ├── card_renderer.py 等      # Pillow 渲染
    ├── jobs.py                  # 定时任务与生命周期钩子注册
    └── resource/                # 内置小猪资源与字体
```

上游的领域逻辑（存储、流程、渲染、文案）按原样复用；所有框架调用都改为
`host.*`，因此插件不引入任何第三方机器人框架。

## 运行要求

- 项目 `requirements.txt` 中的 `jianer-bot`、`pydantic`、`Pillow`、
  `pilmoji`、`httpx`、`openai`。
- OneBot 11 或 Milky 协议；飞书模式下插件自动跳过。
- 首次启动会在后台按配置同步小猪资源包；同步失败时回退到内置资源。

## 配置

配置来源按优先级：`.env`（`ROLLPIG_*`）> 仓库根目录 `rollpig_config.json`
或 `config/rollpig.json` > 插件默认值。`rollpig_config.example.json` 是可直接
改名的模板。

```dotenv
ROLLPIG_PATH=data/rollpig
# ROLLPIG_CONFIG_FILE=/path/to/rollpig_config.json
# ROLLPIG_STORAGE_BACKEND=local          # local / cloud
# ROLLPIG_CLOUD_API_URL=
# ROLLPIG_CLOUD_TOKEN=
# ROLLPIG_DAILY_SUMMARY_ENABLED=false    # 日报推送默认关闭
# ROLLPIG_ROAST_COOLDOWN_HOURS=8
# ROLLPIG_ROAST_CHARGE_MAX=2
# ROLLPIG_AI_ENABLED=false               # AI 生成烤猪文案
# ROLLPIG_AI_API_KEY=
# ROLLPIG_AI_BASE_URL=
# ROLLPIG_AI_MODEL=
# ROLLPIG_RESOURCE_SYNC_ENABLED=true
```

`ROLLPIG_PATH` 下的内容：

- `data/`：抽猪记录、图鉴与 EX 进度、烤猪事件、预约、日报开关。
- `cache/`：卡片渲染缓存、PigHub 索引、昨日回顾卡。

## 功能入口

- 抽猪：`今日小猪` / `今天是什么小猪`、`随机小猪 [数量]`、`找猪 <关键词>`、
  `明日小猪`、`昨日小猪`。
- 烧烤：`今日烤猪`、`烤群友 [@成员]`（支持引用消息）、`随机烤群友`、
  `加急生火`、`烤箱续火`（群内活跃玩家发起的补货投票）。
- 收集：`我的猪圈`、`小猪图鉴 [页码]`、`小猪投稿`、`本周小猪`。
- 日报：`猪圈日报`（查看最近一期已封版卡片）、
  `小猪日报 开启/关闭/状态`（群主、管理员或超级用户）。
- 管理：`同步小猪资源`（超级用户）。

命令同时接受 `~今日小猪` 与裸 `今日小猪` 两种写法；`随机小猪3`、`找猪玩偶`、
`小猪图鉴2` 这类黏连写法由 `handlers/fallback.py` 兼容。

## 定时任务

插件自带调度器（`host/scheduler.py`），随首次连接启动，宿主关闭时收束：

- 每天 03:30 数据保留清理。
- 每天 23:45 生成并推送日报（仅对已开启的群）。
- 每 30 秒检查一次次日保护失败的补偿队列。
- 按 `ROLLPIG_RESOURCE_SYNC_INTERVAL_HOURS`（默认 24 小时）同步资源与共享烤猪文案。

## 与上游的差异

- 命令边界：Alconna 严格匹配，无参数命令不会吞掉多余文本；需要参数的命令同时
  注册「无参数」与「带参数」两种形态，黏连写法由 `on_message` 钩子补齐。
- 事件来源：续标识投票依赖适配器上报 reaction/notice 事件（JianerCore 的
  `MessageReactionEvent`、`NotifyEvent`）。适配器不上报时命令仍可用，只是需要
  重新发送 `烤箱续火` 触发验票；`set_msg_emoji_like` / `fetch_emoji_like`
  需要 OneBot 实现提供对应扩展 API。
- 多 Bot 场景走同一份数据目录；`host.get_bots()` 由宿主当前连接填充。
- 云存储（`ROLLPIG_STORAGE_BACKEND=cloud`）与 PigHub、AI 烤猪按上游实现保留，
  默认关闭，未配置时退化为本地存储。
- 上游使用 apscheduler 与 localstore，本移植分别换成 `host/scheduler.py` 与
  `host/paths.py`，行为（cron/interval、数据目录布局）保持一致。

## 验证

```bash
python -m pytest tests/test_rollpig_plugin_contract.py tests/test_rollpig_store_flow.py -q
python -m pytest tests/ -q
```

仓库内测试覆盖插件元数据与「无 NoneBot 依赖」约束、`host` 绑定层（消息构造、
actions 包装、调度器、配置）、命令边界与黏连写法，以及抽猪落盘、图鉴统计、
烤猪、烤箱续火、日报开关、私聊与预约加入钩子的端到端流程。
