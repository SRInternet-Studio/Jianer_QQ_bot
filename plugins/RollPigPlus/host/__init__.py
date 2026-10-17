"""JianerCore host bindings for the RollPigPlus modules.

Every module under ``rollpig/`` talks to the bot through this package instead
of a third-party framework: logging, data paths, configuration, the message
builder, the OneBot API facade and the job scheduler.
"""

from . import lifecycle, paths
from .bot import (
    ActionFailed,
    Bot,
    bot_for,
    get_bot,
    get_bots,
    protocol_name,
    register_bot,
    reset_bots,
)
from .lifecycle import (
    on_bot_connect,
    on_shutdown,
    on_startup,
    run_bot_connect_hooks,
    run_shutdown_hooks,
    run_startup_hooks,
)
from .logger import logger
from .messages import Message, MessageSegment, reply_text
from .paths import localstore
from .scheduler import Scheduler, scheduler
from .settings import get_plugin_config

__all__ = [
    "ActionFailed",
    "Bot",
    "Message",
    "MessageSegment",
    "Scheduler",
    "bot_for",
    "get_bot",
    "get_bots",
    "get_plugin_config",
    "lifecycle",
    "localstore",
    "logger",
    "on_bot_connect",
    "on_shutdown",
    "on_startup",
    "paths",
    "protocol_name",
    "register_bot",
    "reply_text",
    "reset_bots",
    "run_bot_connect_hooks",
    "run_shutdown_hooks",
    "run_startup_hooks",
    "scheduler",
]
