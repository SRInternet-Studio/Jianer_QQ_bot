"""Startup/shutdown/bot-connect hook registry for the plugin's jobs."""

from __future__ import annotations

import asyncio
from typing import Any, Callable, TypeVar

_F = TypeVar("_F", bound=Callable[..., Any])

_startup: list[Callable[..., Any]] = []
_shutdown: list[Callable[..., Any]] = []
_bot_connect: list[Callable[..., Any]] = []


def on_startup(func: _F) -> _F:
    if func not in _startup:
        _startup.append(func)
    return func


def on_shutdown(func: _F) -> _F:
    if func not in _shutdown:
        _shutdown.append(func)
    return func


def on_bot_connect(func: _F) -> _F:
    if func not in _bot_connect:
        _bot_connect.append(func)
    return func


async def run_startup_hooks() -> None:
    for hook in list(_startup):
        await hook()


async def run_shutdown_hooks() -> None:
    for hook in list(_shutdown):
        try:
            await hook()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


async def run_bot_connect_hooks(bot: Any) -> None:
    for hook in list(_bot_connect):
        try:
            await hook(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


def registered_hooks() -> dict[str, tuple[Callable[..., Any], ...]]:
    return {
        "startup": tuple(_startup),
        "shutdown": tuple(_shutdown),
        "bot_connect": tuple(_bot_connect),
    }


__all__ = [
    "on_bot_connect",
    "on_shutdown",
    "on_startup",
    "registered_hooks",
    "run_bot_connect_hooks",
    "run_shutdown_hooks",
    "run_startup_hooks",
]
