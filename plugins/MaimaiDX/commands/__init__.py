"""Register every MaimaiDX command and expose message-wide routes."""

from . import alias as alias_commands
from . import base as base_commands
from . import guess as guess_commands
from . import score as score_commands
from . import search as search_commands
from . import table as table_commands


async def handle_raw(event, actions):
    for handler in (
        base_commands.handle_pending_oauth,
        guess_commands.handle_guess_answer,
        alias_commands.handle_alias_patterns,
        search_commands.handle_search_patterns,
        base_commands.handle_natural_patterns,
        table_commands.handle_table_patterns,
    ):
        if await handler(event, actions):
            return True
    return False
