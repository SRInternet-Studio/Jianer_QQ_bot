from . import configurator, events

config = configurator.BotConfig.get("hyper-bot")

__all__ = ["run", "reg", "stop", "Actions", "config"]

from .adapters.listener import *

events.init()
