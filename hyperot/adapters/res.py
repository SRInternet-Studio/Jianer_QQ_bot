from .. import configurator

config = configurator.BotConfig.get("hyper-bot")

if config.protocol == "OneBot":
    from ..LecAdapters.OneBotLib.Res import SegmentBase, message_types
elif config.protocol == "Milky":
    from ..LecAdapters.MilkyLib.Res import SegmentBase, message_types
elif config.protocol == "Kritor":
    from ..LecAdapters.KritorLib.Res import SegmentBase, message_types
else:
    from ..LecAdapters.OneBotLib.Res import SegmentBase, message_types
