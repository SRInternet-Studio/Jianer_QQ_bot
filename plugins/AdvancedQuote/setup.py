import os

from jianer import common as Manager, segments as Segments
from jianer.events import gen_message
from jianer.plugins import PluginMetadata

import plugins.AdvancedQuote.AdvancedQuote as Quote
from bot import plugin_state


__plugin_meta__ = PluginMetadata(
    name="jianerbot-plugin-advanced-quote",
    description="Render a quoted message as an image quote.",
    usage="{reminder}名人名言【引用一条消息】 —> 将消息载入史诗",
    requires={"jianerbot-plugin-alconna"},
)


async def dispatch(event, actions):
    if plugin_state.current_stage() != "command":
        return False
    order = plugin_state.current_order()
    if order != "名人名言" and not order.startswith("名人名言 "):
        return False

    image_url = None
    if not getattr(event, "message", None) or not isinstance(event.message[0], Segments.Reply):
        await actions.send(
            group_id=event.group_id,
            message=Manager.Message(Segments.Reply(event.message_id), Segments.Text("在记录一条名言之前先引用一条消息噢 ☆ヾ(≧▽≦*)o")),
        )
        return True

    content = await actions.get_msg(event.message[0].id)
    if not content.data:
        await actions.send(
            group_id=event.group_id,
            message=Manager.Message(Segments.Reply(event.message_id), Segments.Text("记录一条名言所引用的消息必须是图文噢 ヾ(ﾟ∀ﾟゞ)")),
        )
        return True

    message = gen_message({"message": content.data["message"]})
    for segment in message:
        if isinstance(segment, Segments.Image):
            image_url = segment.file if str(segment.file).startswith("http") else segment.url
            print(image_url)

    quote_image = await Quote.handle(event.message, actions, image_url, Manager, Segments)
    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), quote_image))
    if os.path.exists("./temps/web_.png"):
        os.remove("./temps/web_.png")
    return True
