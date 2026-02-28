from Tools.log_helper import project_log
import plugins.Quote.Quote as Quote
from Hyper import Configurator
Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())

TRIGGHT_KEYWORD = "名言"
HELP_MESSAGE = f"{Configurator.cm.get_cfg().others['reminder']}名言【引用一条消息】 —> {Configurator.cm.get_cfg().others["bot_name"]}将消息载入史册"

async def on_message(event, actions, Manager, Segments, os, gen_message):
        project_log("获取名言")
        imageurl = None
        if isinstance(event.message[0], Segments.Reply):
            content = await actions.get_msg(event.message[0].id)
            project_log(content.data)
            message = gen_message({"message": content.data["message"]})
            for i in message:
                if isinstance(i, Segments.Image):
                    project_log("应该有图")
                    if i.file.startswith("http"):
                        imageurl = i.file
                    else:
                        imageurl = i.url
                    project_log(imageurl)
                    
            quoteimage = await Quote.handle(event.message, actions, imageurl)
            project_log("制作名言")
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), quoteimage))
            os.remove("./temps/quote.png")
        else:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text("在记录一条名言之前先引用一条消息噢 ☆ヾ(≧▽≦*)o")))
        return True
