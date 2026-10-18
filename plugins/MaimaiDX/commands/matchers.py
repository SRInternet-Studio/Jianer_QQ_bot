"""Alconna command registration for MaimaiDX.

The host projects QQ messages to plain text before plugin dispatch, so
``@机器人 b50`` reaches the command matchers as ``b50``.  MaimaiDX leaves
those messages to the JianerAI fallback, therefore every matcher built here
declines events that @ the bot account.
"""

from __future__ import annotations

from typing import Any

from arclet.alconna import Alconna
from jianer.plugins.builtin.alconna import CommandMatcher as _CommandMatcher

from .. import adapter


class CommandMatcher(_CommandMatcher):
    """Command matcher that ignores messages mentioning the bot itself."""

    async def dispatch(self, event: Any, actions: Any) -> bool:
        if adapter.mentions_self(event):
            return False
        return await super().dispatch(event, actions)


def Command(command: str | Alconna) -> CommandMatcher:
    """Drop-in ``Command`` replacement used by the command modules."""

    return CommandMatcher(command)
