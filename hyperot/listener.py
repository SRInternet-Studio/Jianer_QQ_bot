from . import configurator, events

import os
import sys

config = configurator.BotConfig.get("hyper-bot")

if config.protocol == "OneBot":
    from .LecAdapters.OneBot import *
elif config.protocol == "Milky":
    from .LecAdapters.Milky import *
elif config.protocol == "Kritor":
    from .LecAdapters.Kritor import *
elif config.protocol == "Feishu":
    from .LecAdapters.Feishu import *
else:
    from .LecAdapters.OneBot import *

__all__ = ["run", "reg", "stop", "restart", "Actions", "config"]

events.init()


def restart() -> None:
    stop()
    os.execv(sys.executable, [sys.executable] + sys.argv)
