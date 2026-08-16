from jianer import common as Manager, segments as Segments
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


async def _send_text(event, actions, target, text: str) -> None:
    await UniMessage.send(
        UniMessage.reply(event.message_id),
        UniMessage.text(text),
        target=target,
        actions=actions,
    )


def _log_exception(message: str) -> None:
    logger = plugin_state.get_logger()
    if logger is not None:
        try:
            logger.exception(message)
        except Exception:
            pass


@Command(f"{_REMINDER}名人名言").handle()
@Command(f"{_REMINDER}名人名言 <extra>").handle()
async def _handle_quote(event, actions, user_message, extra: str = ""):
    if getattr(event, "group_id", None) is None:
        return False

    target = Target.group(event.group_id)
    quote_image = None
    try:
        if not user_message or not isinstance(user_message[0], Segments.Reply):
            await _send_text(
                event,
                actions,
                target,
                "在记录一条名言之前先引用一条消息噢 ☆ヾ(≧▽≦*)o",
            )
            return True

        content = await Quote.get_message_response(actions, user_message[0].id)
        message = Quote.response_message(content)
        if not getattr(content, "data", None) or not message:
            await _send_text(
                event,
                actions,
                target,
                "记录一条名言所引用的消息必须是图文噢 ヾ(ﾟ∀ﾟゞ)",
            )
            return True

        image_url = None
        for segment in message:
            if isinstance(segment, Segments.Image):
                image_url = Quote.image_source(segment)
                if image_url:
                    break

        quote_image = await Quote.handle(
            user_message,
            actions,
            image_url,
            Manager,
            Segments,
            content=content,
        )
        await UniMessage.send(
            UniMessage.reply(event.message_id),
            UniMessage(quote_image),
            target=target,
            actions=actions,
        )
    except Exception:
        _log_exception("AdvancedQuote 生成名人名言失败")
        try:
            await _send_text(
                event,
                actions,
                target,
                "名人名言生成失败，请稍后重试。",
            )
        except Exception:
            _log_exception("AdvancedQuote 发送失败提示失败")
    finally:
        if quote_image is not None:
            Quote.remove_rendered_image(quote_image)
    return True
