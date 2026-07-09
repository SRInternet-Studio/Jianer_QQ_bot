# 新式插件运行时说明

本项目插件已迁移到 JianerCore 新式插件系统，不再通过 `main.py` 的反射参数注入向插件传递任意变量。

## 插件入口

单文件插件：

```python
from jianer.plugins import PluginMetadata

__plugin_meta__ = PluginMetadata(name="jianerbot-plugin-example")


async def dispatch(event, actions):
    return False
```

目录插件：

```text
plugins/
└── example/
    └── setup.py
```

目录插件入口固定为 `setup.py`。

## 可用上下文

插件通过 `bot.plugin_state` 获取项目侧上下文：

- `current_stage()`：当前派发阶段，例如 `always` 或 `command`。
- `current_order()`：命令阶段解析出的命令内容，不包含机器人触发前缀。
- `get_runtime()`：项目运行时状态字典。
- `websocket_url()`：当前配置连接对应的 websocket 地址。

常见 `runtime` 字段：

- `reminder`：机器人触发前缀。
- `bot_name` / `bot_name_en`：机器人名称。
- `root_users` / `super_users` / `manage_users`：权限组。
- `admins` / `supers`：绑定平台用户后的运行时权限快照。
- `cooldowns`：ACG 生图冷却表。
- `cooldowns1`：Pixiv 生图冷却表。
- `generating`：Pixiv 生图并发状态。
- `confused_word`：无权限提示模板。

## 事件与发送

`dispatch(event, actions)` 接收 JianerCore 事件与 actions：

- `event.message`：消息链。
- `event.msg_str`：消息文本（框架可提供时）。
- `event.user_id`：发送者 ID。
- `event.group_id`：群 ID，私聊事件通常没有。
- `actions.send(...)`：发送消息。

常用消息段来自 `jianer.segments`：

- `Segments.Text("文本")`
- `Segments.Image("file:///D:/path/image.png")`
- `Segments.At(user_id)`
- `Segments.Reply(message_id)`

消息容器来自 `jianer.common`：

```python
from jianer import common as Manager, segments as Segments

await actions.send(
    group_id=event.group_id,
    message=Manager.Message(Segments.Text("hello")),
)
```

## 插件管理

- `PluginMetadata.name` 必须使用 `jianerbot-plugin-{name}`。
- 文件或目录以 `d_` 开头会被视为禁用插件。
- 插件加载与派发由 JianerCore `PluginManager` 负责，项目内不再提供旧式关键词函数 loader。
