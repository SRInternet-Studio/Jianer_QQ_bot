import httpx
from Hyper import Configurator
Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())

TRIGGHT_KEYWORD = "一言"
HELP_MESSAGE = f"{Configurator.cm.get_cfg().others["reminder"]}一言 —> 找一句好听的名言👍"

async def on_message(event, actions, Manager, Segments, bot_name):
    response = httpx.get("https://international.v1.hitokoto.cn/")
    try:
        txt = f"{response.json()['hitokoto']} —— {response.json()['from_who']}, {response.json()['from']}"
    except:
        txt = f"请求失败 - {bot_name}"
    await actions.send(
        group_id=event.group_id,
        user_id=event.user_id,
        message=Manager.Message(Segments.Text(txt))
    )

    return True #阻止继续执行其他功能