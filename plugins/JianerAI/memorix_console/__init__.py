"""LAN memory console for the JianerAI Jianer Memory runtime.

The console is deliberately kept outside the memory implementation.  The host
can pass an active ``SDKMemoryKernel`` (or a compatible facade) to
``create_app`` when it starts the bot.
"""

from .adapter import MemoryConsoleAdapter
from .app import ConsoleConfig, create_app, serve
from .launcher import start_console_in_thread

__all__ = ["ConsoleConfig", "MemoryConsoleAdapter", "create_app", "serve", "start_console_in_thread"]
