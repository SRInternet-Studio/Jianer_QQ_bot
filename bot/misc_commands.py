"""Host-only sleep command; all other native commands live in plugins."""


async def _send(actions, Manager, Segments, event, text):
    await actions.send(
        group_id=event.group_id,
        message=Manager.Message(Segments.Text(text)),
    )


async def _confused(actions, Manager, Segments, event, CONFUSED_WORD, bot_name):
    await _send(
        actions,
        Manager,
        Segments,
        event,
        CONFUSED_WORD.format(bot_name=bot_name),
    )


async def cmd_sleep(
    actions,
    Manager,
    Segments,
    event,
    ADMINS,
    ROOT_User,
    CONFUSED_WORD,
    bot_name,
    get_user_nickname,
):
    """Return True when the host should enter the sleep gate."""

    if str(event.user_id) not in ADMINS:
        await _confused(
            actions,
            Manager,
            Segments,
            event,
            CONFUSED_WORD,
            bot_name,
        )
        return False
    nick = await get_user_nickname(
        event.user_id,
        Manager,
        actions,
        sender=getattr(event, "sender", None),
    )
    await actions.send(
        user_id=ROOT_User[0],
        message=Manager.Message(
            Segments.Text(
                f"用户 {nick} 在 {event.time_str} 休眠QQ机器人"
            )
        ),
    )
    await _send(
        actions,
        Manager,
        Segments,
        event,
        f"谢谢喵，{bot_name}睡觉去了 ヾ(＠ ˘ω˘ ＠)ノ💤",
    )
    return True
