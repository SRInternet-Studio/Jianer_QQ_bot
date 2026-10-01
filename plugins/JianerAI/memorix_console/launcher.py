from __future__ import annotations

"""Optional background launcher for the JianerAI host.

The bot can call ``start_console_in_thread(kernel, config)`` after the memory
kernel has initialized.  The function is intentionally opt-in so importing the
plugin never binds a LAN port by itself.
"""

import threading
from typing import Any, Mapping

from .app import ConsoleConfig, serve


def start_console_in_thread(kernel: Any = None, *, config: Mapping[str, Any] | ConsoleConfig | None = None, memory_store: Any = None) -> threading.Thread:
    console_config = config if isinstance(config, ConsoleConfig) else ConsoleConfig.from_mapping(config)
    if not console_config.enabled:
        raise RuntimeError("memory console is disabled")
    thread = threading.Thread(
        target=serve,
        kwargs={"kernel": kernel, "memory_store": memory_store, "config": console_config},
        name="jianer-memory-console",
        daemon=True,
    )
    thread.start()
    return thread


__all__ = ["start_console_in_thread"]
