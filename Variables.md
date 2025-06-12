## 插件可供调用的参数（部分常用）
**本文格式：参数名: 参考值（非实际提供值，大部分参数有相应注解注意甄别）**

### 变量
1. ```'__name__'```: ```'__main__'```
> 运行模块名称

2. ```'__file__'```: ```'/root/Jianer/main.py'```
> 简儿主程序的入口文件路径

3. ```'bot_name'```: ```'简儿'```
> 机器人的名称

4. ```'bot_name_en'```: ```'Jianer'```
> 机器人的英文名称

5. ```'user_message'```: ```'-你好呀'```
> 用户发送的消息

6. ```'order'```: ```'你好呀'```
> 用户发送的消息（不包含机器人触发关键词）

7. ```'event_user'```: ```'简儿'```
> 发送消息的用户昵称

8. ```'version_name'```: ```'3.0-NextPreviewUltra'```
> 简儿的项目版本号

9. ```'cooldowns'```: ```{}```
> 用于 **生图 ACG** 的个人冷却时间列表，键名对应每个具有冷却使用时间的用户的QQ号，键值对应冷却剩余时间。

10. ```'cooldowns1'```: ```{}```
> 用于 **生图 Pixiv** 的个人冷却时间列表，键名对应每个具有冷却使用时间的用户的QQ号，键值对应冷却剩余时间。

11. ```'second_start'```: ```65536```
> 机器人累计运行时长，以秒为单位

12. ```'EnableNetwork'```: ```'Pixmap'```
> 当前机器人的AI回复模式， ```Pixmap``` 对应**读图**，即 Google Gemini 模型； ```Normal``` 对应**默认3.5**，即 ChatGPT 3.5 turbo 16k 模型； ```Net``` 对应**默认4**，即 ChatGPT 4o mini 模型

13. ```'user_lists'```: ```{}```
> ChatGPT 系列模型的用户上下文，每个键名对应一个用户的QQ号，每个键值对应其上下文

14. ```'in_timing'```: ```True```
> 指示机器人是否已经进入事件循环，此变量在机器人已经启动完成后均为 ```True```

15. ```'generating'```: ```False```
> 指示机器人是否正在从 Pixiv 生成图片

16. ```'emoji_send_count'```: ```12.34```
> 指示 emoji 复述功能的设定间隔已经经过了多少秒（原生设定复述一次之后间隔15秒）

17. ```'gptsovitsoff'```: ```'False'```
> 指示是否为机器人的AI回复启用了 tts 语音回复功能

18. ```'generation_config'```: ```{
    'temperature': 1,
    'top_p': 0.95,
    'top_k': 64,
    'max_output_tokens': 8192,
    'response_mime_type': 'text/plain'
}```
> Google Gemini 模型的回复生成配置，上文的原生配置清单设定了最大回复长度64k，最大消耗token数目8192，回复类型为文本（text）

19. ```'tools'```: ```[]```
> Google Gemini 模型可以用于生成回复使用的工具列表

20. ```'model'```: ```genai.GenerativeModel(model_name='models/gemini-pro',
generation_config=generation_config,
safety_settings={},
tools=None,
system_instruction=None,
cached_content=None)```
> Google Gemini 模型的设置，上文的原生配置清单设定了回复生成配置、回复内容安全设置、模型可以用于生成回复使用的工具等

21. ```'key'```: ```''```
> Google Gemini 模型的 API Key 

22. ```'reminder'```: ```'-'```
> 机器人的触发关键词（符号）

23. ```'sys_prompt'```: ```'你叫简儿……'```
> 机器人当前的AI回复预设

24. ```'ROOT_User'```: ```[
    '123456789'
]```
> ROOT_User 用户组的用户列表，由QQ号组成

25. ```'Super_User'```: ```[
    '123456789'
]```
> Super_User 用户组的用户列表，由QQ号组成

26. ```'Manage_User'```: ```[
    '123456789'
]```
> Manage_User 用户组的用户列表，由QQ号组成

27. ```'sisters'```: ```[
    '0987654321'
]```
> 使用 “做我姐姐吧” 预设的用户列表，由QQ号组成

28. ```'jhq'```: ```[
    '987654321'
]```
> 使用 《工作细胞》 预设的用户列表，由QQ号组成

29. ```'PLUGIN_FOLDER'```: ```'plugins'```
> 插件存放的目录名称

30. ```'loaded_plugins'```: ```[
    'SoGood_54c9bf1cad5f4024a398fe45b2acf29a',
    'Hitokota_a97eb7c5a2f0496499a3e804d84fcbdc',
    'HelloWorld_9704e6f963a34389ae7eb8288767675d'
]```
> 已经加载成功的插件，插件名称+独立uuid

31. ```'disabled_plugins'```: ```[]```
> 已经被禁用的插件（忽略加载），插件名称+独立uuid

32. ```'failed_plugins'```: ```[]```
> 加载失败的插件，插件名称+加载失败原因

### 模块
1. 以下均为内置库或第三方库，详细调用方法请见各个库官方的详细说明。
```
'faulthandler': <module'faulthandler'(built-in)>,
'asyncio': <module'asyncio'from'/usr/lib/python3.12/asyncio/__init__.py'>,
'datetime': <module'datetime'from'/usr/lib/python3.12/datetime.py'>,
'os': <module'os'(frozen)>,
'importlib': <module'importlib'from'/usr/lib/python3.12/importlib/__init__.py'>,
'sys': <module'sys'(built-in)>,
'inspect': <module'inspect'from'/usr/lib/python3.12/inspect.py'>,
'random': <module'random'from'/usr/lib/python3.12/random.py'>,
'uuid': <module'uuid'from'/usr/lib/python3.12/uuid.py'>,
're': <module're'from'/usr/lib/python3.12/re/__init__.py'>,
'base64': <module'base64'from'/usr/lib/python3.12/base64.py'>,
'urllib': <module'urllib'from'/usr/lib/python3.12/urllib/__init__.py'>,
'emoji': <module'emoji'from'/usr/local/lib/python3.12/dist-packages/emoji/__init__.py'>,
'time': <module'time'(built-in)>,
'traceback': <module'traceback'from'/usr/lib/python3.12/traceback.py'>,
'OpenAI': <class'openai.OpenAI'>,
'requests': <module'requests'from'/usr/lib/python3/dist-packages/requests/__init__.py'>,
'aiohttp': <module'aiohttp'from'/usr/local/lib/python3.12/dist-packages/aiohttp/__init__.py'>,
'Configurator': <module'Hyper.Configurator'from'/usr/local/lib/python3.12/dist-packages/Hyper/Configurator.py'>,
'platform': <module'platform'from'/usr/lib/python3.12/platform.py'>,
'psutil': <module'psutil'from'/usr/lib/python3/dist-packages/psutil/__init__.py'>,
'GPUtil': <module'GPUtil'from'/usr/local/lib/python3.12/dist-packages/GPUtil/__init__.py'>,
'subprocess': <module'subprocess'from'/usr/lib/python3.12/subprocess.py'>,
'Set': typing.Set,
'Image': <module'PIL.Image'from'/usr/local/lib/python3.12/dist-packages/PIL/Image.py'>,
'io': <module'io'(frozen)>,
'threading': <module'threading'from'/usr/lib/python3.12/threading.py'>,
'paramiko': <module'paramiko'from'/usr/local/lib/python3.12/dist-packages/paramiko/__init__.py'>,
```

2. ```'Listener'```
> 事件监听模块，详见[文档](https://harcicyang.github.io/hyper-bot/more/classes.html#listener-%E6%A8%A1%E5%9D%97)

3. ```'Events'```
> 事件类型模块，具有以下子目：
> 
> 'MessageEvent': <class'Hyper.Events.MessageEvent'>,
> 
> 'PrivateMessageEvent': <class'Hyper.Events.PrivateMessageEvent'>,
> 
> 'GroupMessageEvent': <class'Hyper.Events.GroupMessageEvent'>,
> 
> 'NoticeEvent': <class'Hyper.Events.NoticeEvent'>,
> 
> 'GroupFileUploadEvent': <class'Hyper.Events.GroupFileUploadEvent'>,
> 
> 'GroupAdminEvent': <class'Hyper.Events.GroupAdminEvent'>,
> 
> 'GroupMemberDecreaseEvent': <class'Hyper.Events.GroupMemberDecreaseEvent'>,
> 
> 'GroupMemberIncreaseEvent': <class'Hyper.Events.GroupMemberIncreaseEvent'>,
> 
> 'GroupMuteEvent': <class'Hyper.Events.GroupMuteEvent'>,
> 
> 'FriendAddEvent': <class'Hyper.Events.FriendAddEvent'>,
> 
> 'GroupRecallEvent': <class'Hyper.Events.GroupRecallEvent'>,
> 
> 'FriendRecallEvent': <class'Hyper.Events.FriendRecallEvent'>,
> 
> 'NotifyEvent': <class'Hyper.Events.NotifyEvent'>,
> 
> 'GroupEssenceEvent': <class'Hyper.Events.GroupEssenceEvent'>,
> 
> 'MessageReactionEvent': <class'Hyper.Events.MessageReactionEvent'>,
> 
> 'RequestEvent': <class'Hyper.Events.RequestEvent'>,
> 
> 'GroupAddInviteEvent': <class'Hyper.Events.GroupAddInviteEvent'>,
> 
> 详见[文档](https://harcicyang.github.io/hyper-bot/more/classes.html#events-%E6%A8%A1%E5%9D%97)

4. ```'Logger'```
> 日志类型模块，详见[文档](https://harcicyang.github.io/hyper-bot/more/classes.html#logger-py)

5. ```'Manager'```
> 消息内容模块，如 Manager.Message 表示一条消息，详见[文档](https://harcicyang.github.io/hyper-bot/more/classes.html#manager-%E6%A8%A1%E5%9D%97)

6. ```'Segments'```
> 消息类型模块，如 Segments.At 表示@一个人 的消息内容、 Segments.Text 表示纯文本消息内容、 Segments.Image 表示图片消息内容、 Segments.Video 表示视频消息内容、 Segments.Reply 表示回复一条消息的消息内容，以此类推。详见[文档](https://harcicyang.github.io/hyper-bot/more/classes.html#segments-%E6%A8%A1%E5%9D%97)

7. ```'Logic'```
> 详见[文档](https://harcicyang.github.io/hyper-bot/more/classes.html#logic-py)

### 类型
1. ```'plugins'```: ```[
    <module'HelloWorld_9704e6f963a34389ae7eb8288767675d'from'/root/Jianer/plugins/HelloWorld.py'>
]```
> 已加载的插件模块列表，可以直接调用其中的插件，也就可以实现插件调用其他插件的效果，但请注意传参规范，详见 ```main.py``` 中的 ```execute_plugin``` 方法

2. ```'event'```
> 当前已被触发的消息事件类型，通过判断其是 ```Events``` 的哪一个子目可以判断当前用户正在执行什么操作。可以[在这里](https://github.com/botuniverse/onebot-11/blob/master/event/README.md)找到它的更多有趣用法。

3. ```'actions'```
> 行动，用于操作机器人执行一系列操作，例如 ```actions.send()``` 可以操作QQ机器人向群内发送某些内容。可以[在这里](https://github.com/botuniverse/onebot-11/blob/master/api/public.md)找到它的更多有趣用法。

> [!Note]
>
> 本文当中提及的内容涵盖大部分开发者可能用到的参数用途指引，但这些并不是全部。**所有位于 ```main.py``` 中的变量、类型、方法等都可以作为参数被传递**，开发者们，你们发挥的时间到啦（๑✧∀✧๑）☀！
