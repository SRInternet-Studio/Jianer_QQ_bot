import os

from jianer import common as Manager, segments as Segments
from jianer.events import gen_message
from jianer.plugins import PluginMetadata
from jianer.plugins.builtin.alconna import Command, Target, UniMessage

import plugins.AdvancedQuote.AdvancedQuote as Quote
from bot import plugin_state


__plugin_meta__ = PluginMetadata(
    name="jianerbot-plugin-advanced-quote",
    description="Render a quoted message as an image quote.",
    usage="{reminder}名人名言【引用一条消息】 —> 将消息载入史诗",
    requires={"jianerbot-plugin-alconna"},
)


_REMINDER = str(plugin_state.get_runtime().get("reminder", ""))


@Command(f"{_REMINDER}名人名言").handle()
@Command(f"{_REMINDER}名人名言 <extra>").handle()
async def _handle_quote(event, actions, user_message, extra: str = ""):
    if getattr(event, "group_id", None) is None:
        return False

    image_url = None
    target = Target.group(event.group_id)
    if not user_message or not isinstance(user_message[0], Segments.Reply):
        await UniMessage.send(
            UniMessage.reply(event.message_id),
            UniMessage.text("在记录一条名言之前先引用一条消息噢 ☆ヾ(≧▽≦*)o"),
            target=target,
            actions=actions,
        )
        return True

    content = await actions.get_msg(user_message[0].id)
    if not content.data:
        await UniMessage.send(
            UniMessage.reply(event.message_id),
            UniMessage.text("记录一条名言所引用的消息必须是图文噢 ヾ(ﾟ∀ﾟゞ)"),
            target=target,
            actions=actions,
        )
        return True

    message = gen_message({"message": content.data["message"]})
    for segment in message:
        if isinstance(segment, Segments.Image):
            image_url = segment.file if str(segment.file).startswith("http") else segment.url
            print(image_url)

    try:
        quote_image = await Quote.handle(user_message, actions, image_url, Manager, Segments)
        await UniMessage.send(
            UniMessage.reply(event.message_id),
            UniMessage(quote_image),
            target=target,
            actions=actions,
        )
    finally:
        if os.path.exists("./temps/web_.png"):
            os.remove("./temps/web_.png")
    return True
