from Hyper import Configurator
Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())

TRIGGHT_KEYWORD = "你好，世界"
HELP_MESSAGE = f"{Configurator.cm.get_cfg().others["reminder"]}你好，世界 —> 仅仅就是一句 Hello world 🤔？"

async def on_message(event, actions, Manager, Segments):
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("Hello, world! 🌍")))
        return True