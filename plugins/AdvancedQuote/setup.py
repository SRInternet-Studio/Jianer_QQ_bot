import plugins.AdvancedQuote.AdvancedQuote as Quote
from hyperot import configurator as Configurator
Configurator.ensure_config_manager(file="config.json")

TRIGGHT_KEYWORD = "名人名言"
HELP_MESSAGE = f"{Configurator.cm.get_cfg().others['reminder']}名人名言【引用一条消息】 —> {Configurator.cm.get_cfg().others['bot_name']}将消息载入史诗"

async def on_message(event, actions, Manager, Segments, os, gen_message):
        imageurl = None
        if isinstance(event.message[0], Segments.Reply):
            content = await actions.get_msg(event.message[0].id)
            if not content.data:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text("记录一条名言所引用的消息必须是图文噢 ヾ(ﾟ∀ﾟゞ)")))
                return True
            
            message = gen_message({"message": content.data["message"]})
            for i in message:
                if isinstance(i, Segments.Image):
                    if i.file.startswith("http"):
                        imageurl = i.file
                    else:
                        imageurl = i.url
                    print(imageurl)
                    
            quoteimage = await Quote.handle(event.message, actions, imageurl, Manager, Segments)
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), quoteimage))
            os.remove("./temps/web_.png")
        else:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text("在记录一条名言之前先引用一条消息噢 ☆ヾ(≧▽≦*)o")))
        return True

