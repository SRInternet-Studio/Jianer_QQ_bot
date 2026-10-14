"""Wordle plugin: loads through the manager and answers commands."""

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import plugin_state


class _Actions:
    protocol = "onebot"
    capabilities = frozenset()

    def __init__(self) -> None:
        self.sent: list[tuple[str | None, str]] = []

    async def send(self, message=None, group_id=None, user_id=None, **kwargs):
        self.sent.append((group_id, str(message)))
        return SimpleNamespace(
            data=SimpleNamespace(message_id=str(len(self.sent)))
        )


def _configure() -> None:
    plugin_state.configure(
        config=SimpleNamespace(others={}, black_list=[]),
        logger=logging.getLogger("wordle_plugin_test"),
        reminder="~",
        bot_name="Jianer",
        bot_name_en="Jianer",
        one_slogan="test",
        confused_word="{bot_name} cannot do that",
        root_users=["1"],
        cooldowns={},
        cooldowns1={},
    )
    plugin_state.set_auth_snapshot(
        admins=["1"],
        supers=["1"],
        root_users=["1"],
        super_users=[],
        manage_users=[],
    )


def _reload():
    _configure()
    return plugin_state.reload_plugins()


def _wordle_module():
    manager = plugin_state.get_plugin_manager()
    plugin = manager.plugins["jianerbot-plugin-wordle"]
    return plugin.module


def _event(text: str, *, user_id: str = "9", group_id: str = "10001"):
    return SimpleNamespace(
        msg_str=text,
        message=text,
        group_id=group_id,
        user_id=user_id,
        self_id="1",
        message_id="m1",
        is_mentioned=False,
    )


async def _dispatch(
    text: str, *, user_id: str = "9", group_id: str = "10001"
) -> _Actions:
    _handled, actions = await _dispatch_handled(
        text, user_id=user_id, group_id=group_id
    )
    return actions


async def _dispatch_handled(
    text: str, *, user_id: str = "9", group_id: str = "10001"
) -> tuple[bool, _Actions]:
    actions = _Actions()
    handled = await plugin_state.dispatch_normal(
        _event(text, user_id=user_id, group_id=group_id),
        actions,
        message_text=text,
    )
    return handled, actions


def _text(actions: _Actions) -> str:
    assert actions.sent, "plugin sent nothing"
    return "\n".join(body for _, body in actions.sent)


def test_wordle_plugin_loads():
    result = _reload()
    assert result.failed == []
    assert "jianerbot-plugin-wordle" in result.loaded
    assert "wordle" in plugin_state.plugin_help_text()


def test_help_and_alias_work():
    _reload()
    help_text = _text(asyncio.run(_dispatch("~wordle 帮助")))
    assert "猜单词" in help_text
    alias = _text(asyncio.run(_dispatch("~猜单词 帮助")))
    assert "猜单词" in alias


def test_start_reports_length_dictionary_and_attempts():
    _reload()
    body = _text(asyncio.run(_dispatch("~wordle -l 5 -d CET4", group_id="20001")))
    assert "6 次机会" in body  # rows = length + 1
    assert "长度 5" in body
    assert "CET4" in body


def test_length_and_dictionary_are_validated():
    _reload()
    bad_length = _text(asyncio.run(_dispatch("~wordle -l 2", group_id="20002")))
    assert "3~8" in bad_length
    bad_dict = _text(
        asyncio.run(_dispatch("~wordle -d NOPE", group_id="20003"))
    )
    assert "词典" in bad_dict


def test_guess_validate_duplicate_and_win():
    _reload()
    module = _wordle_module()
    gid = "20004"
    started = _text(asyncio.run(_dispatch("~wordle -l 5", group_id=gid)))
    assert "机会" in started

    # Retrieve the secret that the plugin picked for this player.
    event = _event("~wordle", user_id="9", group_id=gid)
    key = module._conversation_key(event)
    secret = module._GAMES[key].game.word

    # Illegal guess is rejected.
    illegal = _text(asyncio.run(_dispatch("~wordle 猜 zzzzz", group_id=gid)))
    assert "合法" in illegal

    # A wrong-but-legal guess advances the board (unless it happens to be the
    # secret, which we avoid by picking a different word).
    filler = next(
        word
        for word in ("about", "brain", "crisp", "drain", "eagle")
        if word != secret
    )
    progress = _text(asyncio.run(_dispatch(f"~wordle 猜 {filler}", group_id=gid)))
    assert "剩余" in progress

    # Guessing the same word again is reported as duplicate.
    duplicate = _text(asyncio.run(_dispatch(f"~wordle 猜 {filler}", group_id=gid)))
    assert "已经猜过" in duplicate

    # Guessing the secret wins.
    win = _text(asyncio.run(_dispatch(f"~wordle 猜 {secret}", group_id=gid)))
    assert "猜出了单词" in win
    assert secret in win


def test_hint_status_and_stop_flow():
    _reload()
    module = _wordle_module()
    gid = "20005"
    asyncio.run(_dispatch("~wordle -l 5", group_id=gid))

    # No letters found yet.
    hint0 = _text(asyncio.run(_dispatch("~wordle 提示", group_id=gid)))
    assert "还没有猜中" in hint0

    event = _event("~wordle", user_id="9", group_id=gid)
    key = module._conversation_key(event)
    game = module._GAMES[key].game
    # Guess a word that shares at least one letter with the secret if possible.
    shared = next(
        (
            word
            for word in ("about", "brain", "crisp", "drain", "eagle", "flame")
            if any(letter in game.word for letter in word)
        ),
        None,
    )
    if shared:
        asyncio.run(_dispatch(f"~wordle 猜 {shared}", group_id=gid))
        hint = _text(asyncio.run(_dispatch("~wordle 提示", group_id=gid)))
        assert "绿色方块" in hint or "已猜中" in hint

    status = _text(asyncio.run(_dispatch("~wordle 状态", group_id=gid)))
    assert "剩余" in status

    stopped = _text(asyncio.run(_dispatch("~wordle 结束", group_id=gid)))
    assert "游戏已结束" in stopped
    after = _text(asyncio.run(_dispatch("~wordle 状态", group_id=gid)))
    assert "还没有进行中的游戏" in after


def test_guess_without_game_is_guided():
    _reload()
    body = _text(asyncio.run(_dispatch("~wordle 猜 apple", group_id="20006")))
    assert "还没有进行中的游戏" in body


def test_bare_word_is_a_guess_for_the_owning_player():
    _reload()
    module = _wordle_module()
    gid = "20010"
    asyncio.run(_dispatch("~wordle -l 5", group_id=gid))

    event = _event("x", user_id="9", group_id=gid)
    secret = module._GAMES[module._conversation_key(event)].game.word
    filler = next(
        word
        for word in ("about", "brain", "crisp", "drain", "eagle")
        if word != secret
    )

    handled, actions = asyncio.run(_dispatch_handled(filler, group_id=gid))
    assert handled is True
    assert "剩余" in _text(actions)

    # The bare hint/stop aliases are recognized too.
    handled_hint, hint_actions = asyncio.run(
        _dispatch_handled("提示", group_id=gid)
    )
    assert handled_hint is True
    assert hint_actions.sent


def test_bare_word_does_not_hijack_other_players_or_idle_users():
    _reload()
    gid = "20011"
    # No game at all -> bare word is not handled.
    handled_idle, idle = asyncio.run(
        _dispatch_handled("crane", user_id="7", group_id="20012")
    )
    assert handled_idle is False
    assert idle.sent == []

    # Another member's word is untouched while user 9 owns a game.
    asyncio.run(_dispatch("~wordle -l 5", group_id=gid))
    handled_other, other = asyncio.run(
        _dispatch_handled("crane", user_id="42", group_id=gid)
    )
    assert handled_other is False
    assert other.sent == []
