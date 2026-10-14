"""Pure Wordle game logic (no bot framework, no PIL).

Rules follow ``nonebot-plugin-wordle`` (MIT,
https://github.com/noneplugin/nonebot-plugin-wordle): green = right letter in
the right place, yellow = letter present but misplaced, gray = absent.  A
letter is colored yellow at most as many times as it appears in the answer.
"""

from __future__ import annotations

import random
import re
from enum import Enum
from typing import Iterable, Sequence


DEFAULT_LENGTH = 5
MIN_LENGTH = 3
MAX_LENGTH = 8

GREEN = "g"
YELLOW = "y"
ABSENT = "a"

_WORD_PATTERN = re.compile(r"^[a-z]+$")
_LETTER_CLEANER = re.compile(r"[^a-z]")


class GuessResult(Enum):
    WIN = "win"
    LOSS = "loss"
    DUPLICATE = "duplicate"
    ILLEGAL = "illegal"
    VALID = "valid"
    WRONG_LENGTH = "wrong_length"


def normalize_word(value: object) -> str:
    """Lower-case and keep only ascii letters."""

    return _LETTER_CLEANER.sub("", str(value or "").strip().casefold())


def is_alpha_word(value: object) -> bool:
    return bool(_WORD_PATTERN.fullmatch(normalize_word(value)))


def valid_length(value: object) -> int | None:
    try:
        length = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if MIN_LENGTH <= length <= MAX_LENGTH:
        return length
    return None


def evaluate_row(secret: str, guess: str) -> tuple[str, ...]:
    """Return one ``g`` / ``y`` / ``a`` code per letter of ``guess``."""

    secret_word = normalize_word(secret)
    guess_word = normalize_word(guess)
    if len(secret_word) != len(guess_word):
        raise ValueError("secret and guess must have the same length")

    codes = [ABSENT] * len(guess_word)
    remaining: dict[str, int] = {}
    for index, letter in enumerate(guess_word):
        if letter == secret_word[index]:
            codes[index] = GREEN
        else:
            target = secret_word[index]
            remaining[target] = remaining.get(target, 0) + 1
    for index, letter in enumerate(guess_word):
        if codes[index] == GREEN:
            continue
        if remaining.get(letter, 0) > 0:
            codes[index] = YELLOW
            remaining[letter] -= 1
    return tuple(codes)


class Wordle:
    """One player's active game."""

    def __init__(self, word: str, meaning: str = "") -> None:
        self.word = normalize_word(word)
        if not self.word:
            raise ValueError("secret word is empty")
        self.meaning = str(meaning or "").strip()
        self.length = len(self.word)
        self.rows = self.length + 1  # guesses allowed
        self.guessed_words: list[str] = []

    @property
    def used(self) -> int:
        return len(self.guessed_words)

    @property
    def remaining(self) -> int:
        return max(0, self.rows - self.used)

    @property
    def solved(self) -> bool:
        return bool(self.guessed_words) and self.guessed_words[-1] == self.word

    @property
    def finished(self) -> bool:
        return self.solved or self.used >= self.rows

    def result_text(self) -> str:
        if self.meaning:
            return f"【单词】：{self.word}\n【释义】：{self.meaning}"
        return f"【单词】：{self.word}"

    def guess(self, word: str) -> GuessResult:
        candidate = normalize_word(word)
        if len(candidate) != self.length:
            return GuessResult.WRONG_LENGTH
        if candidate == self.word:
            self.guessed_words.append(candidate)
            return GuessResult.WIN
        if candidate in self.guessed_words:
            return GuessResult.DUPLICATE
        self.guessed_words.append(candidate)
        if self.used >= self.rows:
            return GuessResult.LOSS
        return GuessResult.VALID

    def codes_for(self, word: str) -> tuple[str, ...]:
        return evaluate_row(self.word, word)

    def get_hint(self) -> str:
        """Reveal answer letters that already appear in any previous guess."""

        found = set()
        for word in self.guessed_words:
            if len(word) != self.length:
                continue
            for letter in word:
                if letter in self.word:
                    found.add(letter)
        return "".join(letter if letter in found else "*" for letter in self.word)

    def text_board(self) -> str:
        """Plain-text fallback board: ``A B C`` plus a color-square strip."""

        squares = {GREEN: "🟩", YELLOW: "🟨", ABSENT: "⬜"}
        lines: list[str] = []
        for word in self.guessed_words:
            codes = self.codes_for(word)
            letters = " ".join(word.upper())
            strip = "".join(squares[code] for code in codes)
            lines.append(f"{letters}    {strip}")
        for _ in range(self.remaining):
            lines.append(". " * (self.length - 1) + ".    " + "⬜" * self.length)
        return "\n".join(lines)


def pick_word(
    entries: Iterable[tuple[str, str]],
    rng: random.Random | None = None,
) -> tuple[str, str]:
    """Pick a ``(word, meaning)`` pair from ``entries``."""

    candidates = [
        (normalize_word(word), meaning)
        for word, meaning in entries
        if normalize_word(word)
    ]
    if not candidates:
        raise ValueError("词库为空")
    return (rng or random).choice(candidates)


def fallback_entries(words: Sequence[str]) -> list[tuple[str, str]]:
    return [(normalize_word(word), "") for word in words if normalize_word(word)]
