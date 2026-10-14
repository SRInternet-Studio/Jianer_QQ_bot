"""Wordle plugin: rules, hints, rendering, and dictionaries."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves ``cls.__module__`` via sys.modules at class
    # creation time, so the module must be registered before exec.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


game = _load("wordle_game_under_test", "plugins/Wordle/game.py")


def test_evaluate_row_basic_colors():
    assert game.evaluate_row("crane", "crane") == ("g",) * 5
    assert game.evaluate_row("crane", "pouty") == ("a",) * 5
    assert game.evaluate_row("crane", "nacre") == ("y", "y", "y", "y", "g")


def test_evaluate_row_does_not_over_color_repeated_letters():
    # Answer "adapt" has a single "p"; in "apple" the first p is yellow and the
    # second must be gray because the answer only contains one p.
    assert game.evaluate_row("adapt", "apple") == ("g", "y", "a", "a", "a")
    # The same rule applies to greens already consumed.
    assert game.evaluate_row("apple", "eerie") == ("a", "a", "a", "a", "g")


def test_normalize_and_length_helpers():
    assert game.normalize_word("  CrAnE!! ") == "crane"
    assert game.is_alpha_word("Apple") is True
    assert game.is_alpha_word("cr4ne") is True  # digits stripped -> "crne"
    assert game.is_alpha_word("...") is False
    assert game.valid_length(6) == 6
    assert game.valid_length("4") == 4
    assert game.valid_length(2) is None
    assert game.valid_length(9) is None
    assert game.valid_length("x") is None


def test_wordle_lifecycle_win():
    play = game.Wordle("crane", "起重机")
    assert play.length == 5
    assert play.rows == 6
    assert play.guess("about") is game.GuessResult.VALID
    assert play.guess("crane") is game.GuessResult.WIN
    assert play.solved is True
    assert play.finished is True
    assert "crane" in play.result_text()
    assert "起重机" in play.result_text()


def test_wordle_lifecycle_loss_and_duplicate():
    play = game.Wordle("crane")
    assert play.guess("about") is game.GuessResult.VALID
    assert play.guess("about") is game.GuessResult.DUPLICATE
    # Five more distinct guesses exhaust the six allowed rows.
    for word in ("brain", "crisp", "drain", "eagle", "flame"):
        result = play.guess(word)
    assert result is game.GuessResult.LOSS
    assert play.finished is True
    assert play.remaining == 0


def test_wordle_rejects_wrong_length():
    play = game.Wordle("crane")
    assert play.guess("cat") is game.GuessResult.WRONG_LENGTH
    assert play.guessed_words == []


def test_wordle_hint_reveals_found_letters_only():
    play = game.Wordle("crane")
    assert play.get_hint() == "*****"
    play.guess("about")  # a is present but misplaced
    assert play.get_hint() == "**a**"  # only the 'a' found so far is revealed
    play.guess("nacre")  # n/a/c/r present, e exact
    assert play.get_hint() == "crane"


def test_text_board_fallback_contains_rows_and_squares():
    play = game.Wordle("crane")
    play.guess("crane")
    board = play.text_board().splitlines()
    assert len(board) == play.rows
    assert "C R A N E" in board[0]
    assert "🟩🟩🟩🟩🟩" in board[0]


def test_pick_word_returns_playable_entry():
    import random

    rng = random.Random(0)
    word, meaning = game.pick_word([("Crane", "起重机"), ("about", "关于")], rng)
    assert word in {"crane", "about"}
    assert meaning
    with pytest.raises(ValueError):
        game.pick_word([], rng)


def test_render_board_produces_png_bytes():
    from plugins.Wordle import render

    if not render.available():
        pytest.skip("Pillow is not installed")
    play = game.Wordle("crane")
    play.guess("crane")
    data = render.render_board(play)
    assert isinstance(data, bytes)
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    hint = render.render_hint("cr***")
    assert hint is not None and hint.startswith(b"\x89PNG\r\n\x1a\n")
