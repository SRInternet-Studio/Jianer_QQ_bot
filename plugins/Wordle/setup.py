"""Wordle: guess an English word from an exam dictionary.

Adapted for JianerCore from ``nonebot-plugin-wordle`` (MIT,
https://github.com/noneplugin/nonebot-plugin-wordle).  Each player keeps their
own game; the board is rendered as an image when Pillow is available and falls
back to a plain-text grid otherwise.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
import uuid
from contextlib import suppress
from pathlib import Path

from arclet.alconna import Alconna, Args, MultiVar, Option
from jianer.plugins import PluginMetadata
from jianer.plugins.builtin.alconna import Command, UniMessage

from bot import plugin_state

from plugins.Wordle import render
from plugins.Wordle.dictionaries import (
    DEFAULT_DICTIONARY,
    dictionary_entries,
    is_legal,
    list_dictionaries,
)
from plugins.Wordle.game import (
    DEFAULT_LENGTH,
    GuessResult,
    Wordle,
    fallback_entries,
    is_alpha_word,
    normalize_word,
    pick_word,
    valid_length,
)


__plugin_meta__ = PluginMetadata(
    name="jianerbot-plugin-wordle",
    description="猜单词：在群聊里挑战 Wordle。",
    usage=(
        "{reminder}wordle [-l 长度] [-d 词典] —> 开始猜单词，默认为 5 字母 CET4\n"
        "{reminder}wordle 猜 <单词> —> 猜一个单词\n"
        "开局后群内任何人都可直接发送单词 / 提示 / 结束 —> 无需前缀\n"
        "{reminder}wordle 提示 —> 查看已猜中的字母\n"
        "{reminder}wordle 状态 —> 查看当前棋盘\n"
        "{reminder}wordle 结束 —> 结束当前游戏\n"
        "可用词典：CET4、CET6、专四、专八、考研、GRE、GMAT、TOEFL、SAT、IELTS"
    ),
    requires={"jianerbot-plugin-alconna"},
)


_REMINDER = str(plugin_state.get_runtime().get("reminder", "~"))
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_TEMP_DIR = _PROJECT_ROOT / "temps"
_GAME_TIMEOUT_SECONDS = 600

_FALLBACK_WORDS = (
    "apple",
    "brave",
    "cloud",
    "dream",
    "eagle",
    "flame",
    "grape",
    "house",
    "light",
    "music",
    "ocean",
    "piano",
    "queen",
    "river",
    "stone",
    "tiger",
    "unity",
    "vivid",
    "water",
    "youth",
)

_HELP_ALIASES = {"帮助", "help", "玩法", "规则", "菜单", "?"}
_START_ALIASES = {"开始", "开", "新", "开局", "新建", "新的一局", "start", "new"}
_GUESS_ALIASES = {"猜", "猜词", "猜单词", "猜测", "回答", "answer", "guess", "play"}
_HINT_ALIASES = {"提示", "提示一下", "提示词", "线索", "hint"}
_STATUS_ALIASES = {"状态", "棋盘", "查看", "进度", "盘面", "status", "board", "show"}
_STOP_ALIASES = {"结束", "停止", "放弃", "退出", "关闭", "stop", "end", "giveup", "quit"}


class _GameSlot:
    """An active game plus its idle deadline."""

    __slots__ = ("game", "deadline")

    def __init__(self, game: Wordle, deadline: float) -> None:
        self.game = game
        self.deadline = deadline

    def touch(self, now: float) -> None:
        self.deadline = now + _GAME_TIMEOUT_SECONDS

    def expired(self, now: float) -> bool:
        return now > self.deadline


_GAMES: dict[str, _GameSlot] = {}
_RNG = random.Random()


def _conversation_key(event) -> str | None:
    """Scene key: a group shares one game, a private chat is its own game.

    Mirrors nonebot-plugin-wordle, whose key is
    ``scope_self_id_scene_path`` (scene path is the group id for groups and the
    other user's id for private chats) so every member of a group guesses the
    same word.
    """

    self_id = str(getattr(event, "self_id", "") or "")
    group_id = getattr(event, "group_id", None)
    if group_id is not None:
        return f"{self_id}:group:{group_id}"
    user_id = str(getattr(event, "user_id", "") or "")
    if not user_id:
        return None
    return f"{self_id}:private:{user_id}"


def _route_key(event) -> str | None:
    return _conversation_key(event)


async def _send(
    event,
    actions,
    text: str = "",
    image: bytes | None = None,
    mention: str | None = None,
) -> None:
    """Send one reply through ``UniMessage`` (text and/or a rendered image)."""

    message = UniMessage()
    message_id = getattr(event, "message_id", None)
    if message_id is not None:
        message += UniMessage.reply(str(message_id))
    if mention:
        message += UniMessage.at(str(mention))
    if text:
        message += UniMessage.text(text)
    temp_path: Path | None = None
    if image:
        try:
            _TEMP_DIR.mkdir(parents=True, exist_ok=True)
            temp_path = _TEMP_DIR / f"wordle_{uuid.uuid4().hex}.png"
            temp_path.write_bytes(image)
            message += UniMessage.image(str(temp_path))
        except OSError:
            temp_path = None
    try:
        # Let UniMessage resolve the target from the event.
        await message.send(event=event, actions=actions)
    finally:
        if temp_path is not None:
            with suppress(OSError):
                temp_path.unlink()


def _help_text() -> str:
    dictionaries = "、".join(list_dictionaries())
    return (
        "◤ 猜单词 Wordle ◢ 群里所有人一起猜同一个单词\n"
        f"{_REMINDER}wordle [-l 长度] [-d 词典]　开始游戏（长度 3~8，默认 5 / CET4）\n"
        f"{_REMINDER}wordle 猜 <单词>　猜一个单词\n"
        "开局后也可以直接发送单词、提示、结束（不用带前缀）\n"
        "任何群成员都可以猜、看提示和结束这一局；答对的人会被 @\n"
        f"{_REMINDER}wordle 提示　查看已经猜中的字母\n"
        f"{_REMINDER}wordle 状态　查看棋盘\n"
        f"{_REMINDER}wordle 结束　结束游戏\n"
        "🟩 位置正确　🟨 字母在单词里但位置不对　⬜ 没有这个字母\n"
        f"可用词典：{dictionaries}"
    )


def _expire_if_needed(key: str, now: float) -> Wordle | None:
    slot = _GAMES.get(key)
    if slot is None:
        return None
    if slot.expired(now):
        _GAMES.pop(key, None)
        return slot.game
    return None


def _pick_entry(dictionary: str, length: int) -> tuple[str, str]:
    entries = dictionary_entries(dictionary, length)
    if not entries:
        entries = [
            item
            for item in fallback_entries(_FALLBACK_WORDS)
            if len(item[0]) == length
        ]
    return pick_word(entries, _RNG)


async def _handle_start(
    event,
    actions,
    *,
    length: int,
    dictionary: str,
) -> None:
    key = _route_key(event)
    if key is None:
        return
    now = time.time()
    expired = _expire_if_needed(key, now)
    if expired is not None:
        await _send(
            event,
            actions,
            f"上一局已经超时结束啦，答案是 {expired.word.upper()}。\n"
            f"现在开始新的一局～",
        )
    elif key in _GAMES:
        await _send(
            event,
            actions,
            f"你还有一局没结束呢，回复「{_REMINDER}wordle 结束」放弃后再开新局吧。",
        )
        return

    word, meaning = await asyncio.to_thread(_pick_entry, dictionary, length)
    game = Wordle(word, meaning)
    _GAMES[key] = _GameSlot(game, now + _GAME_TIMEOUT_SECONDS)
    if getattr(event, "group_id", None) is not None:
        header = (
            f"本局单词长度 {game.length}，共 {game.rows} 次机会，词典 {dictionary}。"
            f"群里的任何人都可以直接发送单词来猜，一起加油～"
        )
    else:
        header = (
            f"你有 {game.rows} 次机会猜出单词，长度 {game.length}，"
            f"词典 {dictionary}，请发送你的猜测。"
        )
    image = await asyncio.to_thread(render.render_board, game)
    if image is None:
        header += "\n" + game.text_board()
    await _send(event, actions, header, image)


async def _handle_guess(event, actions, word: str) -> None:
    key = _route_key(event)
    if key is None:
        return
    now = time.time()
    expired = _expire_if_needed(key, now)
    if expired is not None:
        await _send(
            event,
            actions,
            f"上一局已经超时结束了，答案是 {expired.word.upper()}。\n"
            f"发送「{_REMINDER}wordle」可以开新的一局。",
        )
        return
    slot = _GAMES.get(key)
    if slot is None:
        await _send(
            event,
            actions,
            f"你还没有进行中的游戏，发送「{_REMINDER}wordle」开始一局吧。",
        )
        return
    game = slot.game
    guess = normalize_word(word)
    if not guess:
        await _send(event, actions, f"要猜哪个单词呢？比如：{_REMINDER}wordle 猜 crane")
        return
    if len(guess) != game.length:
        await _send(
            event,
            actions,
            f"本局单词长度为 {game.length}，请猜一个 {game.length} 字母的单词。",
        )
        return
    if not is_alpha_word(guess):
        await _send(event, actions, "只能猜英文字母组成的单词哦。")
        return

    if guess in game.guessed_words:
        await _send(event, actions, f"「{guess}」已经猜过啦，换一个试试～")
        return
    if not await asyncio.to_thread(is_legal, guess):
        await _send(event, actions, f"「{guess}」好像不是一个合法的英文单词，再想想？")
        return

    slot.touch(now)
    result = game.guess(guess)
    if result is GuessResult.DUPLICATE:
        await _send(event, actions, f"「{guess}」已经猜过啦，换一个试试～")
        return
    if result is GuessResult.WRONG_LENGTH:
        await _send(event, actions, f"请猜一个 {game.length} 字母的单词。")
        return

    if result in (GuessResult.WIN, GuessResult.LOSS):
        _GAMES.pop(key, None)
        mention: str | None = None
        if result is GuessResult.WIN:
            head = "🎉 恭喜猜出了单词！"
            if getattr(event, "group_id", None) is not None:
                guesser = str(getattr(event, "user_id", "") or "")
                mention = guesser or None
        else:
            head = "💥 很遗憾，次数用完啦～"
        image = await asyncio.to_thread(render.render_board, game)
        text = f"{head}\n{game.result_text()}"
        if image is None:
            text += "\n" + game.text_board()
        await _send(event, actions, text, image, mention=mention)
        return

    progress = f"第 {game.used}/{game.rows} 次　剩余 {game.remaining} 次"
    image = await asyncio.to_thread(render.render_board, game)
    if image is None:
        await _send(event, actions, f"{game.text_board()}\n{progress}")
    else:
        await _send(event, actions, progress, image)


async def _handle_hint(event, actions) -> None:
    key = _route_key(event)
    if key is None:
        return
    now = time.time()
    if _expire_if_needed(key, now) is not None:
        await _send(event, actions, f"上一局已经超时结束，发送「{_REMINDER}wordle」重新开始吧。")
        return
    slot = _GAMES.get(key)
    if slot is None:
        await _send(event, actions, f"你还没有进行中的游戏，发送「{_REMINDER}wordle」开始吧。")
        return
    slot.touch(now)
    game = slot.game
    hint = game.get_hint()
    if not hint.replace("*", ""):
        await _send(event, actions, "你还没有猜中过任何一个字母哦，再猜猜看～")
        return
    image = await asyncio.to_thread(render.render_hint, hint)
    if image is None:
        await _send(event, actions, f"已猜中的字母：{hint.upper()}")
    else:
        await _send(event, actions, "绿色方块是已经猜中的字母位置：", image)


async def _handle_status(event, actions) -> None:
    key = _route_key(event)
    if key is None:
        return
    now = time.time()
    if _expire_if_needed(key, now) is not None:
        await _send(event, actions, f"上一局已经超时结束，发送「{_REMINDER}wordle」重新开始吧。")
        return
    slot = _GAMES.get(key)
    if slot is None:
        await _send(event, actions, f"你还没有进行中的游戏，发送「{_REMINDER}wordle」开始吧。")
        return
    slot.touch(now)
    game = slot.game
    progress = f"第 {game.used}/{game.rows} 次　剩余 {game.remaining} 次"
    image = await asyncio.to_thread(render.render_board, game)
    if image is None:
        await _send(event, actions, f"{game.text_board()}\n{progress}")
    else:
        await _send(event, actions, progress, image)


async def _handle_stop(event, actions) -> None:
    key = _route_key(event)
    if key is None:
        return
    slot = _GAMES.pop(key, None)
    if slot is None:
        await _send(event, actions, "你还没有进行中的游戏哦。")
        return
    await _send(event, actions, f"游戏已结束。\n{slot.game.result_text()}")


def _route(rest: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    tokens = list(rest or ())
    if not tokens:
        return "start", ()
    head = str(tokens[0]).strip()
    lowered = head.casefold()
    if head in _HELP_ALIASES or lowered in _HELP_ALIASES:
        return "help", tuple(tokens[1:])
    if head in _START_ALIASES or lowered in _START_ALIASES:
        return "start", tuple(tokens[1:])
    if head in _GUESS_ALIASES or lowered in _GUESS_ALIASES:
        return "guess", tuple(tokens[1:])
    if head in _HINT_ALIASES or lowered in _HINT_ALIASES:
        return "hint", ()
    if head in _STATUS_ALIASES or lowered in _STATUS_ALIASES:
        return "status", ()
    if head in _STOP_ALIASES or lowered in _STOP_ALIASES:
        return "stop", ()
    if is_alpha_word(head):
        return "guess", tuple(tokens)
    return "help", tuple(tokens)


_ALPHA_WORD = re.compile(r"^[A-Za-z]+$")


def _plain_message_text(event) -> str:
    """Best-effort single-line text of the incoming message."""

    value = getattr(event, "msg_str", None)
    if value is None:
        value = getattr(event, "message", "")
    return str(value or "").strip()


_WORDLE_COMMAND = Alconna(
    f"{_REMINDER}wordle",
    Option("-l|--length", Args["length", int], help_text="单词长度 3~8"),
    Option("-d|--dictionary", Args["dictionary", str], help_text="词典"),
    Args["rest", MultiVar(str, "*")],
)

_ALIAS_COMMAND = Alconna(
    f"{_REMINDER}猜单词",
    Option("-l|--length", Args["length", int], help_text="单词长度 3~8"),
    Option("-d|--dictionary", Args["dictionary", str], help_text="词典"),
    Args["rest", MultiVar(str, "*")],
)


def _resolve_length(value) -> int:
    if value is None:
        return DEFAULT_LENGTH
    return valid_length(value) or 0


def _resolve_dictionary(value) -> str:
    if not value:
        return DEFAULT_DICTIONARY
    return str(value).strip()


@Command(_WORDLE_COMMAND).handle()
@Command(_ALIAS_COMMAND).handle()
async def _handle_wordle(
    event,
    actions,
    rest: tuple[str, ...] = (),
    length=None,
    dictionary=None,
):
    if _conversation_key(event) is None:
        return False
    action, arguments = _route(tuple(rest or ()))
    if action == "start":
        resolved_length = _resolve_length(length)
        if not resolved_length:
            await _send(event, actions, "单词长度需要在 3~8 之间。")
            return True
        resolved_dictionary = _resolve_dictionary(dictionary)
        if resolved_dictionary not in list_dictionaries():
            await _send(
                event,
                actions,
                "不认识的词典，支持：" + "、".join(list_dictionaries()),
            )
            return True
        await _handle_start(
            event,
            actions,
            length=resolved_length,
            dictionary=resolved_dictionary,
        )
    elif action == "guess":
        word = " ".join(arguments).strip()
        if not word:
            await _send(event, actions, f"要猜哪个单词呢？比如：{_REMINDER}wordle 猜 crane")
            return True
        await _handle_guess(event, actions, word)
    elif action == "hint":
        await _handle_hint(event, actions)
    elif action == "status":
        await _handle_status(event, actions)
    elif action == "stop":
        await _handle_stop(event, actions)
    else:
        await _send(event, actions, _help_text())
    return True


async def on_message(event, actions) -> bool:
    """Catch a bare guess (and 提示/结束/状态) while a game is running.

    Command messages like ``~wordle 猜 crane`` are handled by the Alconna
    matcher before this hook runs; here we mirror the reference plugin, where a
    player can simply send the word itself.  A group shares one game, so any
    member's message is intercepted while that group's game is running; chats
    without an active game are never touched.
    """

    key = _route_key(event)
    if key is None:
        return False
    slot = _GAMES.get(key)
    if slot is None:
        return False
    text = _plain_message_text(event)
    if not text:
        return False
    head = text.strip()
    lowered = head.casefold()
    if head in _HINT_ALIASES or lowered in _HINT_ALIASES:
        await _handle_hint(event, actions)
        return True
    if head in _STOP_ALIASES or lowered in _STOP_ALIASES:
        await _handle_stop(event, actions)
        return True
    if head in _STATUS_ALIASES or lowered in _STATUS_ALIASES:
        await _handle_status(event, actions)
        return True
    if _ALPHA_WORD.fullmatch(head) and len(head) == slot.game.length:
        await _handle_guess(event, actions, head)
        return True
    return False


def setup(client, manager) -> None:
    """Directory-plugin entry hook; state is module-level and ready to use."""
