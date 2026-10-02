from __future__ import annotations

"""Optional background launcher for the JianerAI host.

The bot can call ``start_console_in_thread(kernel, config)`` after the memory
kernel has initialized.  The function is intentionally opt-in so importing the
plugin never binds a LAN port by itself.
"""

import threading
import time
from typing import Any, Mapping

from .app import ConsoleConfig, create_app


class MemoryConsoleThread(threading.Thread):
    def __init__(self, kernel: Any, memory_store: Any, config: ConsoleConfig) -> None:
        import uvicorn

        app = create_app(kernel, memory_store=memory_store, config=config)
        self.server = uvicorn.Server(
            uvicorn.Config(app, host=config.host, port=config.port, log_level="info")
        )
        self.startup_error: BaseException | None = None
        super().__init__(target=self._run_server, name="jianer-memory-console", daemon=True)

    def _run_server(self) -> None:
        try:
            self.server.run()
        except BaseException as exc:
            self.startup_error = exc
            raise

    def wait_until_ready(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + max(0.1, float(timeout))
        while self.is_alive() and not self.server.started:
            if self.startup_error is not None:
                raise RuntimeError("memory console failed to start") from self.startup_error
            if time.monotonic() >= deadline:
                raise TimeoutError("memory console startup timed out")
            time.sleep(0.025)
        if not self.server.started:
            if self.startup_error is not None:
                raise RuntimeError("memory console failed to start") from self.startup_error
            raise RuntimeError("memory console stopped before becoming ready")

    def stop(self) -> None:
        self.server.should_exit = True
        self.join(timeout=10.0)
        if self.is_alive():
            self.server.force_exit = True
            self.join(timeout=2.0)
        if self.is_alive():
            raise RuntimeError("memory console did not stop")


def start_console_in_thread(
    kernel: Any = None,
    *,
    config: Mapping[str, Any] | ConsoleConfig | None = None,
    memory_store: Any = None,
) -> MemoryConsoleThread:
    console_config = config if isinstance(config, ConsoleConfig) else ConsoleConfig.from_mapping(config)
    if not console_config.enabled:
        raise RuntimeError("memory console is disabled")
    thread = MemoryConsoleThread(kernel, memory_store, console_config)
    thread.start()
    return thread


__all__ = ["start_console_in_thread"]
